import asyncio
import base64
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from html import escape as html_escape
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- Constants ---
SEARCHES_FILE = "searches.json"
SEEN_FILE = "seen_listings.json"
MARKET_TRACKING_FILE = "market_tracking.json"
MARKET_SOLD_LOG_FILE = "market_sold_log.json"
MAX_SEEN = 5000
MAX_SOLD_LOG = 1000
BROADCAST_LIMIT = 200  # max eBay Browse API allows per call; costs the same 1 API call regardless
DEFAULT_POLL_INTERVAL = 15
TOKEN_TTL = 5400
STATUS_INTERVAL = 3600
SLEEP_CHECK_INTERVAL = 60  # how often to re-check the clock while asleep/paused
EBAY_DAILY_LIMIT = 5000
NO_PRICE = float("inf")  # used when eBay returns no price (always fails max_price check)

EXCLUSIONS = [
    "ecc", "server", "apple", "mac", "macbook", "rdimm", "lrdimm",
    "for parts", "parts only", "not working", "as is",
    "ddr5", "ddr3", "ddr2", "sodimm",
]

# --- Awake-hours schedule ---
# Weekdays (Mon-Fri): 7:00-23:00. Weekends (Sat-Sun): 9:00 through 2:00 the next morning.
WEEKDAY_START_HOUR = 7
WEEKDAY_END_HOUR = 23
WEEKEND_START_HOUR = 9
WEEKEND_END_HOUR = 2  # next calendar day

# --- Env vars ---
def _require(name):
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val

