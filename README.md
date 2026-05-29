# Stock Tracker — Daily AI-Powered Stock Briefing

Automated stock monitoring system built with **n8n** + **Claude Haiku** + **Warren** (OpenClaw agent).  
Every weekday morning it searches for fresh news on each ticker, filters out duplicates via per-ticker memory, synthesizes a French executive briefing, and delivers it via **Telegram** and **Gmail**.

---

## How it works

```
Schedule (12:00 Paris, Mon–Fri)
         │
         ▼
  Read 15 tickers (8 portfolio + 7 watchlist)
         │
         ▼  × 15 parallel calls
  Claude Haiku + web_search
  "Any news on TICKER from TODAY?"
  max 3 searches · 512 tokens
         │
         ▼
  Aggregate all raw news
         │
         ▼
  Warren Call 1 — ticker-watch  (POST /filter)
  Reads memory/tickers/SYMBOL.md (last 3 entries)
  Returns { new: [...], skip: [...] }
         │
         ▼ new tickers only
  Warren Call 2 — executive-synthesis  (POST /synthesize)
  French Markdown briefing
  Writes memory for each new ticker
         │
    ┌────┴────┐
    ▼         ▼
  Gmail     Telegram
```

**SKIP logic** — a ticker is skipped if:
- Haiku found no news from today
- Today's news is semantically identical to an existing memory entry (duplicate)

If no ticker is NEW → pipeline stops. No email, no message.

**Estimated cost**: ~$0.008/day (15 Haiku calls + 2 Warren calls)  
**Estimated runtime**: ~90 seconds

---

## Features

- **Date-filtered news** — Haiku searches for today's news only; stale articles are ignored
- **Duplicate memory** — Warren compares each ticker's news against the last 3 days of entries
- **Two-stage Warren** — Call 1 (filter) and Call 2 (synthesis) are separate for maintainability
- **Per-ticker memory** — `memory/tickers/SYMBOL.md`, 3 entries max, written only when content is genuinely new
- **Sector clustering** — SMR+OKLO, MMED+STIM+GRO, BBAI+VUZI+RGTI, HYLN+BLNK synthesized together
- **Auto-split Telegram** — messages split at 4,000 characters at paragraph boundaries
- **systemd managed** — three services: n8n, OpenClaw gateway, Warren HTTP bridge

---

## Stack

| Component | Role |
|---|---|
| **n8n** (self-hosted) | Scheduling, API calls, credential management, delivery |
| **Claude Haiku** (`claude-haiku-4-5-20251001`) | Per-ticker raw news search via `web_search` |
| **OpenClaw** | Agent framework wrapping Claude Haiku for Warren |
| **Warren** (OpenClaw agent) | Intelligence layer — filtering, memory, French synthesis |
| **warren_server.py** | Python HTTP bridge between n8n and OpenClaw CLI (port 18795) |

---

## Project structure

```
stock-tracker/
├── workflow.json          # n8n workflow — import this
├── warren_server.py       # Python HTTP bridge (port 18795)
├── .env                   # Environment variables (not versioned)
└── n8n-data/              # n8n database and config (not versioned)

/home/warren/.openclaw/workspace-warren/
├── BOOTSTRAP.md           # Warren system prompt / identity
├── SOUL.md                # Warren behavioral principles
├── PROMPT.md              # Skill modes documentation
├── ARCHITECTURE.md        # Pipeline documentation
├── TOOLS.md               # Credentials, paths, endpoints
├── skills/
│   ├── ticker-watch/      # Filter skill (NEW vs SKIP)
│   └── executive-synthesis/ # Synthesis skill (French briefing)
└── memory/
    └── tickers/           # SYMBOL.md — last 3 raw news entries
```

---

## VPS Installation (Ubuntu 22.04+)

### Prerequisites

- Ubuntu 22.04 VPS (2 GB RAM minimum)
- A dedicated system user for the app (recommended: `warren`)
- Your Anthropic API key, Gmail app password, Telegram bot token

---

### 1. Create system user

```bash
sudo useradd -m -s /bin/bash warren
sudo usermod -aG sudo warren  # optional — remove after setup if desired
```

---

