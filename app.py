import asyncio
import base64
import hashlib
import json
import os
import threading
import time

import discord
from discord import app_commands
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- Constants ---
VERIFICATION_TOKEN = "qawfjoewjfoiewfsadfjjwqoifjewoifjoiwjfluhojflanfmdnugjwoiqjfnewfow"
ENDPOINT = "https://ebay-compliance-5902.onrender.com/ebay-deletion"
SEEN_FILE = "seen_listings.json"
SEARCHES_FILE = "searches.json"
MAX_SEEN = 5000
SCAN_INTERVAL = 30
TOKEN_TTL = 5400
STATUS_INTERVAL = 3600

EXCLUSIONS = [
    "ecc", "server", "apple", "mac", "macbook", "rdimm", "lrdimm",
    "for parts", "parts only", "not working", "as is",
    "ddr5", "ddr3", "ddr2", "sodimm",
]

# --- Env vars ---
def _require(name):
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val

EBAY_APP_ID            = _require("EBAY_APP_ID")
EBAY_CERT_ID           = _require("EBAY_CERT_ID")
DISCORD_WEBHOOK        = _require("DISCORD_WEBHOOK")
DISCORD_BOT_TOKEN      = _require("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID     = int(_require("DISCORD_CHANNEL_ID"))
DISCORD_LOG_CHANNEL_ID = int(_require("DISCORD_LOG_CHANNEL_ID"))
DISCORD_GUILD_ID       = int(_require("DISCORD_GUILD_ID"))

# --- Stats ---
stats = {
    "scans": 0,
    "items_found": 0,
    "alerts_sent": 0,
    "started_at": time.time(),
}

# --- Searches config ---
_searches_lock = threading.Lock()

def load_searches() -> list:
    try:
        with open(SEARCHES_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_searches(searches: list):
    with open(SEARCHES_FILE, "w") as f:
        json.dump(searches, f, indent=2)

# --- Seen listings ---
def _load_seen():
    try:
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def _save_seen(seen: set):
    items = list(seen)
    if len(items) > MAX_SEEN:
        items = items[-MAX_SEEN:]
    with open(SEEN_FILE, "w") as f:
        json.dump(items, f)

SEEN_LISTINGS: set = _load_seen()

# --- eBay auth ---
def get_access_token() -> str:
    creds = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    r = requests.post(
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
def _discord(payload: dict, retries: int = 3):
    for attempt in range(retries):
        try:
            r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
            if r.status_code == 429:
                time.sleep(r.json().get("retry_after", 1))
                continue
            r.raise_for_status()
            return
        except Exception as e:
            print(f"Webhook error (attempt {attempt + 1}): {e}")
            time.sleep(2 ** attempt)

# --- Bot log channel ---
_bot_loop: asyncio.AbstractEventLoop | None = None

def _log(message: str):
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

# --- Alerts ---
def send_startup_message():
    searches = load_searches()
    lines = "\n".join(f"• **{s['name']}** — max ${s['max_price']}" for s in searches) or "No searches configured."
    _discord({
        "embeds": [{
            "title": "🟢 RAM Scanner is live",
            "description": f"Scanning every 30 seconds.\n\n**Active searches:**\n{lines}",
            "color": 0x00FF00,
        }]
    })
    _log("🟢 Scanner started.")

def send_alert(title: str, price: float, url: str, search: dict):
    _discord({
        "embeds": [{
            "title": title,
            "url": url,
            "color": search.get("color", 0x00BFFF),
            "fields": [
                {"name": "Price",     "value": f"${price:.2f}",            "inline": True},
                {"name": "Deal Tier", "value": search.get("label", "Deal"), "inline": True},
                {"name": "Search",    "value": search["name"],              "inline": True},
            ],
        }]
    })
    stats["alerts_sent"] += 1
    _log(f"🔔 Alert: [{search['name']}] {title} — ${price:.2f}")

def build_status_embed() -> dict:
    uptime = int(time.time() - stats["started_at"])
    hours, rem = divmod(uptime, 3600)
    minutes = rem // 60
    searches = load_searches()
    return {
        "embeds": [{
            "title": "📊 Scanner Status",
            "color": 0x5865F2,
            "fields": [
                {"name": "Uptime",          "value": f"{hours}h {minutes}m",    "inline": True},
                {"name": "Scans Run",       "value": str(stats["scans"]),        "inline": True},
                {"name": "Items Found",     "value": str(stats["items_found"]),  "inline": True},
                {"name": "Alerts Sent",     "value": str(stats["alerts_sent"]),  "inline": True},
                {"name": "Active Searches", "value": str(len(searches)),         "inline": True},
            ],
        }]
    }

# --- Scanner ---
def _matches(title: str, search: dict) -> bool:
    t = title.lower()
    if any(x in t for x in EXCLUSIONS):
        return False
    must = search.get("must_contain", [])
    if must and not any(k.lower() in t for k in must):
        return False
    return True

def scan():
    global SEEN_LISTINGS

    try:
        token = get_access_token()
        _log("✅ eBay token acquired.")
    except Exception as e:
        _log(f"❌ Fatal: could not get initial eBay token: {e}")
        return

    token_time = time.time()
    last_status = time.time()

    while True:
        if time.time() - token_time > TOKEN_TTL:
            try:
                token = get_access_token()
                token_time = time.time()
                _log("🔄 eBay token refreshed.")
            except Exception as e:
                _log(f"⚠️ Token refresh failed: {e}")

        if time.time() - last_status >= STATUS_INTERVAL:
            _discord(build_status_embed())
            _log("📊 Hourly status posted.")
            last_status = time.time()

        with _searches_lock:
            searches = load_searches()

        stats["scans"] += 1
        dirty = False
        new_this_cycle = 0

        for search in searches:
            try:
                r = requests.get(
                    "https://api.ebay.com/buy/browse/v1/item_summary/search",
                    headers={"Authorization": f"Bearer {token}"},
                    params={
                        "q": search["query"],
                        "category_ids": search.get("category_id", "170083"),
                        "filter": f"price:[..{search['max_price']}],priceCurrency:USD,conditions:{{NEW|USED_EXCELLENT|USED_GOOD|USED_ACCEPTABLE}}",
                        "sort": "newlyListed",
                        "limit": "50",
                    },
                    timeout=15,
                )
                r.raise_for_status()

                for item in r.json().get("itemSummaries", []):
                    item_id = item.get("itemId")
                    if item_id in SEEN_LISTINGS:
                        continue

                    SEEN_LISTINGS.add(item_id)
                    stats["items_found"] += 1
                    new_this_cycle += 1
                    dirty = True

                    title = item.get("title", "")
                    price = float(item.get("price", {}).get("value", 999))
                    url   = item.get("itemWebUrl", "")

                    if _matches(title, search):
                        send_alert(title, price, url, search)

            except Exception as e:
                _log(f"❌ Scan error [{search['name']}]: {e}")

        if new_this_cycle:
            _log(f"🔍 Scan #{stats['scans']}: {new_this_cycle} new item(s) across {len(searches)} search(es).")

        if dirty:
            _save_seen(SEEN_LISTINGS)

        time.sleep(SCAN_INTERVAL)

# --- Discord bot ---
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

search_group = app_commands.Group(name="search", description="Manage eBay searches")

@search_group.command(name="list", description="List all active searches")
async def search_list(interaction: discord.Interaction):
    searches = load_searches()
    if not searches:
        await interaction.response.send_message("No searches configured.", ephemeral=True)
        return
    lines = []
    for i, s in enumerate(searches):
        must = ", ".join(s.get("must_contain", [])) or "any"
        lines.append(f"**{i+1}. {s['name']}**\nQuery: `{s['query']}`\nMax: ${s['max_price']} | Keywords: {must}\n")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@search_group.command(name="add", description="Add a new search")
@app_commands.describe(
    name="Friendly name for this search",
    query="eBay search query",
    max_price="Maximum price",
    must_contain="Comma-separated title keywords (any match required, optional)",
    label="Alert label text (optional)",
    color="Embed color as hex integer (optional, default 49151)",
)
async def search_add(
    interaction: discord.Interaction,
    name: str,
    query: str,
    max_price: float,
    must_contain: str = "",
    label: str = "",
    color: int = 49151,
):
    with _searches_lock:
        searches = load_searches()
        if any(s["name"].lower() == name.lower() for s in searches):
            await interaction.response.send_message(f"A search named **{name}** already exists.", ephemeral=True)
            return
        searches.append({
            "name": name,
            "query": query,
            "category_id": "170083",
            "max_price": max_price,
            "must_contain": [k.strip() for k in must_contain.split(",") if k.strip()],
            "label": label or name,
            "color": color,
        })
        save_searches(searches)

    _log(f"➕ Search added: {name} (max ${max_price})")
    await interaction.response.send_message(f"✅ Search **{name}** added.", ephemeral=True)

@search_group.command(name="remove", description="Remove a search by name")
@app_commands.describe(name="Name of the search to remove")
async def search_remove(interaction: discord.Interaction, name: str):
    with _searches_lock:
        searches = load_searches()
        updated = [s for s in searches if s["name"].lower() != name.lower()]
        if len(updated) == len(searches):
            await interaction.response.send_message(f"No search named **{name}** found.", ephemeral=True)
            return
        save_searches(updated)

    _log(f"➖ Search removed: {name}")
    await interaction.response.send_message(f"✅ Search **{name}** removed.", ephemeral=True)

@search_group.command(name="edit", description="Edit an existing search")
@app_commands.describe(
    name="Name of the search to edit",
    new_name="New name (optional)",
    query="New eBay search query (optional)",
    max_price="New maximum price (optional)",
    must_contain="New comma-separated keywords (optional)",
    label="New alert label text (optional)",
    color="New embed color as decimal integer (optional)",
)
async def search_edit(
    interaction: discord.Interaction,
    name: str,
    new_name: str = "",
    query: str = "",
    max_price: float = -1,
    must_contain: str = "",
    label: str = "",
    color: int = -1,
):
    with _searches_lock:
        searches = load_searches()
        match = next((s for s in searches if s["name"].lower() == name.lower()), None)
        if not match:
            await interaction.response.send_message(f"No search named **{name}** found.", ephemeral=True)
            return

        if new_name:
            match["name"] = new_name
        if query:
            match["query"] = query
        if max_price >= 0:
            match["max_price"] = max_price
        if must_contain:
            match["must_contain"] = [k.strip() for k in must_contain.split(",") if k.strip()]
        if label:
            match["label"] = label
        if color >= 0:
            match["color"] = color

        save_searches(searches)

    display = new_name or name
    _log(f"✏️ Search edited: {name} → {display}")
    await interaction.response.send_message(f"✅ Search **{display}** updated.", ephemeral=True)

@tree.command(name="status", description="Get current RAM scanner stats")
async def status_command(interaction: discord.Interaction):
    _discord(build_status_embed())
    await interaction.response.send_message("Status posted!", ephemeral=True)

@tree.command(name="echo", description="Echo")
async def echo_command(interaction: discord.Interaction):
    await interaction.response.send_message("echo")

tree.add_command(search_group)

@bot.event
async def on_ready():
    global _bot_loop
    _bot_loop = asyncio.get_event_loop()
    tree.clear_commands(guild=None)
    await tree.sync()
    guild = discord.Object(id=DISCORD_GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print(f"Bot logged in as {bot.user} — commands synced to guild")

def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(bot.start(DISCORD_BOT_TOKEN))

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

# --- Startup ---
threading.Thread(target=run_bot, daemon=True).start()
threading.Thread(target=scan, daemon=True).start()
send_startup_message()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
