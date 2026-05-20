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