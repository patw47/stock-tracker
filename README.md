# Stock Tracker — Market Context Brief & Anomaly Alerts

Automated stock monitoring system built with **n8n** + **Claude Haiku** + **Warren** (OpenClaw agent) + a **Python anomaly-detection layer** (`market_intelligence/`).

Three independent layers, one Telegram channel:

| Layer | What | When | LLM? |
|---|---|---|---|
| **Macro Brief** | Daily market context brief — Fed, rates, dollar, oil, VIX, small caps, sectors, geopolitics | 16:00 Paris, Mon–Fri | Haiku (web search) + Warren (prose) |
| **A — News (silent)** | Web news per ticker → dedup → memory write only, no message | 16:00 Paris, Mon–Fri | Haiku (search) + Warren (filter) |
| **B — Anomaly detection (EOD)** | Price/volume anomaly scan → beta gate → dedup → targeted Warren explanation | 21:30 UTC, Mon–Fri (after US close) | **Zero LLM in the detection path** — Warren only called for surviving alerts |

The Macro Brief answers *"what is the market doing today?"*. Layer A silently maintains the news memory that feeds Layer B context. Layer B answers *"when"* (something unusual just happened on this stock).

---

## Macro Brief — Daily Market Context

```
Macro Brief Schedule 16h Mon-Fri (Europe/Paris)
         │
         ▼
  Call Warren Macro Brief — POST /macro-brief
  (warren_server.py — handle_macro_brief)
         │
         ├─ FRED API (get_snapshot)
         │   Fed Funds, 10Y/2Y rates, VIX, dollar index, S&P 500
         │
         ├─ yfinance (get_market_closes)
         │   IWM close + daily change, WTI crude close + daily change
         │
         ├─ Haiku web_search (fetch_macro_snapshot)
         │   Fed stance, rate expectations/rumors, geopolitics,
         │   notable IPOs, hot sectors, Fear & Greed, market rumors
         │
         ├─ sector_rotation.py (get_sector_rotation) — zero LLM
         │   Sector ETF relative performance, IWM/SPY ratio,
         │   small caps appetite trend
         │
         └─ fear_greed.py (get_fear_greed) — zero LLM
             CNN Fear & Greed index (best-effort)
         │
         ▼
  Warren — prose brief 150–300 words
  Signal-first: opens with dominant regime (risk-on / risk-off / neutral)
  Max 5 numeric values — Fed, rates, VIX, IWM move, Fear & Greed
  No bullet points, no tables, no # headings, no bold headers
  Rumors always labeled as rumors
  Closes with one-sentence regime conclusion
         │
         ▼
  Extract Macro Brief
         │
         ▼
  Aggregate for Telegram → Split for Telegram → Send Telegram
```

**Graceful degradation** — if one source fails, the brief continues with the remaining data:
- Web search fails → quantitative FRED data only, brief mentions qualitative uncertainty
- All FRED sources fail → fallback values + warning
- Brief is never empty, never crashes

**Estimated cost**: ~$0.02/day (Haiku macro web search + Warren brief call)

---

## Layer A — Silent News Collection

```
Layer A News Schedule 16h Mon-Fri (Europe/Paris)
         │
         ▼
  Read Tickers — reads portfolio.json + watchlist.json (16 tickers: 8 + 8)
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
  Call Warren Filter — ticker-watch  (POST /filter)
  Reads memory/tickers/SYMBOL.md (last 3 entries)
  Returns { new: [...], skip: [...] }
         │
         ▼
  Extract Filter Result → If New Items
         │
         ▼ NEW tickers only
  Call Memorize — POST /memorize (handle_memorize)
  Writes memory/tickers/SYMBOL.md for each NEW ticker
  No Warren synthesis call — no Telegram message
```

**SKIP logic** — a ticker is skipped if:
- Haiku found no news from today
- Today's news is semantically identical to an existing memory entry (duplicate)

If no ticker is NEW → pipeline stops silently. No message is sent.

**Purpose** — Layer A feeds `memory/tickers/SYMBOL.md`, which Warren reads when researching anomaly alerts (Layer B step S7). Suppressing the synthesis/Telegram node removes noise while preserving the memory pipeline.

**Note on `/synthesize`** — `warren_server.py` still exposes `POST /synthesize` (legacy endpoint, present for backward compatibility). It is no longer called by the n8n workflow.

---

## Layer B — EOD anomaly pipeline

