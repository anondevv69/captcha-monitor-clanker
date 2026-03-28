# CAPTCHA Feed Alert Monitor

Monitors the [CAPTCHA](https://docs.captcha.social/) feed and sends alerts to
Discord and/or Telegram when a post contains:

- tracked keywords (for example: `clank`, `pump`, `ba3`)
- contract-like addresses (Ethereum `0x...` and Solana-style base58 addresses)

## Features

- Polls `GET /api/v1/feed` on an interval (optional multi-page pagination)
- On startup logs `GET /api/v1/me` and `GET /api/v1/me/balance` (micro-USDC / USD)
- Uses bearer auth with your CAPTCHA API key (`Authorization: Bearer captcha_live_…`)
- De-duplicates posts via local state file
- Bootstrap mode to avoid alerting on historical posts
- Sends notifications to:
  - Discord webhook
  - Telegram bot chat

## Quick start

### 1) Requirements

- Python 3.10+
- Install deps: `pip install -r requirements.txt` (**`curl_cffi`** impersonates a real browser
  TLS fingerprint/JA3). Plain `urllib` or `requests` + headers alone often still get
  Cloudflare **1010** (`browser_signature_banned`) from cloud IPs. Optional: set
  `CURL_IMPERSONATE` (default `chrome124`). Do not set `CAPTCHA_HTTP_USER_AGENT` unless you
  know you need it — a custom UA can conflict with the impersonated browser profile.

### 2) Configure environment

Create a `.env` file or export vars in your shell:

```bash
export CAPTCHA_API_KEY="captcha_live_xxx"   # never commit real keys; rotate if leaked
export CAPTCHA_BASE_URL="https://api.captcha.social"

# Comma-separated keywords (case-insensitive substring match)
export ALERT_KEYWORDS="clank,pump,ba3"

# Polling options
export POLL_INTERVAL_SECONDS="20"
export FEED_SORT="trending"   # trending | latest
export FEED_LIMIT="50"        # 1..50
export FEED_MAX_PAGES="5"     # feed pages per poll (cursor pagination)

# Skip sending alerts for currently visible feed on first run
export BOOTSTRAP_SKIP_EXISTING="true"

# At least one notification channel is required unless DRY_RUN=true
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export TELEGRAM_BOT_TOKEN="123456:ABCDEF..."
export TELEGRAM_CHAT_ID="-1001234567890"

# Helpful for testing
export DRY_RUN="false"        # true prints alerts instead of sending
export RUN_ONCE="false"       # true executes one poll and exits
```

### 3) Run

One-shot test:

```bash
RUN_ONCE=true DRY_RUN=true python3 monitor.py
```

Continuous monitoring:

```bash
python3 monitor.py
```

## How matching works

- Keywords: case-insensitive substring check
- ETH contract pattern: `0x` + 40 hex chars
- Solana-like pattern: base58 string length 32-44

If either keywords or addresses match, an alert is sent.

## CAPTCHA API (this monitor)

Full reference: [docs.captcha.social](https://docs.captcha.social). This process is **read-only**
(feed + profile + balance). Creating posts and some other actions cost USDC; always check
`GET /api/v1/me/balance` before any write in your own tooling.

- **Auth:** `Authorization: Bearer <API key>` on every request.
- **Money:** amounts are **micro-USDC** (6 decimals): `1000000` = \$1.00.
- **Time:** timestamps are Unix **milliseconds**.
- **Errors:** JSON body `{ "error": "…", "code": "…" }` (e.g. rate limits **429**).

Alerts include links to the web app and `GET /api/v1/posts/:id` on the API host.

## State file

The monitor writes seen post IDs to:

- `.captcha_monitor_state.json` (default)

Override path with:

```bash
export STATE_FILE="/path/to/state.json"
```
