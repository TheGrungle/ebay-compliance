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
MAX_SEEN = 5000
SCAN_INTERVAL = 30
TOKEN_TTL = 5400  # 90 minutes
STATUS_INTERVAL = 3600  # 60 minutes

# --- Env vars (fail fast if missing) ---
def _require(name):
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val

EBAY_APP_ID        = _require("EBAY_APP_ID")
EBAY_CERT_ID       = _require("EBAY_CERT_ID")
DISCORD_WEBHOOK    = _require("DISCORD_WEBHOOK")
DISCORD_BOT_TOKEN  = _require("DISCORD_BOT_TOKEN")
DISCORD_CHANNEL_ID = int(_require("DISCORD_CHANNEL_ID"))
DISCORD_LOG_CHANNEL_ID = int(_require("DISCORD_LOG_CHANNEL_ID"))

# --- Stats ---
stats = {
    "scans": 0,
    "items_found": 0,
    "alerts_sent": 0,
    "started_at": time.time(),
}

# --- Seen listings (persisted to disk) ---
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

# --- Discord webhook (alerts) ---
def _discord(payload: dict, retries: int = 3):
    for attempt in range(retries):
        try:
            r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
            if r.status_code == 429:
                wait = r.json().get("retry_after", 1)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return
        except Exception as e:
            print(f"Discord webhook error (attempt {attempt + 1}): {e}")
            time.sleep(2 ** attempt)

# --- Discord bot log channel ---
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
    _discord({
        "embeds": [{
            "title": "🟢 RAM Scanner is live",
            "description": "Scanning every 30 seconds for DDR4 deals.\n⚡ 2x16 ≤ $70\n🔥 2x32 ≤ $150",
            "color": 0x00FF00,
        }]
    })
    _log("🟢 Scanner started.")

def send_alert(title: str, price: float, url: str, tier: str):
    if tier == "fire":
        label, color = "🔥 2x32 DDR4 — $150 or under", 0xFF4500
    else:
        label, color = "⚡ 2x16 DDR4 — $70 or under", 0x00BFFF

    _discord({
        "embeds": [{
            "title": title,
            "url": url,
            "color": color,
            "fields": [
                {"name": "Price", "value": f"${price:.2f}", "inline": True},
                {"name": "Deal Tier", "value": label, "inline": True},
            ],
        }]
    })
    stats["alerts_sent"] += 1
    _log(f"🔔 Alert sent: {title} — ${price:.2f}")

def build_status_embed() -> dict:
    uptime = int(time.time() - stats["started_at"])
    hours, rem = divmod(uptime, 3600)
    minutes = rem // 60
    return {
        "embeds": [{
            "title": "📊 Scanner Status",
            "color": 0x5865F2,
            "fields": [
                {"name": "Uptime",      "value": f"{hours}h {minutes}m",   "inline": True},
                {"name": "Scans Run",   "value": str(stats["scans"]),       "inline": True},
                {"name": "Items Found", "value": str(stats["items_found"]), "inline": True},
                {"name": "Alerts Sent", "value": str(stats["alerts_sent"]), "inline": True},
            ],
        }]
    }

# --- Listing classification ---
_EXCLUSIONS = [
    "ecc", "server", "apple", "mac", "macbook", "rdimm", "lrdimm",
    "for parts", "parts only", "not working", "as is",
    "ddr5", "ddr3", "ddr2", "sodimm",
]
_KIT_16 = ["2x16", "2 x 16", "32gb kit", "32 gb kit", "dual 16", "16gbx2", "16gb x2"]
_KIT_32 = ["2x32", "2 x 32", "64gb kit", "64 gb kit", "dual 32", "32gbx2", "32gb x2"]

def _excluded(title: str) -> bool:
    return any(x in title.lower() for x in _EXCLUSIONS)

def _is_16gb_kit(title: str) -> bool:
    return any(x in title.lower() for x in _KIT_16)

def _is_32gb_kit(title: str) -> bool:
    return any(x in title.lower() for x in _KIT_32)

# --- Scanner loop ---
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

        try:
            r = requests.get(
                "https://api.ebay.com/buy/browse/v1/item_summary/search",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "q": "DDR4 RAM 2x16 OR 2x32 OR 32gb kit OR 64gb kit",
                    "category_ids": "170083",
                    "filter": "price:[..500],priceCurrency:USD,conditions:{NEW|USED_EXCELLENT|USED_GOOD|USED_ACCEPTABLE}",
                    "sort": "newlyListed",
                    "limit": "50",
                },
                timeout=15,
            )
            r.raise_for_status()

            stats["scans"] += 1
            dirty = False
            new_this_cycle = 0

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

                if _excluded(title):
                    continue

                if _is_16gb_kit(title) and price <= 500:
                    send_alert(title, price, url, "budget")
                elif _is_32gb_kit(title) and price <= 500:
                    send_alert(title, price, url, "fire")

            if new_this_cycle:
                _log(f"🔍 Scan #{stats['scans']}: {new_this_cycle} new item(s) found.")

            if dirty:
                _save_seen(SEEN_LISTINGS)

        except Exception as e:
            _log(f"❌ Scan error: {e}")

        time.sleep(SCAN_INTERVAL)

# --- Discord bot ---
intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

@tree.command(name="status", description="Get current RAM scanner stats")
async def status_command(interaction: discord.Interaction):
    _discord(build_status_embed())
    await interaction.response.send_message("Status posted!", ephemeral=True)

@bot.event
async def on_ready():
    global _bot_loop
    _bot_loop = asyncio.get_event_loop()
    await tree.sync()
    print(f"Bot logged in as {bot.user}")

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
