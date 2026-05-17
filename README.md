# Stock Tracker — Daily AI-Powered Stock Briefing Agent

Automated stock monitoring agent built with **n8n** + **Claude AI** (Anthropic).  
Every weekday morning, it runs a web-search-backed analysis on each ticker and delivers a full briefing via **Telegram** and **Gmail**.

---

## Features

- **Portfolio vs watchlist logic** — different prompts depending on whether you hold the stock or just monitor it
- **Mandatory web search** — Claude uses `web_search` before any decision; SKIP only allowed after a real search
- **Auto-split Telegram messages** — long briefings split automatically at 4,000 characters
- **Email digest** — one Gmail recap with a Portfolio section and a Watchlist section
- **Scheduled daily** — runs at 10:00 AM Paris time (Europe/Paris), Monday–Friday
- **PM2 managed** — auto-restart on crash and on server reboot

---

## How it works

```
Schedule (10:00 Paris) → Read tickers → Build Claude request → Claude API (web_search)
                                                                       ↓
                                                              Extract briefing
                                                              ├── Aggregate Email → Gmail
                                                              └── Aggregate Telegram → Split → Telegram
```

Estimated runtime: **4–6 minutes** (15 tickers × ~20s per Claude call with web_search).

---

## Customizing your tickers

Edit `portfolio.json` for stocks you **own**, and `watchlist.json` for stocks you **monitor**.

**`portfolio.json`**
```json
{
  "tickers": [
    { "symbol": "AAPL", "name": "Apple Inc.", "sector": "Technology" },
    { "symbol": "TSLA", "name": "Tesla Inc.", "sector": "EV / Energy" }
  ],
  "updated_at": "2026-05-17"
}
```

**`watchlist.json`**
```json
{
  "tickers": [
    { "symbol": "NVDA", "name": "NVIDIA Corporation", "sector": "Semiconductors" },
    { "symbol": "PLTR", "name": "Palantir Technologies", "sector": "AI / Defense" }
  ],
  "updated_at": "2026-05-17"
}
```

Rules:
- `symbol` must be a valid US stock ticker (e.g. `AAPL`, `TSLA`)
- `name` and `sector` are used in the Claude prompt — be descriptive for better analysis
- No limit on number of tickers, but each adds ~20s to the daily run
- After editing, no restart needed — files are read at each workflow execution

---

## Project structure

```
stock-tracker/
├── workflow.json          # n8n workflow (import directly)
├── ecosystem.config.js    # PM2 config (port, timezone, data folder)
├── start.sh               # Manual start script (alternative to PM2)
├── portfolio.json         # Stocks you own
├── watchlist.json         # Stocks you monitor
├── .env                   # Environment variables (not versioned)
└── .n8n/                  # n8n database (not versioned)
```

---

## Installation on a VPS (Ubuntu 22.04+)

### 1. Connect to your VPS

```bash
ssh user@your-vps-ip
```

### 2. Install Node.js via nvm

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
source ~/.bashrc
nvm install 22
node -v  # should print v22.x.x
```

### 3. Install n8n and PM2

```bash
npm install -g n8n pm2
```

### 4. Clone the repository

```bash
git clone https://github.com/patw47/stock-tracker.git
cd stock-tracker
```

### 5. Configure environment variables

Create your `.env` file:

```bash
cp .env.example .env
nano .env
```

Fill in:

```env
ANTHROPIC_API_KEY=sk-ant-...
GMAIL_USER=your@gmail.com
GMAIL_PASS=xxxx xxxx xxxx xxxx   # Gmail App Password (not your account password)
TELEGRAM_TOKEN=123456789:ABC...
TELEGRAM_CHAT_ID=1234567890
```

> **Gmail App Password**: go to Google Account → Security → 2-Step Verification → App passwords.  
> **Telegram**: create a bot via [@BotFather](https://t.me/BotFather), then get your chat ID via [@userinfobot](https://t.me/userinfobot).

### 6. Update ecosystem.config.js with your username

Edit the `N8N_USER_FOLDER` path in `ecosystem.config.js`:

```js
N8N_USER_FOLDER: '/home/YOUR_USERNAME/stock-tracker',
```

### 7. Start with PM2

```bash
pm2 start ecosystem.config.js
pm2 save
```

Enable auto-start on server reboot (run once, requires sudo):

```bash
pm2 startup
# copy-paste the command it prints, then run:
pm2 save
```

### 8. Import the workflow

```bash
N8N_USER_FOLDER=/home/YOUR_USERNAME/stock-tracker \
  n8n import:workflow --input=workflow.json
```

### 9. Open the n8n UI

Navigate to `http://your-vps-ip:5680` in your browser.  
Login: `admin` / `stockwatcher2026`

### 10. Configure credentials in the n8n UI

Go to **Credentials** and create:

| Credential | Type | Details |
|---|---|---|
| Anthropic API | HTTP Header Auth | Header: `x-api-key`, Value: your key |
| Gmail SMTP | SMTP | Host: `smtp.gmail.com`, Port: `465`, SSL: on |
| Telegram Bot | HTTP Request | Token from BotFather |

### 11. Activate the workflow

In the n8n UI, open the **Veille Boursière Quotidienne** workflow and toggle it **Active** (top right).

---

## PM2 reference

```bash
pm2 status                  # check running processes
pm2 logs stock-tracker      # view live logs
pm2 restart stock-tracker   # restart after config changes
pm2 stop stock-tracker      # stop
```

---

## Environment variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `GMAIL_USER` | Sender Gmail address |
| `GMAIL_PASS` | Gmail App Password |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Target Telegram chat ID |

---

## PM2 / n8n config (`ecosystem.config.js`)

| Variable | Value |
|---|---|
| `N8N_PORT` | `5680` |
| `N8N_USER_FOLDER` | `/home/<user>/stock-tracker` |
| `N8N_BASIC_AUTH_USER` | `admin` |
| `GENERIC_TIMEZONE` | `Europe/Paris` |

---

## Model

`claude-sonnet-4-5` with `web_search_20250305` (Anthropic beta tool) — max 5 searches per ticker, 2,048 output tokens per analysis.
