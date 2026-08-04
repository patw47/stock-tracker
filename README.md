# Stock Tracker — Anomaly Alerts

Automated stock monitoring system built with **n8n** + **Warren** (OpenClaw agent) + a **Python anomaly-detection layer** (`market_intelligence/`).

Deterministic EOD anomaly detection, one Telegram channel:

| Layer | What | When | LLM? |
|---|---|---|---|
| **B — Anomaly detection (EOD)** | Price/volume anomaly scan → beta gate → dedup → **deterministic digest** | 21:30 UTC, Mon–Fri (after US close) | **Zero LLM** — the digest is pure Python (signals + hysteresis reason); Warren is on-demand only (`point sur TICKER`) |

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
         │   Layer C tension scan (registry + watchlist tickers, no LLM)
         │      → tension.jsonl journal + ⚡ digest block on new episodes
         │   Deterministic digest (zero LLM) — canned per-survivor prose keyed by
         │      the dedup fire_reason with the anomaly numbers slotted in; each
         │      line tagged with the ticker's origin (💼 portfolio / 👀 watchlist)
         │      and opened on the raw close + intraday read + z gloss; points
         │      the reader to on-demand Warren (« point sur TICKER »)
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

## 🔍 Anomaly detection — an attention detector, measured as such

### The problem we are solving

On speculative small caps, **price often moves before the news becomes public**
(rumor, accumulation, squeeze). A pipeline that only reads news therefore arrives
late. Layer B does not predict anything: it detects that **an unusual move has
just happened**, then surfaces it for on-demand investigation (« point sur
TICKER » to Warren). It is an attention detector,
not a crystal ball — and that claim is now **measured**, not asserted: a
no-look-ahead ablation over 2022-2026 ([docs/RESULTS.md](docs/RESULTS.md))
shows directional hit rates of 41-49% (≈ coin flip) for every variant of the
pipeline. Its measured value is **selectivity** (30 alerts/month vs 79 for a
naive >5%-move rule, −62%), not direction. Detecting *before* the move is
Layer C's job (below).

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

### Step 5 — Deterministic digest (and on-demand Warren)

Surviving alerts cost **zero LLM**. Each survivor is rendered with canned prose
keyed by the dedup `fire_reason` (initial / escalation / new signal type /
direction reversal) with the anomaly numbers slotted in, plus a squeeze-prone
flag when relevant.

Every line carries the ticker's **origin** — read from `portfolio.json` /
`watchlist.json`, portfolio winning when a ticker is in both, `registre seul`
when it is in the registry but in neither list (a referential inconsistency
worth seeing). Fail-soft: an unreadable list drops the tags, never the run.

Each survivor's prose opens on the day's **raw close**, then reads the session by
three fixed rules on the opening gap (move already there at the open / built in
session / intraday reversal, with a "cassure ratée" when a 52-week breakout was
sold off). The z-residual keeps its exact figure and gains a plain-language
gloss; each Layer C signal gets a canned one too. Still zero LLM: every sentence
is a template triggered by an existing field.

```
1. HIMS (💼 portefeuille) — baisse ↓   [escalade]
Escalade : HIMS était déjà verrouillé (il avait déclenché à −2,4)…
Concrètement : clôture −9,8 % aujourd'hui, alors que le titre avait OUVERT en
hausse (+2,2 %) et touché un plus-haut de 52 semaines en séance — il s'est
retourné en cours de journée (cassure ratée : l'élan du matin a été vendu).

2. BBAI (👀 watchlist) — hausse ↑   [première alerte]
Concrètement : clôture +7,2 % aujourd'hui, après une ouverture déjà en hausse
(gap +6,4 %). … Son z-résiduel atteint +2,8 (seuil 2,5) — le titre bouge
nettement plus que son comportement habituel : son mouvement propre, une fois
retirée la part expliquée par le marché, fait environ 3× sa journée typique.

ASTS (registre seul): squeeze (bw pctl 8%)      ← ⚡ Layer C block
   ↳ volatilité comprimée dans le pire décile de son année — un mouvement se
     prépare, direction inconnue
```

