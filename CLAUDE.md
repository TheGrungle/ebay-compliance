# eBay RAM Scanner — Project Context

## What This Is
A Python app hosted on Render that monitors eBay for DDR4 RAM deals and sends Discord alerts instantly when matching listings appear. The goal is to find deals faster than other buyers.

## Hosting
- **Platform:** Render (free tier, Python 3.12)
- **Repo:** https://github.com/TheGrungle/ebay-compliance
- **Start command:** `gunicorn app:app --bind 0.0.0.0:$PORT --log-file=- --log-level=debug`
- **Auto-deploys** on every push to `main`

## Architecture
Single file (`app.py`) running three concurrent threads:
1. **Flask** — handles eBay's marketplace account deletion compliance endpoint (`/ebay-deletion`)
2. **Scanner** — polls eBay Browse API every 30s (per-search configurable), sends Discord alerts via webhook
3. **Discord bot** — handles slash commands, posts to the logs channel

## Environment Variables (all required in Render)
| Variable | Description |
|---|---|
| `EBAY_APP_ID` | eBay developer app ID |
| `EBAY_CERT_ID` | eBay developer cert ID |
| `DISCORD_WEBHOOK` | Webhook URL for alert messages |
| `DISCORD_BOT_TOKEN` | Bot token from Discord Developer Portal |
| `DISCORD_CHANNEL_ID` | Main alerts channel ID |
| `DISCORD_LOG_CHANNEL_ID` | Logs channel ID (1503493724611547277) |
| `DISCORD_GUILD_ID` | Server ID (1445992804760293499) |
| `PYTHON_VERSION` | Must be `3.12.0` |

## Key Files
- `app.py` — entire application
- `searches.json` — search configs, read every 30s (no redeploy needed to change)
- `seen_listings.json` — persisted set of already-alerted item IDs (ephemeral on Render — wiped on redeploy)
- `requirements.txt` — flask, gunicorn==21.2.0, requests, discord.py
- `runtime.txt` — python-3.12.0 (Render may ignore this; use PYTHON_VERSION env var instead)
- `Procfile` — gunicorn start command with explicit port and logging

## Search Config (`searches.json`)
Each search object has:
```json
{
  "name": "Friendly name",
  "query": "eBay search query string",
  "category_id": "170083",
  "max_price": 70,
  "poll_interval": 30,
  "must_contain": ["keyword1", "keyword2"],
  "label": "⚡ Alert label",
  "color": 49151
}
```
- `poll_interval` must be a multiple of 30 (seconds)
- Changes to this file take effect within 30s without redeploying
- **Caveat:** edits made via Discord commands are wiped on redeploy. Push `searches.json` to GitHub to make defaults permanent.

## Discord Slash Commands
| Command | Description |
|---|---|
| `/status` | Posts scanner stats embed to main channel |
| `/echo` | Responds with "echo" — used to verify bot is alive |
| `/debug` | Toggles verbose scan logging to logs channel |
| `/search list` | Lists all active searches |
| `/search add` | Adds a new search |
| `/search remove` | Removes a search by name |
| `/search edit` | Edits any field of an existing search |

## Alert Embeds
Sent to the main channel via webhook. Include: title (linked to listing), price, deal tier label, search name, and listing age (e.g. "4m old") when available from eBay's API.

## Status Embed
Sent hourly and on `/status`. Includes: uptime, scans run, alerts sent, eBay API calls today vs 5,000 daily limit, projected 24h usage (turns orange with ⚠️ if on track to exceed limit), active search count.

## eBay API Notes
- Uses Browse API (`/buy/browse/v1/item_summary/search`)
- OAuth client credentials flow, token refreshed every 90 minutes
- Daily call limit: **5,000 calls/day**
- With 2 searches at 30s intervals = ~2,880 calls/day (safe)
- `fieldgroups=EXTENDED` is passed to get `itemCreationDate` for listing age

## Global Exclusions (hardcoded)
Listings are skipped if their title contains: `ecc`, `server`, `apple`, `mac`, `macbook`, `rdimm`, `lrdimm`, `for parts`, `parts only`, `not working`, `as is`, `ddr5`, `ddr3`, `ddr2`, `sodimm`

## Known Limitations
- `seen_listings.json` is wiped on every Render redeploy — causes a one-time flood of old listings on restart
- `searches.json` edits via Discord commands don't survive redeploys
- Fix for both: use a persistent database (Redis or SQLite with Render disk)
- Render free tier may spin down after inactivity

## Discord Server Info
- Server ID: 1445992804760293499
- Alerts channel: 1445992892056473610
- Logs channel: 1503493724611547277
- Bot application ID: 1503490922434793522
- Webhook: https://discord.com/api/webhooks/1503485957155061801/...
