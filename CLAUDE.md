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
2. **Scanner** — makes ONE broad eBay Browse API call per poll cycle (~15s), then runs every configured filter against the results in code. Sends Discord alerts via webhook.
3. **Discord bot** — handles slash commands, posts to the logs channel

### Why "broadcast + filter" instead of one API call per search
The old design ran a separate eBay API call per search config. Two searches at 30s = ~2,880 calls/day,
which capped how fast polling could go before hitting the 5,000/day limit. Now there's exactly **one**
broad query per cycle (e.g. `"DDR4 RAM"` in the RAM category), and all the specific matching (2x16 kit,
2x32 kit, price caps, keyword requirements) happens in code against that single result set. Adding more
filters costs zero extra API calls, which is what makes ~15s polling affordable.

## Awake-hours schedule
The scanner only polls eBay (and only counts against the daily API budget) during the hours below,
evaluated in the `SCANNER_TZ` timezone. Outside these hours it sleeps and checks the clock every 60s —
no API calls, no wasted budget.
- **Weekdays (Mon-Fri):** 7:00 - 23:00
- **Weekends (Sat-Sun):** 9:00 through 2:00 the following morning (i.e. Saturday 9am → Sunday 2am, Sunday 9am → Monday 2am)

Posts a "waking up" / "sleeping" embed to the **logs channel** (not the alerts channel) on each
transition, so it never triggers a phone/watch push notification — see "Notification design" below.
Manual `/pause` is independent of this schedule and overrides it either way.

**Budget check:** one call per 15s only during awake hours ≈ 3,840 calls/day on weekdays, ~4,080 on
weekends — both comfortably under the 5,000/day limit, with the two big API-cost drivers (redundant
per-search calls, always-on polling) both eliminated by this redesign.

## Environment Variables (all required in Render unless noted)
| Variable | Description |
|---|---|
| `EBAY_APP_ID` | eBay developer app ID |
| `EBAY_CERT_ID` | eBay developer cert ID |
| `DISCORD_WEBHOOK` | Webhook URL for alert messages |
| `DISCORD_BOT_TOKEN` | Bot token from Discord Developer Portal |
| `DISCORD_CHANNEL_ID` | Main alerts channel ID |
| `DISCORD_LOG_CHANNEL_ID` | Logs channel ID (1503493724611547277) |
| `DISCORD_GUILD_ID` | Server ID (1445992804760293499) |
| `SCANNER_TZ` | **IANA timezone for the awake-hours schedule (e.g. `America/New_York`, `America/Chicago`). Optional — defaults to `America/Chicago` if unset.** |
| `TEST_DISCORD_WEBHOOK` | Optional. Webhook URL for a second "test channel" that gets every scanned RAM listing — unfiltered, including excluded/non-matching ones — with exact listed time and seconds-since-listed. Unset = feature disabled. |
| `DISCORD_CRASH_LOG_CHANNEL_ID` | Optional. Channel ID for a one-line-per-scan-cycle firehose (via the bot) — purely so gaps in it show exactly when/how long the process was down. Unset = feature disabled. |
| `PYTHON_VERSION` | Must be `3.12.0` |

## Key Files
- `app.py` — entire application
- `searches.json` — broadcast query + filters, read every cycle (no redeploy needed to change)
- `seen_listings.json` — persisted set of already-alerted item IDs (ephemeral on Render — wiped on redeploy)
- `market_tracking.json` — item IDs currently active in the broadcast results, for sold/ended detection (ephemeral)
- `market_sold_log.json` — completed entries (title, price, listed/gone timestamps, duration), capped at 1,000, backs the `/market-trends` page (ephemeral)
- `runtime_state.json` — persisted eBay API call tally (`total`, `calls_today`, `day_start`) plus a `last_heartbeat` timestamp written every 60s regardless of awake/asleep/paused state. Survives Render free-tier spin-down/spin-up (same disk, same container resuming) — only wiped on an actual redeploy, same caveat as the other state files below. On boot, `last_heartbeat` is used to compute how long the process was actually offline, reported in the startup message.
- `requirements.txt` — flask, gunicorn==21.2.0, requests, discord.py, tzdata
- `runtime.txt` — python-3.12.0 (Render may ignore this; use PYTHON_VERSION env var instead)
- `Procfile` — gunicorn start command with explicit port and logging

