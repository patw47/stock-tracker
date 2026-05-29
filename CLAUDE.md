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

Auto-deploy on push to `main`, gated on green CI.

```
push main → CI green ──► deploy.yml (workflow_run)
                              │
                              ├─ gate: "Notify PR merge conflicts" green on same SHA
                              ├─ SSH queenp@VPS
                              │     git fetch + reset --hard origin/main   (queenp owns infra)
                              │     deploy/remote.sh:
                              │        systemctl stop stock-tracker
                              │        import_workflow.py  (sqlite upsert, queenp owns DB)
                              │        restart openclaw-warren → warren-server → stock-tracker
                              │        healthcheck services + n8n :5680/healthz + bridge :18795
                              └─ Telegram status (✅/❌ + PROJECT_NAME + per-service state)
```

**Roles** — `queenp` runs the infra (git, sqlite, systemctl via passwordless sudo);
`warren` is only the OpenClaw agent user (limited access).

**Workflow import** — n8n CLI `import:workflow` is blocked (`EACCES` on `.n8n/config`,
owned by `warren`). Instead `import_workflow.py` writes `workflow.json` straight into
`database.sqlite` (owned by `queenp`), upserts `veille-boursiere-001` as the single
active workflow, and deactivates any other (kills the legacy `48dff…`). n8n must be
stopped during import to avoid sqlite locks.

**Required GitHub secrets** — `VPS_SSH_HOST`, `VPS_SSH_USER`, `VPS_SSH_KEY` (SSH),
plus `TELEGRAM_ORCHESTRATION_BOT_TOKEN`, `TELEGRAM_ORCHESTRATION_CHAT_ID`,
`PROJECT_NAME` (status message).

---