### 2. Install Node.js

```bash
# As warren user (or any user that will run n8n)
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
source ~/.bashrc
nvm install 22
node -v   # v22.x.x
```

Or system-wide via NodeSource:

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
```

---

### 3. Install n8n

```bash
sudo npm install -g n8n
n8n --version
```

---

### 4. Install OpenClaw

OpenClaw is the agent framework that runs Warren. Install it globally:

```bash
sudo npm install -g openclaw
# or: npm install -g @openclaw/cli  (check current package name)
openclaw --version
```

Then configure OpenClaw for the `warren` user:

```bash
sudo -u warren openclaw init
```

This creates `/home/warren/.openclaw/` with the default config.

---

### 5. Clone the repository

```bash
sudo mkdir -p /opt/apps/stock-tracker
sudo chown warren:warren /opt/apps/stock-tracker
sudo -u warren git clone https://github.com/patw47/stock-tracker.git /opt/apps/stock-tracker
cd /opt/apps/stock-tracker
```

---

### 6. Configure environment variables

```bash
sudo -u warren cp .env.example .env
sudo -u warren nano .env
```

Fill in:

```env
ANTHROPIC_API_KEY=sk-ant-...
GMAIL_USER=your@gmail.com
GMAIL_PASS=xxxxxxxxxxxxxxxxxxxx    # 16-char App Password, NO spaces
TELEGRAM_TOKEN=123456789:ABC...
TELEGRAM_CHAT_ID=1234567890
N8N_PORT=5680
N8N_USER_FOLDER=/opt/apps/stock-tracker/n8n-data
N8N_BASIC_AUTH_ACTIVE=true
N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=your-password
GENERIC_TIMEZONE=Europe/Paris
N8N_DEFAULT_LOCALE=fr
N8N_ENCRYPTION_KEY=$(openssl rand -base64 32)
```

> **Gmail App Password**: Google Account → Security → 2-Step Verification → App passwords. Copy the 16-char password **without spaces**.  
> **Telegram**: create a bot via [@BotFather](https://t.me/BotFather), get your chat ID via [@userinfobot](https://t.me/userinfobot).

---

### 7. Set up Warren workspace

Copy the workspace files to the OpenClaw directory:

```bash
sudo cp -r /opt/apps/stock-tracker/workspace-warren \
  /home/warren/.openclaw/workspace-warren
sudo chown -R warren:warren /home/warren/.openclaw/workspace-warren
```

Create the memory directory:

```bash
sudo -u warren mkdir -p /home/warren/.openclaw/workspace-warren/memory/tickers
```

Register Warren as an OpenClaw agent:

```bash
sudo -u warren openclaw agent add warren \
  --workspace /home/warren/.openclaw/workspace-warren \
  --model claude-haiku-4-5-20251001
```

Verify:

```bash
sudo -u warren openclaw agent list   # warren should appear
```

---

### 8. Configure OpenClaw with your Anthropic key

```bash
sudo -u warren openclaw config set anthropic.apiKey sk-ant-...
# or edit /home/warren/.openclaw/openclaw.json directly
```

---

### 9. Set up systemd services

#### n8n service

```bash
sudo nano /etc/systemd/system/stock-tracker.service
```

```ini
[Unit]
Description=Stock Tracker — n8n
After=network.target

[Service]
Type=simple
User=warren
WorkingDirectory=/opt/apps/stock-tracker
EnvironmentFile=/opt/apps/stock-tracker/.env
ExecStart=/usr/bin/n8n start
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

#### OpenClaw gateway service

```bash
sudo nano /etc/systemd/system/openclaw-warren.service
```

```ini
[Unit]
Description=OpenClaw Warren Gateway
After=network.target

[Service]
Type=simple
User=warren
Environment=HOME=/home/warren
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/local/bin/openclaw gateway start --agent warren
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

#### Warren HTTP bridge service

```bash
sudo nano /etc/systemd/system/warren-server.service
```

```ini
[Unit]
Description=Warren HTTP Bridge — n8n to OpenClaw
After=network.target openclaw-warren.service
Wants=openclaw-warren.service

