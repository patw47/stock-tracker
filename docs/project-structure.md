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


