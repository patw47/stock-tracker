# Ticker Files Schema

Location and structure of portfolio and watchlist files.

| Property | Portfolio | Watchlist |
|----------|-----------|-----------|
| **Relative path** | `portfolio.json` | `watchlist.json` |
| **Top-level key** | `tickers` (array) | `tickers` (array) |
| **Entry format** | Object | Object |
| **Entry fields** | `symbol`, `name`, `sector` | `symbol`, `name`, `sector` ; entries written by the [v5 bridge](../README.md#-v5-bridge--smallcaps-cohorts-into-the-watchlist) carry `symbol`, `added`, `source: "smallcaps-v5"` and `cohort` instead (no `name`/`sector` — nothing reads them for detection) |
| **Example entry** | `{ "symbol": "BBAI", "name": "BigBear.ai", "sector": "IA défense" }` | `{ "symbol": "SMR", "name": "NuScale Power", "sector": "Nucléaire SMR" }` |
| **Entry count** | 8 in the example (VPS file is the source of truth) | 8 in the example ; **~152 on the VPS** (grown via `/modifywatchlist`), plus up to 150 bridged entries once `v5-bridge.timer` is deployed (~82 at the first run, expiring at J+63) |
| **Additional fields** | `updated_at` (metadata) | `updated_at` (metadata), `v5_scanned_at` (bridge monotonicity bound), and per bridged entry `cohort` = the screener's judgment context, kept verbatim (`entry_date`, `entry_price`, `days_held`, `days_left`, `ret`, `status`), refreshed on every applied snapshot and rendered in the EOD alert block |
| **Detection tier** | Layer B + Layer C (registry entry, classification and factor mapping required) | Layer B + C once in the registry (PR #53 onboarded all 152) ; a **new** add is covered by the tension tier (Layer C only) until onboarded |

## Usage in n8n Code node

Extract symbols from both files:

```javascript
// Read both files
const portfolio = JSON.parse(readFileSync('./portfolio.json', 'utf8'));
const watchlist = JSON.parse(readFileSync('./watchlist.json', 'utf8'));

// Extract ticker symbols
const tickers = [
  ...portfolio.tickers.map(t => t.symbol),
  ...watchlist.tickers.map(t => t.symbol)
];
```

Result: array of 16 strings (8 portfolio + 8 watchlist).

## n8n node update

Node `Read Tickers` (`node-read-watchlist`) updated to read from files at runtime.
Absolute host paths: `/opt/apps/stock-tracker/portfolio.json` and `/opt/apps/stock-tracker/watchlist.json`.
Output shape unchanged: `{ symbol, sector, status }` per item (status derived from source file).

## Editing the lists

Three supported paths — all converge on the same JSON files:

1. **Telegram** — Warren skills `modifyportfolio` / `modifywatchlist` (inline buttons,
   add/remove, confirmation message). Backend: `agents/warren/manage_tickers.py`,
   which also stamps `updated_at`.
2. **Direct edit** on the VPS — picked up at the next scheduled run.
3. **Git** — the deploy syncs the repo; note the real files are not versioned, only
   `portfolio.example.json` / `watchlist.example.json` are.

Layer B (`market_intelligence/`) does not read these files directly: it uses
`runtime/referential/registry.json` (symbol → api_symbol → expected_name) plus
`data/quarantine.json` so analysis never runs on a wrong or ambiguous ticker.

**Where each half lives (Epic 10 S4).** The referential is split *inside* its
files, along one line: what is **decided** stays in git, what **follows the
cohort** does not.

| Half | Files | Versioned? | Who writes it |
|---|---|---|---|
| Configuration | `data/alert_thresholds.json` (thresholds), `data/sector_factors.json` (market factor, correlation threshold, sector ETF map), `data/dedup_thresholds.json`, `data/short_interest_thresholds.json`, `data/quarantine.json` | yes | a human, in a PR |
| State | `runtime/referential/registry.json`, `runtime/referential/classifications.json`, `runtime/referential/single_factor_symbols.json` | **no** (gitignored) | the v5 bridge, every run |

The state half follows the cohort since Epic 10 S2, so it changes daily: keeping
it in git meant a deploy `git reset --hard` destroyed it, which is why an
automatic commit had to exist at all. `market_intelligence/registry_check.py`
resolves both halves — readers follow by importing its constants, and nothing
else needs to know where the files sit.

`make check-config-invariant` locks the configuration half against accidental
rewriting: an automatic path (onboarding, offboarding, bridge sync) may never
change a threshold or a sector mapping. A deliberate change is made in a PR, then
the reference is regenerated with `--update`.

Add a **portfolio** ticker → its registry entry follows automatically; a sector
ETF mapping in `data/sector_factors.json` remains a PR decision if relevant.
`registry_check` blocks the deploy on an incoherence.

**Watchlist safety net (tension tier).** The EOD orchestrator also reads
`watchlist.json` directly (`_load_watchlist_symbols`): any entry **not yet in
the registry** is scanned by Layer C the same evening (tension: squeeze +
quiet accumulation — see `docs/TENSION.md`), OHLCV fetched in one batched
call, invalid symbols degrading to an empty frame (never journaled, never
alerted). `registry_check` reports such a ticker as **info**, not blocking —
the deploy never fails because someone added tickers via Telegram. Onboard the
ticker to the registry to get full Layer B on it (beta gate, Warren research);
as of PR #53 the entire current watchlist (152) is onboarded.