[Service]
Type=simple
User=warren
Environment=HOME=/home/warren
Environment=PATH=/usr/local/bin:/usr/bin:/bin
ExecStart=/usr/bin/python3 /opt/apps/stock-tracker/warren_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start all three:

```bash
sudo systemctl daemon-reload
sudo systemctl enable stock-tracker openclaw-warren warren-server
sudo systemctl start openclaw-warren warren-server
sleep 3
sudo systemctl start stock-tracker
```

Verify:

```bash
sudo systemctl status stock-tracker openclaw-warren warren-server
```

All three should show `active (running)`.

---

### 10. Import the workflow into n8n

```bash
sudo -u warren \
  N8N_USER_FOLDER=/opt/apps/stock-tracker/n8n-data \
  N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=false \
  n8n import:workflow --input=/opt/apps/stock-tracker/workflow.json
```

Activate the workflow via SQLite (n8n import deactivates by default):

```bash
sqlite3 /opt/apps/stock-tracker/n8n-data/.n8n/database.sqlite \
  "UPDATE workflow_entity SET active=1 WHERE id='veille-boursiere-001';"
```

Restart n8n to pick up the change:

```bash
sudo systemctl restart stock-tracker
```

---

### 11. Configure credentials in the n8n UI

Access the UI via SSH tunnel (n8n binds to localhost only):

```bash
ssh -L 5680:localhost:5680 warren@your-vps-ip -N
```

Then open `http://localhost:5680` in your browser.  
Login: the values you set in `.env` (`N8N_BASIC_AUTH_USER` / `N8N_BASIC_AUTH_PASSWORD`).

Go to **Credentials** and create:

| Name | Type | Settings |
|---|---|---|
| Header Auth account | HTTP Header Auth | Header: `x-api-key` · Value: your Anthropic key |
| Gmail SMTP | SMTP | Host: `smtp.gmail.com` · Port: `465` · SSL: on · user/pass from `.env` |
| Telegram account | Telegram API | Token from BotFather |

After creating credentials, check the node IDs in the workflow match. If they don't, open each affected node and reselect the credential from the dropdown.

---

### 12. Smoke test

Test the Warren HTTP bridge directly:

```bash
# Test filter endpoint
curl -s -X POST http://127.0.0.1:18795/filter \
  -H 'Content-Type: application/json' \
  -d '{"news":{"OKLO":"NO_NEWS_TODAY","SMR":"NuScale signs PPA with Azure today"}}' \
  | python3 -m json.tool

# Expected: {"new": ["SMR"], "skip": ["OKLO"], "reasons": {...}}
```

Test n8n is up:

```bash
curl -s http://localhost:5680/healthz
# Expected: {"status":"ok"}
```

---

## Continuous deployment (GitHub Actions → VPS)

Pushing to `main` auto-deploys to the VPS once CI is green.

```
push main → CI green ──► .github/workflows/deploy.yml
                              │
                              ├─ gate: "Notify PR merge conflicts" green on same commit
                              ├─ SSH queenp@VPS
                              │     git fetch + reset --hard origin/main
                              │     deploy/remote.sh:
                              │        stop stock-tracker  (release sqlite lock)
                              │        import_workflow.py  (upsert workflow into sqlite)
                              │        restart openclaw-warren → warren-server → stock-tracker
                              │        healthcheck: services + n8n :5680/healthz + bridge :18795
                              │        n8n execute (manual validation run, as warren)
                              └─ Telegram status message (success / failure + per-service state)
```

After restarting, the deploy runs the workflow **once** (`n8n execute`, as `warren` — the
only user able to read `.n8n/config` to decrypt credentials) so you receive the briefing in
Telegram and can confirm the run end-to-end. If there's no fresh news the SKIP logic sends
no briefing, but the deploy status message still arrives.

### Roles

- **`queenp`** — infra user. SSH target. Owns `database.sqlite`, has passwordless `sudo`.
  Runs git, the sqlite import, and `systemctl restart`.
- **`warren`** — OpenClaw agent user only, limited access. Not used by the deploy.

### Why a custom workflow importer

