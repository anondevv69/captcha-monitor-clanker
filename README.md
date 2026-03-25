# CAPTCHA Feed Alert Monitor

Monitors the [CAPTCHA](https://docs.captcha.social/) feed and sends alerts to
Discord and/or Telegram when a post contains:

- tracked keywords (for example: `clank`, `pump`, `ba3`)
- contract-like addresses (Ethereum `0x...` and Solana-style base58 addresses)

## Features

- Polls `GET /api/v1/feed` on an interval
- Uses bearer auth with your CAPTCHA API key
- De-duplicates posts via local state file
- Bootstrap mode to avoid alerting on historical posts
- Sends notifications to:
  - Discord webhook
  - Telegram bot chat

## Quick start

### 1) Requirements

- Python 3.10+ (standard library only; no external dependency required)

### 2) Configure environment

Create a `.env` file or export vars in your shell:

```bash
export CAPTCHA_API_KEY="captcha_live_xxx"
export CAPTCHA_BASE_URL="https://proficient-magpie-162.convex.site/"

# Comma-separated keywords (case-insensitive substring match)
export ALERT_KEYWORDS="clank,pump,ba3"

# Polling options
export POLL_INTERVAL_SECONDS="20"
export FEED_SORT="latest"     # latest | trending
export FEED_LIMIT="50"        # 1..50

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

## State file

The monitor writes seen post IDs to:

- `.captcha_monitor_state.json` (default)

Override path with:

```bash
export STATE_FILE="/path/to/state.json"
```

## Deploy on Railway (fast path)

This repo is ready to run as a Railway **worker** process.

### 1) Create Discord webhook (required for Discord alerts)

In Discord:

1. Open your server channel settings
2. Go to **Integrations** -> **Webhooks**
3. Create webhook for the target channel
4. Copy the webhook URL

You will use that URL for `DISCORD_WEBHOOK_URL` in Railway.

### 2) Deploy from GitHub

1. In Railway, click **New Project** -> **Deploy from GitHub Repo**
2. Select this repository/branch
3. Prefer Docker runtime for reliability (fixes `python3: command not found`):
   - Railway settings -> Build -> enable Dockerfile build
   - This repo includes `Dockerfile` with Python 3.12 preinstalled
4. Railway can also use:
   - `Procfile` (`worker: python3 monitor.py`)
   - `start.sh` fallback entrypoint (if Railpack expects a script)
   - `railway.json` (restart policy)

### 3) Set Railway variables

Use `env/railway.env.example` as your copy/paste source.

Minimum required for Discord-only alerts:

```env
CAPTCHA_API_KEY=your_captcha_api_key
CAPTCHA_BASE_URL=https://proficient-magpie-162.convex.site/
ALERT_KEYWORDS=clank,pump,ba3
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Recommended defaults:

```env
POLL_INTERVAL_SECONDS=20
FEED_SORT=latest
FEED_LIMIT=50
BOOTSTRAP_SKIP_EXISTING=true
DRY_RUN=false
RUN_ONCE=false
LOG_LEVEL=INFO
```

### 4) Turn it on

After variables are set, trigger a deploy/redeploy in Railway.
The worker should stay running and poll continuously.

### 5) Verify logs

You should see startup lines similar to:

- `Connected to CAPTCHA API as @...`
- `Starting monitor. sort=latest ...`

If the feed has a matching post (keyword or contract pattern), Discord should receive alerts.