```
Layer B EOD Schedule (21:30 UTC, Mon–Fri — DST-safe, always ≥ 30 min after US close)
         │
         ▼
  python3 -m market_intelligence.eod_orchestrator --history-days 280
         │   S0 fetch EOD OHLCV (yfinance, Twelve Data fallback) + registry/quarantine
         │   S1 anomaly signals (z-score MAD, RVOL, gap, ATR, 52-week breakout)
         │   S2 beta gate (market-model regression vs IWM + sector ETF)
         │   S3 candidate alerts (calm |z|>2.0 / speculative |z|>2.5)
         │   S4 short interest → squeeze-prone flag
         │   S5 dedup (hysteresis latch per ticker)
         │   S6 macro snapshot (computed once, cached, attached to every alert)
         │   S7 Warren targeted research per surviving alert:
         │      EDGAR Form 4 (structured) · product/sector news (yfinance)
         │      halt status (FINRA) · SSR status (Nasdaq) · squeeze flag
         │      Layer A news memory for the ticker · macro snapshot
         │      → explicitly allowed to answer "no identifiable catalyst"
         ▼
  JSON result { survivor_count, should_send, digest, data_issues,
                candidates_detail, dry_run, run_id, pending_state_path }
         │
         ├─ one line appended to runtime/market_intelligence/runs.jsonl
         │  (every candidate with its exact non-alert reason — the run explains itself)
         │
         ▼ if survivors
  One Telegram digest (HTML-safe, reuses Aggregate / Split / Send Telegram nodes)
         │
         ▼ after confirmed delivery (or immediately when no survivor)
  Commit Dedup State — the pending dedup state is only promoted once Telegram
  delivery is confirmed (two-phase commit). A failed send leaves the state
  untouched and the alert re-fires at the next run.
```

No alert survives → nothing is sent (but the run is journaled, the Friday
heartbeat proves the pipeline is alive, and an external watchdog would have
alerted if the run had not happened at all).

---

## 🔍 Anomaly detection to anticipate a potential move

### The problem we are solving

On speculative small caps, **price often moves before the news becomes public**
(rumor, accumulation, squeeze). A pipeline that only reads news therefore arrives
late. Layer B does not predict anything: it detects that **an unusual move has
just happened**, then asks Warren to investigate. It is an attention detector,
not a crystal ball.

### Step 1 — "Is this move unusual for THIS ticker?" (the z-score)

A stock moving 8% in one day is huge for Xylem (a quiet water company) and
ordinary for Rigetti (a volatile quantum stock). A fixed percentage threshold
would therefore make no sense. Instead, the day's move is compared with **the
ticker's own behavior** over the last 60 days:

> **z-score = "how many times larger than usual?"**
> z = 1 → normal day. z = 2 → big, rare day. z = 3 → exceptional.

The z-score "normalizes" each ticker by its own volatility.

Robustness detail: "usual behavior" is measured with the **median** (MAD) rather
than the mean, so that a few past extreme sessions do not distort the reference.

### Step 2 — "Is the ticker moving, or is the whole market moving?" (the beta gate)