EBAY_APP_ID            = _require("EBAY_APP_ID")
EBAY_CERT_ID           = _require("EBAY_CERT_ID")
DISCORD_WEBHOOK        = _require("DISCORD_WEBHOOK")
# Optional second webhook for an unfiltered "test channel" feed — every scanned listing gets posted
# there with exact timing info, regardless of exclusions/filters. Leave unset to disable.
TEST_DISCORD_WEBHOOK   = os.environ.get("TEST_DISCORD_WEBHOOK", "")
DISCORD_BOT_TOKEN      = _require("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID     = int(_require("DISCORD_CHANNEL_ID"))
DISCORD_LOG_CHANNEL_ID = int(_require("DISCORD_LOG_CHANNEL_ID"))
DISCORD_GUILD_ID       = int(_require("DISCORD_GUILD_ID"))

# IANA timezone the awake-hours schedule is evaluated in. MUST match where you actually live —
# set this in Render's env vars. Defaults to Central time if unset.
SCANNER_TZ = ZoneInfo(os.environ.get("SCANNER_TZ", "America/Chicago"))

# Sensitive — env vars with backwards-compat fallbacks. Add these to Render to remove from source.
VERIFICATION_TOKEN = os.environ.get(
    "EBAY_VERIFICATION_TOKEN",
    "qawfjoewjfoiewfsadfjjwqoifjewoifjoiwjfluhojflanfmdnugjwoiqjfnewfow",
)
ENDPOINT = os.environ.get(
    "EBAY_DELETION_ENDPOINT",
    "https://ebay-compliance-5902.onrender.com/ebay-deletion",
)

# --- Stats ---
stats = {
    "alerts_sent": 0,
    "last_scan_at": 0,
    "started_at": time.time(),
}
_stats_lock = threading.Lock()

# --- Debug mode ---
debug_mode = False

# --- Pause flag (manual, via /pause) ---
paused = False

# --- eBay API call tracking ---
api_calls = {
    "total": 0,
    "timestamps": [],   # rolling window of call times for rate calculation
    "day_start": time.time(),
    "calls_today": 0,
}
_api_lock = threading.Lock()

def record_api_call():
    now = time.time()
    with _api_lock:
        api_calls["total"] += 1
        api_calls["calls_today"] += 1
        api_calls["timestamps"].append(now)
        # Reset daily counter at midnight (86400s)
        if now - api_calls["day_start"] >= 86400:
            api_calls["calls_today"] = 1
            api_calls["day_start"] = now
        # Keep only last 2 hours of timestamps for rate calc
        cutoff = now - 7200
        api_calls["timestamps"] = [t for t in api_calls["timestamps"] if t > cutoff]

def get_api_projection():
    with _api_lock:
        calls_today = api_calls["calls_today"]
        timestamps = list(api_calls["timestamps"])
        day_start = api_calls["day_start"]

    elapsed = time.time() - day_start
    if elapsed < 60 or len(timestamps) < 2:
        return calls_today, None

    # Rate based on rolling window
    window = timestamps[-1] - timestamps[0]
    if window <= 0:
        return calls_today, None

    rate_per_sec = len(timestamps) / window
    remaining_secs = 86400 - elapsed
    projected = int(calls_today + rate_per_sec * remaining_secs)
    return calls_today, projected

# --- Awake-hours schedule ---
def is_awake(dt=None):
    dt = dt or datetime.now(SCANNER_TZ)
    dow = dt.weekday()  # Mon=0 ... Sun=6
    hour = dt.hour

    if dow <= 4:  # Mon-Fri
        awake = WEEKDAY_START_HOUR <= hour < WEEKDAY_END_HOUR
        if dow == 0:  # Monday: tail end of Sunday night's weekend window
            awake = awake or hour < WEEKEND_END_HOUR
        return awake
    else:  # Sat=5, Sun=6
        awake = hour >= WEEKEND_START_HOUR
        if dow == 6:  # Sunday: tail end of Saturday night's weekend window
            awake = awake or hour < WEEKEND_END_HOUR
        return awake

# --- Config (broadcast search + filters) ---
_config_lock = threading.Lock()

def load_config():
    try:
        with open(SEARCHES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"broadcast": {}, "filters": []}

def save_config(config):
    with open(SEARCHES_FILE, "w") as f:
        json.dump(config, f, indent=2)

# --- Seen listings (ordered: dict in Python 3.7+ preserves insertion order, O(1) lookups) ---
def _load_seen():
    try:
        with open(SEEN_FILE) as f:
            data = json.load(f)
            return {item_id: True for item_id in data}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_seen(seen):
    items = list(seen.keys())
    with open(SEEN_FILE, "w") as f:
        json.dump(items, f)

def _add_seen(item_id):
    SEEN_LISTINGS[item_id] = True
    while len(SEEN_LISTINGS) > MAX_SEEN:
        # Remove oldest entry (first inserted)
        oldest = next(iter(SEEN_LISTINGS))
        del SEEN_LISTINGS[oldest]

SEEN_LISTINGS = _load_seen()

# --- Market trend tracking ---
# MARKET_TRACKING: item_id -> {title, price, created_at, first_seen, matched_filters} for listings
# currently active in the broadcast results. When a tracked item stops showing up in the active
# results, it's assumed sold/ended and moved into SOLD_LOG with a computed duration.
def _load_market_tracking():
    try:
        with open(MARKET_TRACKING_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_market_tracking(tracking):
    with open(MARKET_TRACKING_FILE, "w") as f:
        json.dump(tracking, f)

def _load_sold_log():
    try:
        with open(MARKET_SOLD_LOG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def _save_sold_log(log):
    with open(MARKET_SOLD_LOG_FILE, "w") as f:
        json.dump(log, f)

MARKET_TRACKING = _load_market_tracking()
SOLD_LOG = _load_sold_log()
_market_lock = threading.Lock()

def _update_market_tracking(items, filters):
    """Diff this cycle's active items against MARKET_TRACKING to detect listings that dropped out
       of the active set (proxy for sold/ended), and record how long they were up."""
    now_iso = datetime.now(timezone.utc).isoformat()
    current_ids = set()
    dirty = False

    with _market_lock:
        for item in items:
            item_id = item.get("itemId")
            if not item_id:
                continue
            current_ids.add(item_id)
            if item_id in MARKET_TRACKING:
                continue

            title = item.get("title", "")
            price_val = item.get("price", {}).get("value")
            price = float(price_val) if price_val is not None else None
            matched = [
                f["name"] for f in filters
                if price is not None and price <= f["max_price"] and _matches(title, f)
            ]
            MARKET_TRACKING[item_id] = {
                "title": title,
                "price": price,
                "created_at": item.get("itemCreationDate") or now_iso,
                "first_seen": now_iso,
                "matched_filters": matched,
            }
            dirty = True

        for item_id in [iid for iid in MARKET_TRACKING if iid not in current_ids]:
            entry = MARKET_TRACKING.pop(item_id)
            try:
                created_dt = datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00"))
            except Exception:
                created_dt = datetime.fromisoformat(entry["first_seen"])
            gone_dt = datetime.now(timezone.utc)
            duration_secs = max(0, int((gone_dt - created_dt).total_seconds()))
            SOLD_LOG.append({
                "title": entry["title"],
                "price": entry["price"],
                "matched_filters": entry["matched_filters"],
                "created_at": entry["created_at"],
                "gone_at": gone_dt.isoformat(),
                "duration_secs": duration_secs,
            })
            dirty = True

        del SOLD_LOG[:-MAX_SOLD_LOG]

        if dirty:
            _save_market_tracking(MARKET_TRACKING)
            _save_sold_log(SOLD_LOG)

# --- HTTP session (keep-alive saves ~200ms per call) ---
_http = requests.Session()

# --- eBay auth ---
def get_access_token():
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    r = _http.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope",
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]

# --- Discord webhook ---
def _discord(payload, retries=3, webhook_url=None):
    url = webhook_url or DISCORD_WEBHOOK
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 429:
                time.sleep(r.json().get("retry_after", 1))
                continue
            r.raise_for_status()
            return
        except Exception as e:
            print(f"Webhook error (attempt {attempt + 1}): {e}")
            time.sleep(2 ** attempt)

# --- Bot log channel ---
_bot_loop = None
_bot_initialized = False  # prevents duplicate startup work on Discord reconnects

def _log(message):
    print(message)
    if _bot_loop is None:
        return
    async def _send():
        try:
            ch = bot.get_channel(DISCORD_LOG_CHANNEL_ID)
            if ch:
                await ch.send(message)
        except Exception as e:
            print(f"Log send error: {e}")
    asyncio.run_coroutine_threadsafe(_send(), _bot_loop)

# --- Listing age ---
MAX_LISTING_AGE = 86400  # 24 hours in seconds

def get_listing_age_seconds(item):
    raw = item.get("itemCreationDate")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return None

def get_listing_age(item):
    age_secs = get_listing_age_seconds(item)
    if age_secs is None:
        return None
    if age_secs < 60:
        return f"{age_secs}s old"
    elif age_secs < 3600:
        return f"{age_secs // 60}m old"
    else:
        h = age_secs // 3600
        m = (age_secs % 3600) // 60
        return f"{h}h {m}m old"

# --- Alerts & status ---
def send_startup_message():
    config = load_config()
    broadcast = config.get("broadcast", {})
    filters = config.get("filters", [])
    lines = "\n".join(
        f"• **{f['name']}** — max ${f['max_price']}"
        for f in filters
    ) or "No filters configured."
    pid = os.getpid()
    _discord({
        "embeds": [{
            "title": "🟢 eBay Scanner is live",
            "description": (
                f"**PID: `{pid}`**\n\n"
                f"**Broadcast query:** `{broadcast.get('query', '(none)')}` every {broadcast.get('poll_interval', DEFAULT_POLL_INTERVAL)}s\n\n"
                f"**Filters:**\n{lines}"
            ),
            "color": 0x00FF00,
        }]
    })
    _log(f"🟢 Scanner started (PID {pid}).")

def send_alert(title, price, url, filt, item):
    age = get_listing_age(item)
    item_id = item.get("itemId", "?")
    fields = [
        {"name": "Price",   "value": f"${price:.2f}",                       "inline": True},
        {"name": "Search",  "value": filt.get("label") or filt["name"],     "inline": True},
        {"name": "Item ID", "value": f"`{item_id}`",                        "inline": True},
    ]
    if age:
        fields.append({"name": "Listed", "value": age, "inline": True})

    payload = {
        "embeds": [{
            "title": title,
            "url": url,
            "color": filt.get("color", 0x00BFFF),
            "fields": fields,
        }]
    }
    # Fire-and-forget: a slow/rate-limited webhook must never stall the scan loop.
    threading.Thread(target=_discord, args=(payload,), daemon=True).start()

    with _stats_lock:
        stats["alerts_sent"] += 1
    _log(f"🔔 Alert: [{filt['name']}] {title} — ${price:.2f}" + (f" ({age})" if age else ""))

def build_status_embed():
    uptime = int(time.time() - stats["started_at"])
    hours, rem = divmod(uptime, 3600)
    minutes = rem // 60
    config = load_config()
    broadcast = config.get("broadcast", {})
    filters = config.get("filters", [])
    calls_today, projected = get_api_projection()
    pct = f"{calls_today / EBAY_DAILY_LIMIT * 100:.1f}%"
    proj_str = f"{projected:,}" if projected is not None else "calculating..."
    warn = " ⚠️" if (projected or 0) > EBAY_DAILY_LIMIT else ""

    last_scan_secs = int(time.time() - stats["last_scan_at"]) if stats["last_scan_at"] else None
    last_scan_str = f"{last_scan_secs}s ago" if last_scan_secs is not None else "never"

    awake = is_awake()
    if paused:
        title = "⏸️ Scanner Status (PAUSED)"
        color = 0x808080
    elif not awake:
        title = "😴 Scanner Status (SLEEPING)"
        color = 0x2C2F33
    else:
        title = "📊 Scanner Status"
        color = 0xFF4500 if (projected or 0) > EBAY_DAILY_LIMIT else 0x5865F2

    return {
        "embeds": [{
            "title": title,
            "color": color,
            "fields": [
                {"name": "Uptime",            "value": f"{hours}h {minutes}m",         "inline": True},
                {"name": "Last Scan",         "value": last_scan_str,                   "inline": True},
                {"name": "Alerts Sent",       "value": str(stats["alerts_sent"]),       "inline": True},
                {"name": "API Calls Today",   "value": f"{calls_today:,} / {EBAY_DAILY_LIMIT:,} ({pct})", "inline": True},
                {"name": f"Projected 24h{warn}", "value": proj_str,                    "inline": True},
                {"name": "Poll Interval",     "value": f"{broadcast.get('poll_interval', DEFAULT_POLL_INTERVAL)}s", "inline": True},
                {"name": "Active Filters",    "value": str(len(filters)),               "inline": True},
            ],
        }]
    }

# --- Scanner ---
def _matches(title, filt):
    t = title.lower()
    for x in filt.get("exclude", []):
        if x.lower() in t:
            return False
    must = filt.get("must_contain", [])
    if must and not any(k.lower() in t for k in must):
        return False
    return True

def _fetch_broadcast(token, broadcast, filters):
    """One API call for the whole cycle: broad query, capped at the highest filter's max_price."""
    max_price = max((f["max_price"] for f in filters), default=1000)
    params = {
        "q": broadcast.get("query", ""),
        "filter": f"price:[..{max_price}],priceCurrency:USD,conditions:{{NEW|USED_EXCELLENT|USED_GOOD|USED_ACCEPTABLE}}",
        "sort": "newlyListed",
        "limit": str(BROADCAST_LIMIT),
        "fieldgroups": "EXTENDED",
    }
    if broadcast.get("category_id"):
        params["category_ids"] = broadcast["category_id"]

    try:
        record_api_call()
        r = _http.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("itemSummaries", []), None
    except Exception as e:
        return [], e

def _process_results(items, filters, broadcast):
    """Dedupe against the global seen set, then run every filter against each new item in code.
       Returns (new_count, excluded_count, matched_count, test_feed) so the cycle log can show a real
       breakdown instead of just "N new items" (which includes excluded/non-matching noise), and the
       unfiltered test-channel feed (empty list if TEST_DISCORD_WEBHOOK isn't configured)."""
    new_count = 0
    excluded_count = 0
    matched_count = 0
    test_feed = []
    collect_test_feed = bool(TEST_DISCORD_WEBHOOK)
    is_ram_category = broadcast.get("category_id") == "170083"

    for item in items:
        item_id = item.get("itemId")
        title = item.get("title", "")
        t = title.lower()
        price_val = item.get("price", {}).get("value")
        price = float(price_val) if price_val is not None else NO_PRICE

        if item_id in SEEN_LISTINGS:
            if debug_mode:
                _log(f"[DEBUG] SEEN | ${price:.2f} | {title[:60]}")
            continue

        _add_seen(item_id)
        new_count += 1

        excluded = is_ram_category and any(x in t for x in EXCLUSIONS)
        if excluded:
            excluded_count += 1

        age_secs = get_listing_age_seconds(item)
        too_old = age_secs is not None and age_secs > MAX_LISTING_AGE

        matched_filters = []
        if not excluded and not too_old:
            for filt in filters:
                if price <= filt["max_price"] and _matches(title, filt):
                    matched_filters.append(filt)

        url = item.get("itemWebUrl", "")
        if matched_filters:
            matched_count += 1
            for filt in matched_filters:
                send_alert(title, price, url, filt, item)

        if debug_mode:
            if excluded:
                _log(f"[DEBUG] EXCLUDED | ${price:.2f} | {title[:60]}")
            elif too_old:
                _log(f"[DEBUG] SKIPPED (too old: {age_secs // 3600}h) | {title[:60]}")
            else:
                _log(f"[DEBUG] {'MATCH' if matched_filters else 'NO MATCH'} | ${price:.2f} | {title[:60]}")

        if collect_test_feed:
            if excluded:
                status = "🚫 Excluded"
            elif too_old:
                status = f"⏳ Too old ({age_secs // 3600}h)"
            elif matched_filters:
                status = "✅ Matched: " + ", ".join(f["name"] for f in matched_filters)
            else:
                status = "— No filter match"
            test_feed.append({
                "title": title,
                "price": price,
                "url": url,
                "age_secs": age_secs,
                "created_raw": item.get("itemCreationDate"),
                "status": status,
            })

    return new_count, excluded_count, matched_count, test_feed

def send_test_feed(entries):
    """Post every scanned listing (unfiltered) to the test channel with exact timing info."""
    if not TEST_DISCORD_WEBHOOK or not entries:
        return
    fields = []
    for e in entries:
        listed_str = "unknown"
        if e["created_raw"]:
            try:
                listed_dt = datetime.fromisoformat(e["created_raw"].replace("Z", "+00:00")).astimezone(SCANNER_TZ)
                listed_str = listed_dt.strftime("%Y-%m-%d %I:%M:%S %p %Z")
            except Exception:
                pass
        age_str = f"{e['age_secs']}s ago" if e["age_secs"] is not None else "unknown"
        price_str = f"${e['price']:.2f}" if e["price"] != NO_PRICE else "no price"
        value = f"Listed: {listed_str}\nSpotted: {age_str}\nStatus: {e['status']}"
        if e["url"]:
            value += f"\n[View listing]({e['url']})"
        fields.append({"name": f"{price_str} — {e['title'][:150]}", "value": value, "inline": False})

    for i in range(0, len(fields), 10):
        batch = fields[i:i + 10]
        _discord(
            {"embeds": [{"title": f"🧪 Unfiltered RAM feed — {len(batch)} listing(s)", "color": 0x99AAB5, "fields": batch}]},
            webhook_url=TEST_DISCORD_WEBHOOK,
        )

def _format_duration(secs):
    if secs is None:
        return "unknown"
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"{hours}h {mins}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"

def scan():
    token = None
    token_time = 0
    last_status = time.time()
    was_awake = None  # None = not yet determined, forces an initial log line

    while True:
        cycle_start = time.time()
        awake = is_awake()

        if awake != was_awake:
            if awake:
                _log("☀️ Awake hours started — scanner resuming.")
                if was_awake is not None:
                    _discord({"embeds": [{"title": "☀️ Scanner waking up", "color": 0xFFD700}]})
            else:
                _log("😴 Awake hours ended — scanner sleeping.")
                if was_awake is not None:
                    _discord({"embeds": [{"title": "😴 Scanner sleeping until next awake window", "color": 0x2C2F33}]})
            was_awake = awake

        if paused or not awake:
            time.sleep(SLEEP_CHECK_INTERVAL)
            continue

        if token is None or time.time() - token_time > TOKEN_TTL:
            try:
                token = get_access_token()
                token_time = time.time()
                _log("✅ eBay token acquired." if token_time == cycle_start else "🔄 eBay token refreshed.")
            except Exception as e:
                _log(f"⚠️ Token error: {e}")
                time.sleep(15)
                continue

        if time.time() - last_status >= STATUS_INTERVAL:
            _discord(build_status_embed())
            _log("📊 Hourly status posted.")
            last_status = time.time()

        with _config_lock:
            config = load_config()
        broadcast = config.get("broadcast", {})
        filters = config.get("filters", [])

        stats["last_scan_at"] = time.time()
        items, err = _fetch_broadcast(token, broadcast, filters)
        if err:
            _log(f"❌ Scan error: {err}")
        else:
            _update_market_tracking(items, filters)
            new_count, excluded_count, matched_count, test_feed = _process_results(items, filters, broadcast)
            if new_count:
                _log(
                    f"🔍 {new_count} new item(s) — {excluded_count} excluded, "
                    f"{matched_count} matched a filter (alert sent)"
                )
                _save_seen(SEEN_LISTINGS)
            send_test_feed(test_feed)

        poll_interval = broadcast.get("poll_interval", DEFAULT_POLL_INTERVAL)
        elapsed = time.time() - cycle_start
        time.sleep(max(0, poll_interval - elapsed))

# --- Discord bot ---
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)
search_group = app_commands.Group(name="search", description="Manage eBay result filters")
broadcast_group = app_commands.Group(name="broadcast", description="Manage the shared eBay broadcast search")

@search_group.command(name="list", description="List all active filters")
async def search_list(interaction: discord.Interaction):
    filters = load_config().get("filters", [])
    if not filters:
        await interaction.response.send_message("No filters configured.", ephemeral=True)
        return
    lines = []
    for i, f in enumerate(filters):
        must = ", ".join(f.get("must_contain", [])) or "any"
        lines.append(f"**{i+1}. {f['name']}**\nMax: ${f['max_price']} | Keywords: {must}\n")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@search_group.command(name="add", description="Add a new filter")
@app_commands.describe(
    name="Friendly name for this filter",
    max_price="Maximum price",
    must_contain="Comma-separated title keywords (any match required, optional)",
    label="Alert label text (optional)",
    color="Embed color as decimal integer (optional, default 49151)",
)
async def search_add(
    interaction: discord.Interaction,
    name: str,
    max_price: float,
    must_contain: str = "",
    label: str = "",
    color: int = 49151,
):
    with _config_lock:
        config = load_config()
        filters = config.setdefault("filters", [])
        if any(f["name"].lower() == name.lower() for f in filters):
            await interaction.response.send_message(f"A filter named **{name}** already exists.", ephemeral=True)
            return
        filters.append({
            "name": name,
            "max_price": max_price,
            "must_contain": [k.strip() for k in must_contain.split(",") if k.strip()],
            "label": label or name,
            "color": color,
        })
        save_config(config)

    _log(f"➕ Filter added: {name} (max ${max_price})")
    await interaction.response.send_message(f"✅ Filter **{name}** added.", ephemeral=True)

@search_group.command(name="remove", description="Remove a filter by name")
@app_commands.describe(name="Name of the filter to remove")
async def search_remove(interaction: discord.Interaction, name: str):
    with _config_lock:
        config = load_config()
        filters = config.get("filters", [])
        updated = [f for f in filters if f["name"].lower() != name.lower()]
        if len(updated) == len(filters):
            await interaction.response.send_message(f"No filter named **{name}** found.", ephemeral=True)
            return
        config["filters"] = updated
        save_config(config)

    _log(f"➖ Filter removed: {name}")
    await interaction.response.send_message(f"✅ Filter **{name}** removed.", ephemeral=True)

@search_group.command(name="edit", description="Edit an existing filter")
@app_commands.describe(
    name="Name of the filter to edit",
    new_name="New name (optional)",
    max_price="New maximum price (optional)",
    must_contain="New comma-separated keywords (optional)",
    label="New alert label text (optional)",
    color="New embed color as decimal integer (optional)",
)
async def search_edit(
    interaction: discord.Interaction,
    name: str,
    new_name: str = "",
    max_price: float = None,
    must_contain: str = "",
    label: str = "",
    color: int = None,
):
    with _config_lock:
        config = load_config()
        filters = config.get("filters", [])
        match = next((f for f in filters if f["name"].lower() == name.lower()), None)
        if not match:
            await interaction.response.send_message(f"No filter named **{name}** found.", ephemeral=True)
            return
        if new_name:
            match["name"] = new_name
        if max_price is not None:
            match["max_price"] = max_price
        if must_contain:
            match["must_contain"] = [k.strip() for k in must_contain.split(",") if k.strip()]
        if label:
            match["label"] = label
        if color is not None:
            match["color"] = color
        save_config(config)

    display = new_name or name
    _log(f"✏️ Filter edited: {name} → {display}")
    await interaction.response.send_message(f"✅ Filter **{display}** updated.", ephemeral=True)

@broadcast_group.command(name="show", description="Show the current broadcast search settings")
async def broadcast_show(interaction: discord.Interaction):
    b = load_config().get("broadcast", {})
    await interaction.response.send_message(
        f"**Query:** `{b.get('query', '(none)')}`\n"
        f"**Category:** {b.get('category_id') or 'all'}\n"
        f"**Poll interval:** every {b.get('poll_interval', DEFAULT_POLL_INTERVAL)}s",
        ephemeral=True,
    )

@broadcast_group.command(name="edit", description="Edit the shared broadcast search")
@app_commands.describe(
    query="New eBay search query (optional)",
    category_id="New eBay category ID, use 'none' to search all categories (optional)",
    poll_interval="New poll interval in seconds (optional, minimum 5)",
)
async def broadcast_edit(
    interaction: discord.Interaction,
    query: str = "",
    category_id: str = "",
    poll_interval: int = None,
):
    with _config_lock:
        config = load_config()
        b = config.setdefault("broadcast", {})
        if query:
            b["query"] = query
        if category_id:
            b["category_id"] = None if category_id.lower() == "none" else category_id
        if poll_interval is not None:
            b["poll_interval"] = max(5, poll_interval)
        save_config(config)

    _log(f"✏️ Broadcast search updated by {interaction.user}: {b}")
    await interaction.response.send_message("✅ Broadcast search updated.", ephemeral=True)

@tree.command(name="status", description="Get current scanner stats")
async def status_command(interaction: discord.Interaction):
    _discord(build_status_embed())
    await interaction.response.send_message("Status posted!", ephemeral=True)

@tree.command(name="echo", description="Echo")
async def echo_command(interaction: discord.Interaction):
    await interaction.response.send_message("echo")

@tree.command(name="test", description="Send a test ping and sample fake alerts to verify Discord delivery")
async def test_command(interaction: discord.Interaction):
    _discord({"content": "🧪 Test ping — if you can see this, the webhook works."})

    samples = [
        ("Crucial 32GB (2x16GB) DDR4-3200 Desktop RAM UDIMM", 59.99, "⚡ 2x16 DDR4", 49151),
        ("G.Skill Ripjaws V 64GB (2x32GB) DDR4-3600 Desktop Memory", 139.99, "🔥 2x32 DDR4", 16721920),
    ]
    for title, price, label, color in samples:
        _discord({
            "embeds": [{
                "title": f"[TEST] {title}",
                "url": "https://www.ebay.com/",
                "color": color,
                "fields": [
                    {"name": "Price",   "value": f"${price:.2f}", "inline": True},
                    {"name": "Search",  "value": label,           "inline": True},
                    {"name": "Item ID", "value": "`TEST-0000`",   "inline": True},
                    {"name": "Listed",  "value": "2m old",        "inline": True},
                ],
            }]
        })

    _log(f"🧪 Test ping + sample alerts sent by {interaction.user}")
    await interaction.response.send_message("✅ Sent a test ping and 2 sample alerts to the main channel.", ephemeral=True)

@tree.command(name="debug", description="Toggle debug logging of all scanned items to the logs channel")
async def debug_command(interaction: discord.Interaction):
    global debug_mode
    debug_mode = not debug_mode
    state = "🟡 ON" if debug_mode else "⚫ OFF"
    _log(f"🐛 Debug mode toggled {state} by {interaction.user}")
    await interaction.response.send_message(f"Debug mode is now **{state}**. Every scanned item will be logged.", ephemeral=True)

@tree.command(name="pause", description="Toggle the scanner on/off (no API calls while paused)")
async def pause_command(interaction: discord.Interaction):
    global paused
    paused = not paused
    state = "⏸️ PAUSED" if paused else "▶️ RUNNING"
    _log(f"{state} by {interaction.user}")
    await interaction.response.send_message(f"Scanner is now **{state}**.", ephemeral=True)

tree.add_command(search_group)
tree.add_command(broadcast_group)

@bot.event
async def on_ready():
    global _bot_loop, _bot_initialized
    _bot_loop = asyncio.get_event_loop()

    if _bot_initialized:
        print(f"[PID {os.getpid()}] on_ready fired again (reconnect) — skipping init")
        return
    _bot_initialized = True

    guild = discord.Object(id=DISCORD_GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    tree.clear_commands(guild=None)
    await tree.sync()
    print(f"[PID {os.getpid()}] Bot logged in as {bot.user}")
    threading.Thread(target=send_startup_message, daemon=True).start()

def run_bot():
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.start(DISCORD_BOT_TOKEN))
    except Exception as e:
        print(f"Bot error: {e}")

# --- eBay compliance endpoint ---
@app.route("/ebay-deletion", methods=["GET", "POST"])
def deletion():
    challenge = request.args.get("challenge_code")
    if challenge:
        m = hashlib.sha256()
        m.update(challenge.encode())
        m.update(VERIFICATION_TOKEN.encode())
        m.update(ENDPOINT.encode())
        return jsonify({"challengeResponse": m.hexdigest()}), 200
    return "", 200

# --- Market trends page ---
@app.route("/market-trends")
def market_trends():
    with _market_lock:
        log = list(reversed(SOLD_LOG))  # newest gone first

    durations = [e["duration_secs"] for e in log if e.get("duration_secs") is not None]
    avg_secs = int(sum(durations) / len(durations)) if durations else None

    config = load_config()
    price_cap = max((f["max_price"] for f in config.get("filters", [])), default=None)

    rows = []
    for e in log:
        price = e.get("price")
        price_str = f"${price:.2f}" if price is not None else "?"
        matched = html_escape(", ".join(e.get("matched_filters", [])) or "—")
        try:
            created_str = datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")).astimezone(SCANNER_TZ).strftime("%Y-%m-%d %I:%M %p")
        except Exception:
            created_str = "?"
        try:
            gone_str = datetime.fromisoformat(e["gone_at"]).astimezone(SCANNER_TZ).strftime("%Y-%m-%d %I:%M %p")
        except Exception:
            gone_str = "?"
        rows.append(
            "<tr>"
            f"<td>{html_escape(e.get('title', ''))}</td>"
            f"<td>{price_str}</td>"
            f"<td>{matched}</td>"
            f"<td>{created_str}</td>"
            f"<td>{gone_str}</td>"
            f"<td>{_format_duration(e.get('duration_secs'))}</td>"
            "</tr>"
        )

    table_rows = "\n".join(rows) or "<tr><td colspan=6>No data yet — check back after listings have cycled off eBay.</td></tr>"
    cap_note = f"under the ${price_cap:.0f} price cap" if price_cap else "under the tracked price cap"

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>RAM Market Trends</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 2rem; background: #111; color: #eee; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #444; padding: 6px 10px; text-align: left; font-size: 14px; }}
th {{ background: #222; }}
tr:nth-child(even) {{ background: #1a1a1a; }}
h1 {{ font-size: 1.4rem; }}
.note {{ color: #999; font-size: 0.85rem; max-width: 700px; }}
</style>
</head>
<body>
<h1>RAM Market Trends</h1>
<p class="note">
Tracks every listing that disappeared from active eBay search results in the scanned category,
{cap_note}. "Disappeared" is used as a proxy for sold/ended since the Browse API only exposes active
listings, not confirmed sale data — this can occasionally include expired, removed, or relisted items,
not just genuine sales. Data resets on every redeploy.
</p>
<p><b>{len(log)}</b> tracked so far. Average time to go: <b>{_format_duration(avg_secs)}</b></p>
<table>
<tr><th>Title</th><th>Price</th><th>Matched Filter</th><th>Listed At</th><th>Gone At</th><th>Time to Go</th></tr>
{table_rows}
</table>
</body>
</html>"""

# --- Startup (no blocking calls here) ---
threading.Thread(target=run_bot, daemon=True).start()
threading.Thread(target=scan, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