## Config (`searches.json`)
```json
{
  "broadcast": {
    "query": "DDR4 RAM",
    "category_id": "170083",
    "poll_interval": 15
  },
  "filters": [
    {
      "name": "Friendly name",
      "max_price": 70,
      "must_contain": ["keyword1", "keyword2"],
      "exclude": ["optional per-filter exclusion"],
      "label": "⚡ Alert label",
      "color": 49151
    }
  ]
}
```
- **`broadcast`** is the single eBay API call made each cycle. `poll_interval` is in seconds (no longer
  constrained to multiples of 30 — minimum enforced is 5s). The price cap sent to eBay is computed
  automatically each cycle as the max of all filters' `max_price`, so you never have to keep it in sync
  by hand.
- **`filters`** are applied in code against every item from that one broadcast call — no additional API
  calls per filter. Each item is checked against every filter and can trigger multiple alerts if it
  matches more than one.
- Changes to this file take effect within one poll cycle without redeploying.
- **Caveat:** edits made via Discord commands are wiped on redeploy. Push `searches.json` to GitHub to make defaults permanent.

## Discord Slash Commands
| Command | Description |
|---|---|
| `/status` | Posts scanner stats embed to the logs channel (shows RUNNING / PAUSED / SLEEPING) |
| `/echo` | Responds with "echo" — used to verify bot is alive |
| `/test` | Sends a plain test ping plus 2 sample fake alerts (one 2x16, one 2x32, clearly marked `[TEST]`) to the main channel to verify webhook delivery and embed formatting |
| `/debug` | Toggles verbose scan logging to logs channel |
| `/pause` | Manually toggles the scanner on/off, independent of the awake-hours schedule |
| `/broadcast show` | Shows the current broadcast query/category/poll interval |
| `/broadcast edit` | Edits the broadcast query, category, or poll interval |
| `/search list` | Lists all active filters |
| `/search add` | Adds a new filter |
| `/search remove` | Removes a filter by name |
| `/search edit` | Edits any field of an existing filter |

## Alert Embeds
Sent to the main channel via webhook, fired on a background thread so a slow/rate-limited webhook never
blocks the scan loop. Include: title (linked to listing), price, deal tier label, filter name, and
listing age (e.g. "4m old") when available from eBay's API. The webhook payload also sets plain
`content` (`🔔 <title> — $<price>`) alongside the embed, since phone/watch push notification previews
generally show message content, not embed fields — this keeps title + price readable without opening
the app.

## Notification design
Only real finds (alert embeds above) are sent to the main alerts webhook/channel — that's the only
channel that should have push notifications enabled on your phone. Everything else (server startup,
Render free-tier restarts, awake/sleep transitions, `/status`) posts to the **logs channel**
(`DISCORD_LOG_CHANNEL_ID`) via the bot instead, using `_log_embed()` (mirrors `_discord()` but targets
the logs channel). Mute/silence notifications for the logs channel in Discord's per-channel settings —
no code change needed to adjust that. The old hourly automatic status ping was removed entirely (it
added noise without being actionable); `/status` still works on demand and now posts to the logs
channel.

**Per-cycle scan noise** used to log a line to the logs channel every cycle that had any new listing
(common, since the broadcast query is broad) even when nothing matched a filter. That's now lumped into
one brief line every `SCAN_SUMMARY_INTERVAL` (15 min): `"<scans> scans, <finds> found, <rate> calls/min"`
— `scans`/`finds`/the rate are all accumulated in `scan()`'s local `summary_*` counters and reset after
each flush. No per-cycle "0 found" logging happens anymore in the logs channel.

