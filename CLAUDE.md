# Stock Tracker Automation

Stock Tracker is an Automated stock monitoring system built with n8n + Claude Haiku + Warren (OpenClaw agent).
Every weekday morning it searches for fresh news on each ticker, filters out duplicates via per-ticker memory, synthesizes a French executive briefing, and delivers it via Telegram and Gmail.

## How it works
Schedule (08:00 Paris, Mon–Fri)
         │
         ▼
  Read 16 tickers (8 portfolio + 8 watchlist)
         │
         ▼  × 16 parallel calls
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

  SKIP logic — a ticker is skipped if:

Haiku found no news from today
Today's news is semantically identical to an existing memory entry (duplicate)
If no ticker is NEW → pipeline stops. No email, no message.

## Features

Date-filtered news — Haiku searches for today's news only; stale articles are ignored
Duplicate memory — Warren compares each ticker's news against the last 3 days of entries
Two-stage Warren — Call 1 (filter) and Call 2 (synthesis) are separate for maintainability
Per-ticker memory — memory/tickers/SYMBOL.md, 3 entries max, written only when content is genuinely new
Sector clustering — SMR+OKLO, MMED+STIM+GRO, BBAI+VUZI+RGTI, HYLN+BLNK synthesized together
Auto-split Telegram — messages split at 4,000 characters at paragraph boundaries
systemd managed — three services: n8n, OpenClaw gateway, Warren HTTP bridge


## Project structure

```
stock-tracker/
├── workflow.json          # n8n workflow — import this
├── warren_server.py       # Python HTTP bridge (port 18795)
├── deploy/                # CI/CD deployment
│   ├── remote.sh          # runs on the VPS: import + restart + healthcheck
│   └── import_workflow.py # direct-sqlite workflow import (bypasses n8n CLI)
├── .github/workflows/
│   └── deploy.yml         # GitHub Actions — auto-deploy to VPS after CI
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

## Deployment (CI/CD)

Auto-deploy on push to `main`, gated on green CI. The deploy job runs on a
**self-hosted GitHub Actions runner installed on the VPS** (`actions.runner.*` service,
user `queenp`) — GitHub cloud runners cannot SSH in (the VPS firewall blackholes their
return traffic; the handshake stalls to sshd's LoginGraceTime). Running on the VPS means
no SSH at all.

```
push main → CI green ──► deploy.yml (workflow_run, runs-on: self-hosted)
                              │
                              ├─ gate: "Notify PR merge conflicts" green on same SHA
                              ├─ git fetch (token-auth) + reset --hard origin/main
                              └─ deploy/remote.sh (local on VPS):
                                   pip install -r requirements.txt   (system python)
                                   stop stock-tracker
                                   import_workflow.py  (sqlite upsert, queenp owns DB)
                                   restart openclaw-warren → warren-server
                                   n8n execute --id=<wf>  (validation run, as warren, n8n stopped)
                                   start stock-tracker
                                   healthcheck services + n8n :5680/healthz + bridge :18795
                              → Telegram status (✅/❌ + PROJECT_NAME + per-service state)
```

**Roles** — `queenp` runs the infra (git, sqlite, systemctl via passwordless sudo, hosts
the runner); `warren` is only the OpenClaw agent user (limited; owns `.n8n/config`).

**Workflow import** — n8n CLI `import:workflow` is blocked (`EACCES` on `.n8n/config`,
ACL mask `---`). Instead `import_workflow.py` writes `workflow.json` straight into
`database.sqlite` (owned by `queenp`), upserts `veille-boursiere-001` as the single active
workflow, and deactivates any other (killed the legacy `48dff…`). n8n is stopped during
import to avoid sqlite locks.

**Validation run** — `remote.sh` runs `n8n execute --id` once **while the n8n service is
stopped** (so its task broker port 5679 is free) and **as `warren`** (only user that can
read `.n8n/config` to decrypt credentials). The workflow carries an **Execute Workflow
Trigger** node (besides the Schedule Trigger) because the CLI cannot start from a schedule
trigger. SKIP logic means no briefing is sent if there's no fresh news — expected; the
deploy status message still arrives.

**Python deps** — `warren_server.py` / `agents/warren` need `pydantic` + `requests`
(`requirements.txt`); `remote.sh` pip-installs them into the system python (the one
`warren-server.service` runs) before restarting.

**Required GitHub secrets** — `TELEGRAM_ORCHESTRATION_BOT_TOKEN`,
`TELEGRAM_ORCHESTRATION_CHAT_ID`, `PROJECT_NAME` (status message). No SSH secrets needed
(self-hosted runner). Runner uses the repo-scoped `GITHUB_TOKEN` for the git fetch.

---

## Watchlist & Portfolio Management via Telegram

Users manage their watched and held tickers directly from Telegram. Warren agent exposes two commands to add or remove stocks from the tracking lists.

**Commands**

- `/modifywatchlist` — Opens an inline button menu to add or remove tickers from the watchlist
- `/modifyportfolio` — Opens an inline button menu to add or remove tickers from the portfolio

**Conversation flow**

1. User sends `/modifywatchlist` or `/modifyportfolio`
2. Warren responds with two inline buttons: `➕ Add` and `➖ Remove`
3. User selects an action (add or remove)
4. Warren prompts `Type the ticker symbol (uppercase):` and waits for plaintext input
5. User sends ticker (e.g. `AAPL`)
6. Warren confirms: `✅ AAPL added to watchlist` or `✅ AAPL removed from watchlist`

Lists are immediately persisted after each action.

**Storage**

Watchlist and portfolio are stored as JSON files in `agents/warren/data/`:

- `watchlist.json` — array of uppercase ticker strings (e.g. `["AAPL", "MSFT", "GOOG"]`)
- `portfolio.json` — array of uppercase ticker strings

Each file contains only valid tickers that have been added and not yet removed. Files are overwritten on every modification to ensure consistency.

**Extending**

To add a new ticker list type:

1. Create a handler function in `agents/warren/telegram_list_handlers.py` following the `CommandHandler` pattern
2. Register the handler in `warren_server.py` by adding it to the bot command set in `set_my_commands()`
3. Store the list as a JSON file in `agents/warren/data/{name}.json`

---