`n8n import:workflow` fails on this VPS — the n8n CLI run as `warren` hits `EACCES` on
`/opt/apps/stock-tracker/n8n-data/.n8n/config` (mode `600`, owned by `warren`). So
`deploy/import_workflow.py` (run as `queenp`, owner of `database.sqlite`) writes
`workflow.json` directly into the n8n database:

- upserts the workflow from `workflow.json` (`veille-boursiere-001`) with `active=1`
- deactivates every other workflow → enforces a **single active workflow** and removes
  the legacy `48dff814-…` ("Veille Boursière Quotidienne")
- introspects the schema (`PRAGMA table_info`) so it survives n8n version changes

n8n is stopped during the import to avoid sqlite lock contention, then all three
services are restarted.

### Required GitHub secrets

| Secret | Purpose |
|---|---|
| `VPS_SSH_HOST` | VPS IP/host (e.g. `77.42.72.164`) |
| `VPS_SSH_USER` | SSH user (`queenp`) |
| `VPS_SSH_KEY` | Private SSH key (full `-----BEGIN…END-----` block) |
| `TELEGRAM_ORCHESTRATION_BOT_TOKEN` | Bot token for the status message |
| `TELEGRAM_ORCHESTRATION_CHAT_ID` | Target chat for the status message |
| `PROJECT_NAME` | Project label shown in the status message |

### Manual run

The deploy auto-triggers after CI, but you can also re-run it from the **Actions** tab
(select the latest *Deploy to VPS* run → *Re-run jobs*), or run the steps by hand on the
VPS:

```bash
ssh queenp@<vps-ip>
git -C /opt/apps/stock-tracker pull
bash /opt/apps/stock-tracker/deploy/remote.sh
```

---

## Customizing tickers

Tickers are hardcoded in the **Read Tickers** node of the workflow. Edit them in the n8n UI:

1. Open the workflow
2. Click **Read Tickers**
3. Edit the `portfolio` and `watchlist` arrays in the JS code
4. Save

Each ticker needs: `symbol`, `sector`, `status` (`"portfolio"` or `"watchlist"`).

---

## Warren memory

Warren stores raw news per ticker in `/home/warren/.openclaw/workspace-warren/memory/tickers/`.

- One file per ticker: `SYMBOL.md`
- Max 3 entries, newest first, separated by `---`
- Written only when new content is confirmed (after synthesis)
- To reset a ticker's memory: `rm memory/tickers/SYMBOL.md`
- To reset all memory: `rm memory/tickers/*.md`

---

## Service management

```bash
# Status
sudo systemctl status stock-tracker warren-server openclaw-warren

# Restart all
sudo systemctl restart openclaw-warren warren-server stock-tracker

# Logs
sudo journalctl -u warren-server -f       # Python bridge logs
sudo journalctl -u stock-tracker -f       # n8n logs
sudo journalctl -u openclaw-warren -f     # OpenClaw logs
```

---

## Environment variables reference

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (used by Haiku calls in n8n) |
| `GMAIL_USER` | Gmail sender address |
| `GMAIL_PASS` | Gmail App Password — 16 chars, **no spaces** |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Target chat ID |
| `N8N_PORT` | n8n HTTP port (default: `5680`) |
| `N8N_USER_FOLDER` | n8n data directory |
| `N8N_BASIC_AUTH_USER` | n8n login username |
| `N8N_BASIC_AUTH_PASSWORD` | n8n login password |
| `N8N_ENCRYPTION_KEY` | Credentials encryption key (generate once, never change) |
| `GENERIC_TIMEZONE` | Timezone for scheduling (e.g. `Europe/Paris`) |

---

## Architecture decisions

See `/home/warren/.openclaw/workspace-warren/DECISIONS.md` for the full decision log.

Key choices:
- **Haiku for search** — cheaper, faster, sufficient for raw news retrieval (512 tokens vs 2048)
- **Two Warren calls** — filter and synthesis separated for testability and fail-safety
- **Memory = raw news** — storing Haiku output (not Warren synthesis) for stable duplicate comparison
- **warren_server.py** — Python bridge needed because n8n sandboxes `fs` and `child_process` modules
- **systemd over PM2** — PM2 not available on this VPS; systemd provides equivalent reliability