The digest points the reader to Warren for a deeper look
**on demand** — « point sur TICKER » — which is when insider buying/selling
(SEC EDGAR Form 4), product and sector news, halt (FINRA) and SSR (Nasdaq)
status and the ticker's news memory are pulled together. Warren is allowed to
conclude *"no identifiable catalyst — flow/technical/squeeze likely"* rather
than make things up. The dead n8n→OpenClaw HTTP bridge that used to run this
per-alert automatically was removed in Epic 6 (deterministic digest is now the
only alert path).

### What anomaly detection does not do

- It does not predict direction (a volume spike says "look", not "it goes up").
  Measured: J+5 hit rate 44% vs 49% for the naive baseline
  ([docs/RESULTS.md](docs/RESULTS.md)).
- It does not detect *before* the move: it scans close bars at 21:30 UTC, so
  the move is hours old by design. That is Layer C's job.
- It does not trade. A high false-positive rate is accepted: this is an
  attention tool, strictly better than a daily news scan, not a robot.

Configuration: thresholds in `market_intelligence/data/alert_thresholds.json`,
sector mapping in `data/sector_factors.json`, hysteresis in
`data/dedup_thresholds.json`, squeeze in `data/short_interest_thresholds.json`.

---

## ⚡ Layer C — Tension (detect *before* the move)

The theory: explosions on small caps are preceded by **silent accumulation**
visible on close bars — volatility compression and volume without price. Layer C
scores that tension every evening and alerts on the **first day** a ticker
enters a tension state, typically days before any move (median lead in
backtest: ~13 trading days).

| Signal | Definition | Threshold |
|---|---|---|
| **SQUEEZE** | 20d Bollinger bandwidth percentile within the trailing year | ≤ 10% |
| **QUIET** (silent accumulation) | 5d mean relative volume, with a flat 5d cumulative return | rvol5 ≥ 2 and \|cum5\| < 3% |
| **TENSION** | SQUEEZE or QUIET | — |

No direction is predicted (compression predicts expansion, not sign) and no
LLM is involved: the ⚡ digest block is deterministic.

