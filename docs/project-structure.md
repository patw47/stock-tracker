## Project structure

```
stock-tracker/
├── workflow.json              # n8n workflow — Layer A (news) + Layer B (EOD anomaly) wiring
├── warren_server.py           # Python HTTP bridge n8n → OpenClaw (port 18795)
├── portfolio.json             # 8 portfolio tickers — source of truth (not versioned; see *.example.json)
├── watchlist.json             # 8 watchlist tickers — source of truth (not versioned)
├── requirements.txt           # pydantic, requests, anthropic, numpy, pandas, yfinance, pyarrow
├── agents/
│   └── warren/
│       ├── prompt_builder.py  # Warren persona + output format (briefing template, n8n skill rules)
│       ├── macro_provider.py  # MacroContext (FRED) + MacroSnapshot (web search geopolitics/Fed/dollar)
│       ├── models.py          # MacroContext / MacroSnapshot / UpcomingEvent
│       └── manage_tickers.py  # add/remove tickers in the JSON files (Telegram skills backend)
├── market_intelligence/       # Layer B — EOD anomaly detection (no LLM in the critical path)
│   ├── fetch_eod.py           # S0 — OHLCV batch (yfinance primary, Twelve Data fallback)
│   ├── normalize_quality.py   # S0 — clean series + short-history flag
│   ├── registry_schema.py     # S0 — ticker registry (symbol, api_symbol, expected_name)
│   ├── symbol_validator.py    # S0 — identity check + quarantine
│   ├── anomaly_signals.py     # S1 — returns, MAD z-score, RVOL, gap, ATR, 52-week breakout
│   ├── beta_gate.py           # S2 — market-model regression (IWM + sector ETF), z_resid
│   ├── candidate_alerts.py    # S3 — thresholds calm/speculative, signal combination, direction
│   ├── short_interest.py      # S4 — Yahoo short data → squeeze-prone flag
│   ├── dedup_hysteresis.py    # S5 — latch/re-arm state machine per ticker (file-persisted)
│   ├── macro_snapshot.py      # S6 — ^TNX/IWM/OIL/^VIX/DXY snapshot, computed once, cached
│   ├── edgar_form4.py         # S7 — SEC EDGAR Form 4 structured fetch
│   ├── web_research.py        # S7 — product news (yf.Ticker.news) + sector ETF news
│   ├── market_status.py       # S7 — halt status (FINRA) + SSR status (Nasdaq), process-cached
│   ├── warren_alert_research.py # S7 — AlertResearchContext + targeted Warren prompt/call
│   ├── eod_orchestrator.py    # S8 — chains S0→S7, prints JSON {survivor_count, should_send, digest}
│   └── data/                  # registry.json, quarantine.json, alert/dedup/short thresholds,
│                              #   sector_factors.json (IWM + sector ETF mapping)
├── skills/                    # OpenClaw skill sources (synced to the Warren workspace)
│   ├── modifyportfolio/
│   └── modifywatchlist/
├── tests/                     # pytest — agents/warren, market_intelligence, workflow wiring
├── docs/                      # this file, deployement.md, ticker-files-schema.md
├── deploy/                    # CI/CD: remote.sh (runs on VPS) + import_workflow.py (sqlite upsert)
├── .github/workflows/         # ci.yml, deploy.yml (self-hosted runner), notion-sync, PR conflicts
├── .env                       # Environment variables (not versioned)
└── n8n-data/                  # n8n database and config (not versioned)

/home/warren/.openclaw/workspace-warren/        (on the VPS — Warren agent definition)
├── PROMPT.md                  # Skill modes (ticker-watch JSON / executive-synthesis briefing)
├── SOUL.md / IDENTITY.md / AGENTS.md / TOOLS.md / HEARTBEAT.md
├── ARCHITECTURE.md            # Pipeline documentation (two layers)
├── skills/
│   ├── ticker-watch/          # Filter skill (NEW vs SKIP)
│   ├── executive-synthesis/   # Synthesis skill (French briefing, signal-first, no # headings)
│   ├── modifyportfolio/       # Telegram portfolio management
│   └── modifywatchlist/       # Telegram watchlist management
└── memory/
    └── tickers/               # SYMBOL.md — last 3 raw news entries (read by both layers)
```

Mirror copies of the workspace files are kept in the Obsidian vault under
`Agents/OpenClaw/warren/` (injected into agent context via `Memory/stock-tracker/context-map.md`).
