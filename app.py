import hashlib
import os
import time
import requests
from flask import Flask, request, jsonify
import threading
import base64

app = Flask(__name__)

VERIFICATION_TOKEN = "qawfjoewjfoiewfsadfjjwqoifjewoifjoiwjfluhojflanfmdnugjwoiqjfnewfow"
ENDPOINT = "https://ebay-compliance-5902.onrender.com/ebay-deletion"

EBAY_APP_ID = os.environ.get("EBAY_APP_ID")
EBAY_CERT_ID = os.environ.get("EBAY_CERT_ID")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK")

SEEN_LISTINGS = set()

def send_startup_message():
    requests.post(DISCORD_WEBHOOK, json={
        "embeds": [{
            "title": "🟢 RAM Scanner is live",
            "description": "Scanning every 30 seconds for DDR4 deals.\n⚡ 2x16 ≤ $70\n🔥 2x32 ≤ $150",
            "color": 0x00FF00
        }]
    })

def get_access_token():
    credentials = base64.b64encode(f"{EBAY_APP_ID}:{EBAY_CERT_ID}".encode()).decode()
    response = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data="grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope"
    )
    return response.json().get("access_token")

def send_discord_alert(title, price, url, tier):
    if tier == "fire":
        label = "🔥 2x32 DDR4 — $150 or under"
    else:
        label = "⚡ 2x16 DDR4 — $70 or under"

    requests.post(DISCORD_WEBHOOK, json={
        "embeds": [{
            "title": title,
            "url": url,
            "color": 0xFF4500 if tier == "fire" else 0x00BFFF,
            "fields": [
                {"name": "Price", "value": f"${price}", "inline": True},
                {"name": "Deal Tier", "value": label, "inline": True}
            ]
        }]
    })

def is_excluded(title):
    title_lower = title.lower()
    exclusions = [
        "ecc", "server", "apple", "mac", "macbook", "rdimm", "lrdimm",
        "for parts", "parts only", "not working", "as is", "ddr5",
        "ddr3", "ddr2", "sodimm" 
    ]
    return any(term in title_lower for term in exclusions)

def is_16gb_kit(title):
    title_lower = title.lower()
    triggers = ["2x16", "2 x 16", "32gb kit", "32 gb kit", "dual 16", "16gbx2", "16gb x2"]
    return any(t in title_lower for t in triggers)

def is_32gb_kit(title):
    title_lower = title.lower()
    triggers = ["2x32", "2 x 32", "64gb kit", "64 gb kit", "dual 32", "32gbx2", "32gb x2"]
    return any(t in title_lower for t in triggers)

def scan():
    token = get_access_token()
    token_time = time.time()

    while True:
        # Refresh token every 90 minutes
        if time.time() - token_time > 5400:
            token = get_access_token()
            token_time = time.time()

        try:
            response = requests.get(
                "https://api.ebay.com/buy/browse/v1/item_summary/search",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "q": "DDR4 RAM 2x16 OR 2x32 OR 32gb kit OR 64gb kit",
                    "category_ids": "170083",
                    "filter": "price:[..150],priceCurrency:USD,conditions:{NEW|USED_EXCELLENT|USED_GOOD|USED_ACCEPTABLE}",
                    "sort": "newlyListed",
                    "limit": "50"
                }
            )

            items = response.json().get("itemSummaries", [])

            for item in items:
                item_id = item.get("itemId")
                title = item.get("title", "")
                price = float(item.get("price", {}).get("value", 999))
                url = item.get("itemWebUrl", "")

                if item_id in SEEN_LISTINGS:
                    continue

                SEEN_LISTINGS.add(item_id)

                if is_excluded(title):
                    continue

                if is_16gb_kit(title) and price <= 70:
                    send_discord_alert(title, price, url, "budget")

                elif is_32gb_kit(title) and price <= 150:
                    send_discord_alert(title, price, url, "fire")

        except Exception as e:
            print(f"Scan error: {e}")

        time.sleep(30)

# Compliance endpoint
@app.route('/ebay-deletion', methods=['GET', 'POST'])
def deletion():
    challenge = request.args.get('challenge_code')
    if challenge:
        m = hashlib.sha256()
        m.update(challenge.encode('utf-8'))
        m.update(VERIFICATION_TOKEN.encode('utf-8'))
        m.update(ENDPOINT.encode('utf-8'))
        return jsonify({"challengeResponse": m.hexdigest()}), 200
    return '', 200

if __name__ == '__main__':
    def start_scanner():
        scanner_thread = threading.Thread(target=scan, daemon=True)
        scanner_thread.start()
    
    # This runs whether started by Gunicorn or directly
    start_scanner()
    
    if __name__ == '__main__':
        app.run(host='0.0.0.0', port=10000)