**Crash log channel** (`DISCORD_CRASH_LOG_CHANNEL_ID`, optional) gets one terse, emoji-free line per
scan cycle via `_crash_log()`: `"Scanned and found nothing"`, `"Scanned, found <N>"`, or
`"Scan failed: <error>"`. It's deliberately unaggregated and separate from the logs channel — the point
is a continuous trail so a gap in it (no lines for however long) shows exactly when the process went
down and for how long, without mixing that signal into the regular logs.

## Status Embed
Sent on `/status` only (see "Notification design" above — no longer automatic/hourly). Includes:
uptime, scans run, alerts sent, eBay API calls today vs 5,000 daily limit, projected 24h usage (turns
orange with ⚠️ if on track to exceed limit), poll interval, active filter count, and current state
(RUNNING / PAUSED / SLEEPING).

## eBay API Notes
- Uses Browse API (`/buy/browse/v1/item_summary/search`)
- OAuth client credentials flow, token refreshed every 90 minutes (only while awake)
- Daily call limit: **5,000 calls/day**
- One broadcast call per poll cycle regardless of filter count — see "Awake-hours schedule" above for the budget math
- `fieldgroups=EXTENDED` is passed to get `itemCreationDate` for listing age

## Test Channel Feed (`TEST_DISCORD_WEBHOOK`)
When set, every listing the scanner sees for the first time each cycle is posted to this second
webhook — regardless of exclusions or filter matches — with:
- Exact listed timestamp (from eBay's `itemCreationDate`, converted to `SCANNER_TZ`)
- Exact seconds elapsed since listing when spotted
- A status line: `🚫 Excluded`, `⏳ Too old`, `✅ Matched: <filter name(s)>`, or `— No filter match`

This reuses the same single broadcast API call as everything else — zero extra API cost. Messages are
batched in groups of 10 per Discord embed to stay under rate limits and embed field caps.

## Market Trends Page (`/market-trends`)
A read-only HTML page (no auth) showing every listing that dropped out of the active broadcast
results — used as a proxy for "sold or ended" — with how long it was up before that happened
("time to go"). Columns: title, price, matched filter, listed-at, gone-at, duration.

**Important caveat:** the eBay Browse API only exposes *active* listings, not confirmed sales data
(that requires eBay's Marketplace Insights API, which needs separate developer approval this app
doesn't have). "Gone from active results" can also mean an auction expired unsold, the seller removed
it, or it got relisted as a new item ID — not only a genuine sale. `BROADCAST_LIMIT` is set to 200 (the
Browse API max, same 1 API call either way) specifically to minimize the case where an active item
just falls off the page due to listing volume rather than actually ending.

## Global Exclusions (hardcoded)
Applied only when `broadcast.category_id` is `170083` (Desktop Memory). Listings are skipped if their
title contains: `ecc`, `server`, `apple`, `mac`, `macbook`, `rdimm`, `lrdimm`, `for parts`, `parts only`,
`not working`, `as is`, `ddr5`, `ddr3`, `ddr2`, `sodimm`

## Known Limitations
- `seen_listings.json` is wiped on every Render redeploy — causes a one-time flood of old listings on restart
- `market_tracking.json` / `market_sold_log.json` are also wiped on redeploy — trend history resets
- `runtime_state.json` (API call tally + heartbeat) is likewise wiped on redeploy — persists across free-tier spin-down/spin-up, resets on an actual code push
- `searches.json` edits via Discord commands don't survive redeploys
- Fix for all of the above: use a persistent database (Redis or SQLite with Render disk)
- Render free tier may spin down after inactivity — the startup message reports how long it was down and whether the awake-hours schedule says it should actually be scanning right now (a restart does not imply "running"; see "Notification design")
- `SCANNER_TZ` must be set correctly or the awake-hours schedule runs at the wrong clock hours for your location
- "Sold" on the market trends page is really "disappeared from active results" — see the caveat in that section above

## Discord Server Info
- Server ID: 1445992804760293499
- Alerts channel: 1445992892056473610
- Logs channel: 1503493724611547277
- Bot application ID: 1503490922434793522
- Webhook: https://discord.com/api/webhooks/1503485957155061801/...