If the whole small-cap market drops 3%, your speculative ticker may drop 5%
without any company-specific news. Alerting on that would be noise. The image:
**the tide vs. the wave**. We want to detect the wave (the ticker's own move),
not the tide (the whole market's move).

In practice, the system learns the ticker's market sensitivity over 60 days
(its "beta": *when the IWM small-cap index moves 1%, this ticker moves 2% on
average*). On the day of the move, it calculates the **expected** move given what
the market did, subtracts it from the real move, and keeps the **residual**: the
part of the move the market does not explain. The z-score from step 1 is applied
to this residual.

For strongly thematic stocks (nuclear, quantum, water...), the system also
removes the part explained by the sector ETF (NUKZ for SMR/OKLO, QTUM for
RGTI...), but only if the ticker actually follows that ETF (correlation > 0.35);
otherwise it would only introduce noise.

> A price alert = **|residual z| > 2** (calmer names: XYL, MMED)
> or **> 2.5** (speculative cluster: RGTI, BBAI, OKLO...).
> One global setting; per-ticker normalization does the rest.

### Step 3 — The other sensors

- **RVOL (relative volume)**: today's volume ÷ 20-day average volume. Volume
  ×3 with no news = someone may know something, or a squeeze may be starting.
  This is the #1 early signal on small caps.
- **ATR expansion**: the day's range (high-low) exceeds 1.5× the usual range →
  "the ticker is waking up", even if it closes flat.
- **52-week breakout**: new yearly high or low.
- **Combination**: alert if price is abnormal, OR if abnormal volume + a second
  signal confirms it.

### Step 4 — Avoid crying wolf twice (hysteresis)

A stock that takes off often remains volatile for several days. Without a
guardrail, the same alert would arrive every evening. The mechanism works like a
**thermostat**: once the alert fires, the ticker is "locked" and does not
re-alert **until it has calmed down again** (|z| < 1 for at least one day),
unless something genuinely new happens: direction reversal, a new signal type,
or a clear escalation. Safety valve: after about 10 trading days, the lock
expires.

### Step 5 — Warren investigates (and is allowed to find nothing)

Only surviving alerts cost an LLM call. Warren receives a structured dossier:
insider buying/selling (SEC EDGAR Form 4), product and sector news, trading
suspension status (FINRA), short-sale restriction status (Nasdaq), a
"squeeze-prone ticker" flag (short interest), Layer A's news memory for that
ticker, and the day's macro context. Warren must explain the move **without
making things up**: the prompt explicitly allows it to conclude *"no identifiable
catalyst — flow/technical/squeeze likely"*.

### What anomaly detection does not do

- It does not predict direction (a volume spike says "look", not "it goes up").
- It does not trade. A high false-positive rate is accepted: this is an
  attention tool, strictly better than a daily news scan, not a robot.

Configuration: thresholds in `market_intelligence/data/alert_thresholds.json`,
sector mapping in `data/sector_factors.json`, hysteresis in
`data/dedup_thresholds.json`, squeeze in `data/short_interest_thresholds.json`.

---

## 🛡️ Reliability & self-measurement (Epics 1–5, July 2026)

After post-mortem PM-0001 (19 days of silence: the n8n workflow version was
never published and `executeCommand` was banned by default in n8n 2.20), five
epics turned the demo into a system:

1. **Transactional dedup state** — any non-official run is dry-run by default
   (`--dry-run` / `ANOMALY_DEDUP_READONLY=1`, zero side effects); the official
   run writes a candidate state (`dedup_state.pending.json` + `run_id`) that
   n8n only promotes after confirmed Telegram delivery. Failed send → the alert
   re-fires. Admin tool: `python3 -m market_intelligence.dedup_admin
   show|reset|commit`.
2. **Observability** — append-only run journal (`runs.jsonl`, one line per run
   with the exact reason each candidate did not alert), an EOD **watchdog
   outside n8n** (systemd timer, 22:15 UTC — if n8n dies, the watchdog
   survives, and alerts through the raw Telegram API), and a Friday heartbeat
   so weekly silence is bounded by a positive signal. Zero LLM in the whole
   chain.
3. **Safe delivery** — LLM prose is untrusted input: everything sent to
   Telegram is HTML-escaped producer-side (`parse_mode=HTML`), the 4,000-char
   splitter never cuts a tag in half, the Warren bridge is a
   `ThreadingHTTPServer` with a memory-write lock, and `Send Telegram` retries
   ×3 with the final failure kept visible (no `continueOnFail`).
4. **Unified ticker referential** — `python3 -m
   market_intelligence.registry_check` is blocking in CI and at deploy time: a
   ticker added via Telegram can no longer be silently invisible to detection.
   New tickers are onboarded with safe defaults (`speculative` classification,
   `single_factor_symbols`, symbol validated before any write); the sector-ETF
   choice remains an explicit human decision in PR.
5. **Track record** — J+1/J+5/J+20 outcomes are measured for sent alerts **and
   for gated candidates** (`outcome_tracker`, systemd timer 22:30 UTC), a
   monthly Telegram report aggregates them (with an honest "sample too small"
   guard), a no-look-ahead backtest (`python3 -m market_intelligence.backtest
   --start … --end …`) replays the deterministic pipeline over years of
   history to calibrate thresholds, and Warren's alert prompt includes its own
   past analyses for the ticker paired with the real outcomes (self-critique
   loop, no confabulation when an outcome is unavailable).

---

## Skills — on-demand Telegram commands

| Skill | Trigger | What |
|---|---|---|
| **tickerbrief** | `brief TICKER`, `point sur TICKER`, `actu TICKER` | On-demand brief: today's news + ticker memory + EOD anomaly state + sector. Read-only. |
| **modifyportfolio** | `/modifyportfolio` | Add/remove tickers from portfolio.json interactively |
| **modifywatchlist** | `/modifywatchlist` | Add/remove tickers from watchlist.json interactively |

**tickerbrief** assembles: memory/tickers/SYMBOL.md, fresh web search news, dedup_state.json anomaly state, sector_factors.json. Read-only — never writes files. Returns a signal-first Telegram reply (anomaly status first, then news, then memory). For tickers not in portfolio/watchlist, returns raw web search only.

**modifyportfolio / modifywatchlist** — send `/modifyportfolio` (or `/modifywatchlist`) to Warren in Telegram:

1. Warren replies with **[➕ Add a ticker] [➖ Remove a ticker]** inline buttons.
2. **Add** → type the symbol(s), e.g. `NVDA, MSFT` → file updated with `added` date + ✅ confirmation.
3. **Remove** → current tickers shown as toggle buttons → select → **✅ Validate**.

Never hand-edit `portfolio.json` / `watchlist.json` — the flow goes through `agents/warren/manage_tickers.py` (atomic write, dedupe, date stamp).

---

## Features

- **Macro Brief** — daily market regime brief sent at 16h Paris even with no ticker news
- **Two independent layers** — silent news collection (A) + EOD anomaly trigger (B), one Telegram channel
- **Zero LLM in the detection path** — Layer B is pure Python/statistics until an alert survives
- **Date-filtered news** — Haiku searches for today's news only
- **Duplicate memory** — Warren compares each ticker's news against the last 3 entries; only new content is written
- **Layer A ↔ B cross-reference** — Warren's anomaly research includes the ticker's news memory
- **Sector rotation signal** — `sector_rotation.py` computes sector ETF relative performance + IWM/SPY ratio (zero LLM)
- **Fear & Greed** — `fear_greed.py` fetches CNN Fear & Greed index (best-effort, zero LLM)
- **On-demand ticker brief** — `tickerbrief` skill via Telegram: full ticker context without triggering a digest
- **Signal-first briefing** — dominant market regime on the first line of the macro brief
- **Tickers as data** — `portfolio.json` / `watchlist.json` are the source of truth, editable via Telegram (`modifyportfolio` / `modifywatchlist` skills)
- **Symbol integrity** — registry + quarantine (`market_intelligence/data/`) so analysis never runs on a wrong ticker
- **Auto-split Telegram** — messages split at 4,000 characters at paragraph boundaries, HTML-safe
- **Transactional dedup state** — dry-run by default, two-phase commit after confirmed delivery
- **Run journal + external watchdog + weekly heartbeat** — silence is informative, not ambiguous
- **Referential consistency enforced** — `registry_check` blocking in CI and at deploy
- **Self-measured signal** — J+1/J+5/J+20 outcomes, monthly report, no-look-ahead backtest, Warren self-critique
- **systemd managed** — n8n, OpenClaw gateway, Warren HTTP bridge + watchdog/outcome timers
- **CI/CD** — push to `main` → 507 tests → auto-deploy on the VPS via self-hosted runner (workflow version published, referential validated)

---

## Stack

| Component | Role |
|---|---|
| **n8n** (self-hosted) | Scheduling, API calls, credential management, delivery |
| **Claude Haiku** (`claude-haiku-4-5-20251001`) | Per-ticker raw news search via `web_search` + macro web search |
| **OpenClaw** | Agent framework wrapping Claude for Warren |
| **Warren** (OpenClaw agent) | Intelligence layer — filtering, memory, Macro Brief prose, alert explanations |
| **warren_server.py** | Python HTTP bridge between n8n and OpenClaw CLI (port 18795) |
| **agents/warren/** | Prompt builder (persona, output format) + macro providers (FRED, web search, market closes) |
| **market_intelligence/** | Layer B — EOD fetch, anomaly signals, beta gate, dedup, EDGAR, short interest, orchestrator |
| **market_intelligence/sector_rotation.py** | Sector ETF relative performance + IWM/SPY ratio — zero LLM, feeds Macro Brief |
| **market_intelligence/fear_greed.py** | CNN Fear & Greed index fetch — zero LLM, feeds Macro Brief |

---

## Project structure

```
stock-tracker/
├── workflow.json              # n8n workflow (Macro Brief + Layer A silent + Layer B wiring)
├── warren_server.py           # Python HTTP bridge (port 18795): /filter /memorize /macro-brief /synthesize(legacy)
├── portfolio.json             # 8 portfolio tickers (source of truth)
├── watchlist.json             # 8 watchlist tickers (source of truth)
├── requirements.txt           # Python deps (pydantic, requests, anthropic, numpy, pandas, yfinance, pyarrow)
├── agents/warren/             # Prompt builder, macro providers (FRED, web search, market closes), ticker management
├── market_intelligence/       # Layer B anomaly detection (S0–S7) + reliability & measurement tooling
│   ├── eod_orchestrator.py    # Pipeline chief: dry-run flags, run journal, HTML-safe digest
│   ├── dedup_hysteresis.py    # Hysteresis latch + suppression reasons + pending state (two-phase)
│   ├── dedup_admin.py         # State admin CLI: show / reset / commit
│   ├── registry_check.py      # Referential consistency validator (blocking in CI + deploy)
│   ├── ticker_onboard.py      # Safe-default onboarding of a new ticker
│   ├── outcome_tracker.py     # J+1/J+5/J+20 outcomes for alerts and gated candidates
│   ├── monthly_report.py      # Monthly track-record Telegram report
│   ├── backtest.py            # No-look-ahead backtest & threshold calibration harness
│   ├── weekly_heartbeat.py    # Friday proof-of-life message (zero LLM)
│   ├── sector_rotation.py     # Sector ETF rotation + IWM/SPY ratio (zero LLM)
│   ├── fear_greed.py          # CNN Fear & Greed index (zero LLM)
│   └── data/                  # registry, quarantine, thresholds, sector factors
├── skills/                    # OpenClaw skills sources
│   ├── macrobrief/            # Macro Brief skill spec (SKILL.md)
│   ├── tickerbrief/           # On-demand ticker brief skill spec (SKILL.md)
│   ├── modifyportfolio/       # Portfolio management via Telegram (with Layer B onboarding)
│   └── modifywatchlist/       # Watchlist management via Telegram (with Layer B onboarding)
├── tests/                     # pytest suite — 507 tests (agents, market_intelligence, deploy, workflow wiring)
├── docs/                      # project-structure, deployment, backtest guide, ticker schema
├── deploy/                    # CI/CD: remote.sh, import_workflow.py (version publish),
│                              # watchdog_eod.py + systemd units/timers (watchdog, outcome tracker)
└── .github/workflows/         # CI + auto-deploy + Notion sync

/home/warren/.openclaw/workspace-warren/      (on the VPS)
├── PROMPT.md / SOUL.md / IDENTITY.md / ...   # Warren agent definition
├── ARCHITECTURE.md                            # Pipeline documentation
├── skills/
│   ├── ticker-watch/          # Filter skill (NEW vs SKIP)
│   ├── macro-brief/           # Macro Brief skill (French prose, 150-300 words)
│   ├── tickerbrief/           # On-demand ticker brief skill
│   ├── modifyportfolio/       # Telegram portfolio management
│   └── modifywatchlist/       # Telegram watchlist management
└── memory/tickers/            # SYMBOL.md — last 3 raw news entries (written by Layer A /memorize)
```

---

## VPS Installation (Ubuntu 22.04+)

### Prerequisites

- Ubuntu 22.04 VPS (2 GB RAM minimum)
- A dedicated system user for the app (recommended: `warren`)
- Your Anthropic API key, Telegram bot token

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

### 5. Clone the repository and install Python deps

```bash
sudo mkdir -p /opt/apps/stock-tracker
sudo chown warren:warren /opt/apps/stock-tracker
sudo -u warren git clone https://github.com/patw47/stock-tracker.git /opt/apps/stock-tracker
cd /opt/apps/stock-tracker
sudo pip3 install --break-system-packages -r requirements.txt
```

> `anthropic` is required by the macro web search (Haiku `web_search` calls for Fed/geopolitics/sectors).
> Without it the Macro Brief silently falls back to quantitative FRED data only.

---

### 6. Configure environment variables

```bash
sudo -u warren cp .env.example .env
sudo -u warren nano .env
```

Fill in:

```env
ANTHROPIC_API_KEY=sk-ant-...
TELEGRAM_TOKEN=123456789:ABC...
TELEGRAM_CHAT_ID=1234567890
TWELVE_DATA_API_KEY=...               # Layer B EOD fallback data source
NODE_FUNCTION_ALLOW_BUILTIN=fs        # lets n8n Code nodes read portfolio/watchlist.json
N8N_PORT=5680
N8N_USER_FOLDER=/opt/apps/stock-tracker/n8n-data
N8N_BASIC_AUTH_ACTIVE=true
N8N_BASIC_AUTH_USER=admin
N8N_BASIC_AUTH_PASSWORD=your-password
GENERIC_TIMEZONE=Europe/Paris
N8N_DEFAULT_LOCALE=fr
N8N_ENCRYPTION_KEY=$(openssl rand -base64 32)
```

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
EnvironmentFile=/opt/apps/stock-tracker/.env
ExecStart=/usr/bin/python3 /opt/apps/stock-tracker/warren_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

> `EnvironmentFile` is **required** on the bridge service: the Macro Brief
> calls the Anthropic API directly (Haiku web search) and needs `ANTHROPIC_API_KEY`
> in the process environment.

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

# Test memorize endpoint (silent memory write, no synthesis)
curl -s -X POST http://127.0.0.1:18795/memorize \
  -H 'Content-Type: application/json' \
  -d '{"newTickers":["SMR"],"allNews":{"SMR":"NuScale signs PPA with Azure today"}}' \
  | python3 -m json.tool

# Expected: {"status": "ok", "written": ["SMR"]}

# Test macro-brief endpoint
curl -s -X POST http://127.0.0.1:18795/macro-brief \
  -H 'Content-Type: application/json' \
  -d '{}' \
  | python3 -m json.tool

# Expected: {"brief": "Le marché aborde cette séance dans un régime..."}
```

Test the Layer B pipeline end-to-end (prints a JSON payload):

```bash
cd /opt/apps/stock-tracker && python3 -m market_intelligence.eod_orchestrator --history-days 280
```

Test n8n is up:

```bash
curl -s http://localhost:5680/healthz
# Expected: {"status":"ok"}
```

---

## Continuous deployment (GitHub Actions → VPS)

Pushing to `main` auto-deploys to the VPS once CI is green. The deploy job runs on a
**self-hosted GitHub Actions runner installed on the VPS** (no SSH).

```
push main → CI green ──► .github/workflows/deploy.yml  (runs-on: self-hosted)
                              │
                              ├─ gate: "Notify PR merge conflicts" green on same commit
                              ├─ git fetch (token-auth) + reset --hard origin/main
                              └─ deploy/remote.sh (local on the VPS):
                                   pip install -r requirements.txt
                                   stop stock-tracker  (release sqlite lock + free port 5679)
                                   import_workflow.py  (upsert workflow into sqlite)
                                   restart openclaw-warren → warren-server
                                   n8n execute --id  (validation run, as warren, n8n stopped)
                                   start stock-tracker
                                   healthcheck: services + n8n :5680/healthz + bridge :18795
                              → Telegram status message (success / failure + per-service state)
```

See `docs/deployement.md` for the full rationale (self-hosted runner, custom sqlite
importer, validation-run constraints, user roles).

---

## Customizing tickers

`portfolio.json` and `watchlist.json` **are the source of truth** — the n8n
`Read Tickers` node and the Layer B registry read them at runtime. Three ways to edit:

1. **Telegram** — talk to Warren: the `modifyportfolio` / `modifywatchlist` skills
   add/remove tickers interactively (inline buttons, confirmation message).
2. **Edit the JSON files** on the VPS (`/opt/apps/stock-tracker/*.json`) — picked up
   at the next run, no n8n change needed.
3. **Git** — commit the change; deploy syncs the files.

Each entry needs `symbol`, `name`, `sector` (see `docs/ticker-files-schema.md`).
New tickers are validated against the Layer B registry; unresolvable symbols are
quarantined (`market_intelligence/data/quarantine.json`) instead of corrupting analysis.

---

## Warren memory

Warren stores raw news per ticker in `/home/warren/.openclaw/workspace-warren/memory/tickers/`.

- One file per ticker: `SYMBOL.md`
- Max 3 entries, newest first, separated by `---`
- Written by Layer A `/memorize` endpoint — only when new content is confirmed by Warren filter
- **Also read by Layer B**: the anomaly research prompt includes this memory, so
  Warren interprets a price move knowing what news already surfaced for the ticker
- **Also read by `tickerbrief` skill**: on-demand context without triggering a new search
- To reset a ticker's memory: `rm memory/tickers/SYMBOL.md`
- To reset all memory: `rm memory/tickers/*.md`

---

## Service management

```bash
# Status (services + reliability timers)
sudo systemctl status stock-tracker warren-server openclaw-warren
systemctl list-timers eod-watchdog.timer outcome-tracker.timer

# Restart all
sudo systemctl restart openclaw-warren warren-server stock-tracker

# Logs
sudo journalctl -u warren-server -f       # Python bridge logs
sudo journalctl -u stock-tracker -f       # n8n logs
sudo journalctl -u openclaw-warren -f     # OpenClaw logs
sudo journalctl -u eod-watchdog.service -n 20   # last watchdog verdict
sudo journalctl -u outcome-tracker.service -n 20

# "Why did I receive nothing tonight?" — the run explains itself
tail -1 runtime/market_intelligence/runs.jsonl | python3 -m json.tool

# Dedup state admin (never edit the JSON by hand)
python3 -m market_intelligence.dedup_admin show
python3 -m market_intelligence.dedup_admin reset --ticker SYMBOL
```

---

## Environment variables reference

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (Haiku web_search in n8n + macro brief Haiku calls in the bridge) |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Target chat ID |
| `TWELVE_DATA_API_KEY` | Layer B EOD data fallback (yfinance primary) |
| `NODE_FUNCTION_ALLOW_BUILTIN` | Must include `fs` — n8n Code nodes read the ticker JSON files |
| `N8N_PORT` | n8n HTTP port (default: `5680`) |
| `N8N_USER_FOLDER` | n8n data directory |
| `N8N_BASIC_AUTH_USER` | n8n login username |
| `N8N_BASIC_AUTH_PASSWORD` | n8n login password |
| `N8N_ENCRYPTION_KEY` | Credentials encryption key (generate once, never change) |
| `GENERIC_TIMEZONE` | Timezone for Layer A and Macro Brief scheduling (`Europe/Paris`). Layer B cron is UTC by design (DST safety) |
| `GMAIL_USER` / `GMAIL_PASS` | **Legacy** — email delivery was removed; Telegram only |

---

## Architecture decisions

Specs and decision log live in the Notion epics database (« Epics Stock Tracker »)
and the Obsidian vault (`Memory/stock-tracker/epics/`). Key choices:

- **Macro Brief as main product** — daily market regime brief even with no ticker news; Layer A becomes a silent collector
- **Layer A silent collection** — the Haiku + filter + memory pipeline survives suppression of synthesis/Telegram; preserves the `ticker_news_memory` context consumed by Warren S7
- **`/memorize` endpoint decoupled from `/synthesize`** — memory write triggered by Layer A without a Warren synthesis call; `/synthesize` kept as legacy for backward compatibility
- **`sector_rotation.py` + `fear_greed.py` zero-LLM** — quantitative signals injected into the Macro Brief without adding LLM cost to the collection path
- **Haiku for search** — cheaper, faster, sufficient for raw news retrieval and macro web search
- **Warren filter only in Layer A** — filter (NEW/SKIP dedup) retained; synthesis removed
- **Memory = raw news** — storing Haiku output (not Warren synthesis) for stable duplicate comparison
- **warren_server.py** — Python bridge needed because n8n sandboxes `fs` and `child_process` modules
- **No LLM in the detection path** — Layer B anomalies are pure statistics; Warren is only paid for surviving alerts
- **Beta gate = market-model regression** (not naive z-score comparison) — avoids false alerts on broad risk-off days for high-beta names
- **MAD scale, not standard deviation** — robust to the fat tails of speculative small-caps
- **Hysteresis dedup** — one alert per event, not per day; re-arms when the ticker calms down
- **Layer B cron in fixed UTC (21:30)** — Paris-time cron ran before the US close for ~3 weeks each March (EU/US DST mismatch)
- **systemd over PM2** — PM2 not available on this VPS; systemd provides equivalent reliability
- **Dry-run by default, commit after delivery** — a manual run must never mutate production dedup state; better a duplicate alert than a lost one (PM-0001 / NUAI incident)
- **Watchdog outside n8n** — its purpose is to detect n8n being dead, so it cannot live inside n8n
- **LLM output is untrusted input** — HTML-escaped producer-side before any Telegram parse
- **Workflow import must publish a version** — n8n executes the published version, not the draft; the deploy tooling asserts it
- **Measure gated candidates too** — the cost of a missed alert is measured the same way as the noise of a sent one
