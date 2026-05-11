import base64
import hashlib
import json
import os
import threading
import time

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

# --- Env vars (fail fast if missing) ---
def _require(name):
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val

EBAY_APP_ID     = _require("EBAY_APP_ID")
EBAY_CERT_ID    = _require("EBAY_CERT_ID")
DISCORD_WEBHOOK = _require("DISCORD_WEBHOOK")

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

# --- Discord ---
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
            print(f"Discord error (attempt {attempt + 1}): {e}")
            time.sleep(2 ** attempt)

def send_startup_message():
    _discord({
        "embeds": [{
            "title": "🟢 RAM Scanner is live",
            "description": "Scanning every 30 seconds for DDR4 deals.\n⚡ 2x16 ≤ $70\n🔥 2x32 ≤ $150",
            "color": 0x00FF00,
        }]
    })

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

# --- Listing classification ---
_EXCLUSIONS = [
    "ecc", "server", "apple", "mac", "macbook", "rdimm", "lrdimm",
    "for parts", "parts only", "not working", "as is",
    "ddr5", "ddr3", "ddr2", "sodimm",
]
_KIT_16 = ["2x16", "2 x 16", "32gb kit", "32 gb kit", "dual 16", "16gbx2", "16gb x2"]
_KIT_32 = ["2x32", "2 x 32", "64gb kit", "64 gb kit", "dual 32", "32gbx2", "32gb x2"]

def _excluded(title: str) -> bool:
    t = title.lower()
    return any(x in t for x in _EXCLUSIONS)

def _is_16gb_kit(title: str) -> bool:
    t = title.lower()
    return any(x in t for x in _KIT_16)

def _is_32gb_kit(title: str) -> bool:
    t = title.lower()
    return any(x in t for x in _KIT_32)

# --- Scanner loop ---
def scan():
    global SEEN_LISTINGS

    try:
        token = get_access_token()
    except Exception as e:
        print(f"Fatal: could not get initial eBay token: {e}")
        return

    token_time = time.time()

    while True:
        if time.time() - token_time > TOKEN_TTL:
            try:
                token = get_access_token()
                token_time = time.time()
            except Exception as e:
                print(f"Token refresh failed: {e}")

        try:
            r = requests.get(
                "https://api.ebay.com/buy/browse/v1/item_summary/search",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "q": "DDR4 RAM 2x16 OR 2x32 OR 32gb kit OR 64gb kit",
                    "category_ids": "170083",
                    "filter": "price:[..150],priceCurrency:USD,conditions:{NEW|USED_EXCELLENT|USED_GOOD|USED_ACCEPTABLE}",
                    "sort": "newlyListed",
                    "limit": "50",
                },
                timeout=15,
            )
            r.raise_for_status()

            dirty = False
            for item in r.json().get("itemSummaries", []):
                item_id = item.get("itemId")
                if item_id in SEEN_LISTINGS:
                    continue

                SEEN_LISTINGS.add(item_id)
                dirty = True

                title = item.get("title", "")
                price = float(item.get("price", {}).get("value", 999))
                url   = item.get("itemWebUrl", "")

                if _excluded(title):
                    continue

                if _is_16gb_kit(title) and price <= 70:
                    send_alert(title, price, url, "budget")
                elif _is_32gb_kit(title) and price <= 150:
                    send_alert(title, price, url, "fire")

            if dirty:
                _save_seen(SEEN_LISTINGS)

        except Exception as e:
            print(f"Scan error: {e}")

        time.sleep(SCAN_INTERVAL)

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

# --- Startup (runs for both Gunicorn and direct execution) ---
threading.Thread(target=scan, daemon=True).start()
send_startup_message()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