**Coverage — portfolio and watchlist.** Layer C scans **every registry ticker**
(166 as of July 2026: the full VPS portfolio + watchlist, onboarded in PR #53)
**plus any watchlist ticker not yet in the registry** — the *tension tier*:
OHLCV fetched directly in one batched call, no registry entry, classification
or sector-ETF mapping required. A ticker added via `/modifywatchlist` is
therefore scanned by Layer C the **same evening**, before its registry
onboarding; `registry_check` reports it as info, not blocking. Full Layer B
(beta gate + candidate alerting) requires the registry entry.

**Honesty section.** The phase-0 backtest (2022-2026, [docs/TENSION.md](docs/TENSION.md))
found lift 1.5-1.66 for P(move > 2× the ATR-expected 20d move) after a tension
episode — but the signal is **regime-unstable**: ~1.1 in 2022-2024, ~1.9 in
2024-2026. Alerts are live since 2026-07-10 (owner's decision) while the live
measurement runs in parallel: every ticker-day is journaled
(`runtime/market_intelligence/tension.jsonl`), episode outcomes are measured at
J+20 (`tension_outcomes`, systemd timer 22:35 UTC), and the reading benchmark —
informative, not binding — is lift ≥ 1.5 over ≥ 50 measured episodes.
`python3 -m market_intelligence.tension_outcomes --report` shows the running
tally.

---

## 🌉 v5 bridge — smallcaps cohorts into the watchlist

`market_intelligence/v5_bridge.py` makes every ticker that entered a **washout
cohort** in [smallcaps-screener](https://app.notion.com/p/3ae681d3ae94816c9611d1c61d562677)
watched by Layer C for its judgment window, then drops it out again. It carries
**attention, never a verdict**: both signals (tension here, v5 cohort there) are
in forward validation.

- **Source** — the smallcaps API on the same VPS, read at `SMALLCAPS_API_URL`
  (default `http://localhost:8000`). The `/api/scan` payload embeds the v5
  tracking journal (one row per window × ticker). The union of the 7/14/21-day
  windows is deduplicated per ticker, longest `days_held` winning.
  ⚠️ **Never point this at a public URL**: the API has no authentication and
  serves unversioned edge values.
- **Reconciliation, not an event stream** — the target state is derived from the
  journal on every run: tracked and `days_held < 63` → in the watchlist;
  `days_held >= 63` (the v5 judgment horizon) or gone from the journal → out.
  Idempotent, so a missed run costs nothing.
- **Watchlist only, never Layer B** — entries are written through the atomic
  helpers of `agents/warren/manage_tickers.py`; registry onboarding stays a human
  decision in a PR. The ticker is covered by the tension tier the same evening
  and `registry_check` reports it as `info`, never blocking.
- **Provenance `source: "smallcaps-v5"`** — the bridge removes **only** its own
  entries. A ticker added via Telegram is untouchable (safety rule #1).
- **Cap of 150** bridged tickers, with an explicit log line listing the excluded
  ones — never a silent truncation.
- **Fail-soft throughout** — API down, non-200, malformed payload → logged no-op.
  Empty tracking *while* bridged tickers are still under horizon → no-op plus a
  warning: that is an anomaly, never a purge.
- **Disabling it** — stop the systemd unit; the watchlist keeps whatever it holds
  (bridged entries stop expiring, which is inert). Removing them by hand means
  dropping the entries tagged `smallcaps-v5`.

Run it manually: `python3 -m market_intelligence.v5_bridge` (prints the run
summary as JSON). The daily cadence (systemd timer, 20:45 UTC Mon–Fri, after the
smallcaps scan and before the 21:30 EOD run) ships in Sprint 2.

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
3. **Safe delivery** — every value sent to Telegram is HTML-escaped
   producer-side (`parse_mode=HTML`), the 4,000-char splitter never cuts a tag
   in half, and `Send Telegram` retries ×3 with the final failure kept visible
   (no `continueOnFail`).
4. **Unified ticker referential** — `python3 -m
   market_intelligence.registry_check` is blocking in CI and at deploy time: a
   **portfolio** ticker can no longer be silently invisible to detection.
   A watchlist ticker not yet in the registry is still scanned by the tension
   tier (Layer C) the same evening and is reported as info, not blocking —
   never invisible, never a deploy failure. New registry
   tickers are onboarded with safe defaults (`speculative` classification,
   `single_factor_symbols`, symbol validated before any write); the sector-ETF
   choice remains an explicit human decision in PR.
5. **Track record** — J+1/J+5/J+20 outcomes are measured for sent alerts **and
   for gated candidates** (`outcome_tracker`, systemd timer 22:30 UTC), a
   monthly Telegram report aggregates them (with an honest "sample too small"
   guard), and a no-look-ahead backtest (`python3 -m market_intelligence.backtest
   --start … --end …`) replays the deterministic pipeline over years of
   history to calibrate thresholds.

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

- **EOD anomaly trigger (B)** — deterministic detection, one Telegram channel
- **Zero LLM in the detection path** — Layer B is pure Python/statistics until an alert survives
- **Ticker news memory** — Warren's anomaly research includes the ticker's news memory (`memory/tickers/`)
- **Sector rotation signal** — `sector_rotation.py` computes sector ETF relative performance + IWM/SPY ratio (zero LLM)
- **Fear & Greed** — `fear_greed.py` fetches CNN Fear & Greed index (best-effort, zero LLM)
- **On-demand ticker brief** — `tickerbrief` skill via Telegram: full ticker context without triggering a digest
- **Tickers as data** — `portfolio.json` / `watchlist.json` are the source of truth, editable via Telegram (`modifyportfolio` / `modifywatchlist` skills)
- **Symbol integrity** — registry + quarantine (`market_intelligence/data/`) so analysis never runs on a wrong ticker
- **Auto-split Telegram** — messages split at 4,000 characters at paragraph boundaries, HTML-safe
- **Transactional dedup state** — dry-run by default, two-phase commit after confirmed delivery
- **Run journal + external watchdog + weekly heartbeat** — silence is informative, not ambiguous
- **Referential consistency enforced** — `registry_check` blocking in CI and at deploy
- **Self-measured signal** — J+1/J+5/J+20 outcomes, monthly report, no-look-ahead backtest
- **systemd managed** — n8n, OpenClaw gateway (on-demand Warren) + watchdog/outcome timers
- **CI/CD** — push to `main` → 364 tests → auto-deploy on the VPS via self-hosted runner (workflow version published, referential validated)

---

## Stack

| Component | Role |
|---|---|
| **n8n** (self-hosted) | Scheduling, API calls, credential management, delivery |
| **OpenClaw** | Agent framework wrapping Claude for Warren |
| **Warren** (OpenClaw agent) | On-demand intelligence — ticker briefs ("point sur X"), Telegram ticker management |
| **agents/warren/** | `manage_tickers.py` — add/remove tickers backing the Telegram skills |
| **market_intelligence/** | Layer B — EOD fetch, anomaly signals, beta gate, dedup, EDGAR, short interest, orchestrator |
| **market_intelligence/sector_rotation.py** | Sector ETF relative performance + IWM/SPY ratio — zero LLM |
| **market_intelligence/fear_greed.py** | CNN Fear & Greed index fetch — zero LLM |

---

## Project structure

```
stock-tracker/
├── workflow.json              # n8n workflow (Layer B EOD anomaly wiring)
├── portfolio.json             # 8 portfolio tickers (source of truth)
├── watchlist.json             # 8 watchlist tickers (source of truth)
├── requirements.txt           # Python deps (requests, numpy, pandas, yfinance, pyarrow)
├── agents/warren/             # manage_tickers.py — ticker management backing the Telegram skills
├── market_intelligence/       # Layer B anomaly detection (S0–S5) + reliability & measurement tooling
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
│   ├── tickerbrief/           # On-demand ticker brief skill spec (SKILL.md)
│   ├── modifyportfolio/       # Portfolio management via Telegram (with Layer B onboarding)
│   └── modifywatchlist/       # Watchlist management via Telegram (with Layer B onboarding)
├── tests/                     # pytest suite — 364 tests (agents, market_intelligence, deploy, workflow wiring)
├── docs/                      # project-structure, deployment, backtest guide, ticker schema
├── deploy/                    # CI/CD: remote.sh, import_workflow.py (version publish),
│                              # watchdog_eod.py + systemd units/timers (watchdog, outcome tracker)
└── .github/workflows/         # CI + auto-deploy + Notion sync

/home/warren/.openclaw/workspace-warren/      (on the VPS)
├── PROMPT.md / SOUL.md / IDENTITY.md / ...   # Warren agent definition
├── ARCHITECTURE.md                            # Pipeline documentation
├── skills/
│   ├── tickerbrief/           # On-demand ticker brief skill
│   ├── modifyportfolio/       # Telegram portfolio management
│   └── modifywatchlist/       # Telegram watchlist management
└── memory/tickers/            # SYMBOL.md — last 3 raw news entries (read by tickerbrief)
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

Enable and start both:

```bash
sudo systemctl daemon-reload
sudo systemctl enable stock-tracker openclaw-warren
sudo systemctl start openclaw-warren
sleep 3
sudo systemctl start stock-tracker
```

Verify:

```bash
sudo systemctl status stock-tracker openclaw-warren
```

Both should show `active (running)`.

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
                                   décommission du pont Warren mort (disable --now warren-server)
                                   restart openclaw-warren  (gateway on-demand)
                                   n8n execute --id  (validation run, as warren, n8n stopped)
                                   start stock-tracker
                                   healthcheck: services + n8n :5680/healthz
                              → Telegram status message (success / failure + per-service state)
```

See `docs/deployement.md` for the full rationale (self-hosted runner, custom sqlite
importer, validation-run constraints, user roles).

---

## Customizing tickers

`portfolio.json` and `watchlist.json` **are the source of truth** — the Layer B
registry reads them at runtime. Three ways to edit:

1. **Telegram** — talk to Warren: the `modifyportfolio` / `modifywatchlist` skills
   add/remove tickers interactively (inline buttons, confirmation message).
2. **Edit the JSON files** on the VPS (`/opt/apps/stock-tracker/*.json`) — picked up
   at the next run, no n8n change needed.
3. **Git** — commit the change; deploy syncs the files.

The [v5 bridge](#-v5-bridge--smallcaps-cohorts-into-the-watchlist) also writes to
`watchlist.json`, but only entries tagged `source: "smallcaps-v5"` — it never
touches the ones you added.

Each entry needs `symbol`, `name`, `sector` (see `docs/ticker-files-schema.md`).
New tickers are validated against the Layer B registry; unresolvable symbols are
quarantined (`market_intelligence/data/quarantine.json`) instead of corrupting analysis.

---

## Warren memory

Warren stores raw news per ticker in `/home/warren/.openclaw/workspace-warren/memory/tickers/`.

- One file per ticker: `SYMBOL.md`
- Max 3 entries, newest first, separated by `---`
- No longer auto-populated — the Layer A news collection was removed (Epic 6 S1); existing entries remain
- **Read by the `tickerbrief` skill**: on-demand context without triggering a new search
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
| `ANTHROPIC_API_KEY` | Anthropic API key (OpenClaw gateway — on-demand Warren) |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Target chat ID |
| `TWELVE_DATA_API_KEY` | Layer B EOD data fallback (yfinance primary) |
| `SMALLCAPS_API_URL` | smallcaps-screener API for the v5 bridge (default `http://localhost:8000`, loopback only) |
| `NODE_FUNCTION_ALLOW_BUILTIN` | Must include `fs` — n8n Code nodes read the ticker JSON files |
| `N8N_PORT` | n8n HTTP port (default: `5680`) |
| `N8N_USER_FOLDER` | n8n data directory |
| `N8N_BASIC_AUTH_USER` | n8n login username |
| `N8N_BASIC_AUTH_PASSWORD` | n8n login password |
| `N8N_ENCRYPTION_KEY` | Credentials encryption key (generate once, never change) |
| `GENERIC_TIMEZONE` | n8n display timezone (`Europe/Paris`). Layer B cron is UTC by design (DST safety) |
| `GMAIL_USER` / `GMAIL_PASS` | **Legacy** — email delivery was removed; Telegram only |

---

## Architecture decisions

Specs and decision log live in the Notion epics database (« Epics Stock Tracker »)
and the Obsidian vault (`Memory/stock-tracker/epics/`). Key choices:

- **`sector_rotation.py` + `fear_greed.py` zero-LLM** — quantitative signals available on demand without adding LLM cost
- **Layer A news collection removed** — Epic 6 S1 dropped the automatic Haiku news + Warren filter pipeline; `memory/tickers/` is now read-only context (tickerbrief)
- **Warren HTTP bridge removed** — Epic 6 S4 dropped the dead n8n→OpenClaw bridge; the EOD digest is fully deterministic and Warren is on-demand only (Telegram "point sur X")
- **No LLM in the alert path** — Layer B anomalies and the digest are pure statistics; Warren is on-demand only, never on the critical path
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
