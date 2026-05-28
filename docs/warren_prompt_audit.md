# Warren Prompt Audit — Gap Analysis

**Date:** 2026-05-28
**Scope:** Audit existing "Warren" stock-analyst prompt template(s) across the repo.
**Status:** Read-only audit — no functional changes.
**Ticket:** `.ticket-id 36e681d3-ae94-8134-9744-fc16b761a833`

---

## 1. Where the prompt lives

| Location | Form | Notes |
|---|---|---|
| `workflow.json` → node `Préparer requête Claude` (id `node-build-request`, line 39) | JS template literal inside an n8n Code node | Single source of truth for both prompt branches |
| `prompts/`, `agents/`, `config/` directories | **Absent** | No dedicated prompt folder exists |
| `portfolio.json`, `watchlist.json` | Data only (ticker lists) | Consumed by `Lire tickers` (`node-read-watchlist`) — note: workflow currently uses hardcoded inline arrays, NOT these JSON files (drift between repo files and workflow.json:26) |
| Memory index `MEMORY.md` → `project_setup.md` | Mentions "Haiku+Warren 2-call pipeline, 17-node workflow" | **Stale**: actual workflow is 11 nodes, single Claude call, model `claude-sonnet-4-5` (workflow.json:39 line "model: 'claude-sonnet-4-5'"). No "Warren" string in repo. |

No `system` message is sent — entire prompt sits in a single `role: user` message. Anthropic API `system` field unused.

---

## 2. Pipeline shape

```
Schedule → Lire tickers (inline JS array) → Préparer requête Claude (prompt build)
        → Claude API (POST /v1/messages, web_search beta) → Extraire briefing
        → ├── Agréger pour Email → Gmail
          └── Agréger pour Telegram → Découper → Telegram
```

Model config (workflow.json:39):
- `model: claude-sonnet-4-5`
- `max_tokens: 2048`
- `tools: [{ type: 'web_search_20250305', name: 'web_search', max_uses: 5 }]`
- Headers: `anthropic-version: 2023-06-01`, `anthropic-beta: web-search-2025-03-05`

---

## 3. Injected variables (current)

Only **three** runtime variables flow into the prompt:

| Variable | Source | Type | Notes |
|---|---|---|---|
| `${symbol}` | `Lire tickers` output (`item.json.symbol`) | string | e.g. `BBAI`, `HIMS` |
| `${sector}` | `Lire tickers` output (`item.json.sector`) | string FR | e.g. `IA défense` |
| `${date}` | `new Date().toLocaleDateString('fr-FR', ...)` | string FR `dd/mm/yyyy` | Computed at runtime |

Status flag `${status}` (`portfolio` \| `watchlist`) routes between two templates but is **not** rendered inside the prompt body — only controls branching.

---

## 4. Sections of the prompt (per branch)

### 4.1 Portfolio branch

| Section | Content |
|---|---|
| Persona | `"Tu es un analyste financier expert"` |
| Context | Holder of `${symbol}` (`${sector}`), date `${date}` |
| ÉTAPE 1 | MANDATORY `web_search` with query `"${symbol} stock news 2026"` |
| ÉTAPE 2 | If irrelevant → `SKIP`; else briefing |
| Focus directives | Risques à surveiller, signaux de sortie, news impactant la position |
| Output template | `## 💼 ${symbol} — ${sector} \| Briefing du ${date}` / `### 📰 Actualités récentes` / `### ⚠️ Impact sur ta position` / `### 🎯 Signal du jour` / `### 📝 Verdict` |
| Signal enum | `CONSERVER` \| `RENFORCER` \| `ALLÉGER` \| `VENDRE` |
| Confidence enum | `Faible` \| `Moyenne` \| `Forte` |

### 4.2 Watchlist branch

Identical structure except:

| Section | Difference |
|---|---|
| Context | "SURVEILLE sans l'avoir encore achetée" |
| Focus | Opportunité d'entrée, catalyseurs à venir, niveau de prix intéressant |
| Output | `👀 ${symbol}` / `### 💡 Opportunité d'entrée` / signal block / verdict |
| Signal enum | `ACHETER MAINTENANT` \| `ATTENDRE` \| `IGNORER` |

---

## 5. Gap analysis — macro context

Current prompt asks Claude for ticker-specific news only. Zero macro overlay injected. Below: each missing macro variable, why it matters, and where it should plug in.

### 5.1 Hard macro gaps (no data injected at all)

| Missing input | Why it matters for the signal | Suggested injection point |
|---|---|---|
| **Fed funds rate (current + last move)** | Rate level drives growth/value rotation, weighs on long-duration tickers (RGTI, VUZI, OKLO, SMR) | New `${macro.fed_funds}` variable in user msg or `system` block |
| **FOMC forward guidance / next meeting date** | Position sizing decisions hinge on rate-cut probability; "ATTENDRE" vs "ACHETER" should shift around FOMC weeks | `${macro.next_fomc}` + `${macro.cut_prob}` |
| **CPI / PCE inflation (latest YoY + MoM)** | Inflation surprises trigger sector rotation; small-caps (most of the portfolio) get hit hardest | `${macro.cpi_yoy}`, `${macro.pce_yoy}` |
| **VIX level + regime** | High-VIX regime invalidates "ACHETER MAINTENANT" for speculative names (BBAI, RGTI, MMED) | `${macro.vix}` + `${macro.vix_regime}` (calm/elevated/stressed) |
| **Yield curve (2s10s, 3m10y)** | Inversion regime affects banks, REITs, small-cap risk premium | `${macro.curve_2s10s}` |
| **DXY (dollar index)** | Strong USD pressures multinationals (XYL), commodity-linked names | `${macro.dxy}` |
| **Sector rotation snapshot (XLK/XLE/XLF/XLV 5d, 20d perf)** | Watchlist tickers grouped by sector — rotation context tells "is sector tailwind or headwind?" | `${macro.sector_perf}` table |
| **Earnings season phase** | Pre-earnings vs post-earnings window changes signal weight | `${macro.earnings_phase}` |
| **Ticker-level upcoming catalyst (earnings date, FDA, expiry)** | "Catalyseurs à venir" currently asked of LLM with no data — hallucination risk | `${ticker.next_earnings}`, `${ticker.catalysts[]}` |

### 5.2 Soft gaps (prompt structure / engineering)

| Gap | Risk | Suggested fix |
|---|---|---|
| No `system` message — persona stuffed in user msg | Lower instruction adherence; cache-key churn on every call (no cacheable system block) | Move persona + macro snapshot into `system` (cacheable per Anthropic prompt caching) |
| Hardcoded year `"2026"` in web_search query | Will produce stale searches in 2027+ | Inject `${year}` derived from `new Date().getFullYear()` |
| Date in FR locale `dd/mm/yyyy` | Ambiguous for LLM (could read `05/06` as May 6 or June 5) | Pass ISO `YYYY-MM-DD` alongside FR display string |
| Output is free-text Markdown | Downstream `Extraire briefing` does loose `.toUpperCase() === 'SKIP'` check; no structured signal field for analytics | Request JSON or JSON-in-fenced-block (signal, confidence, verdict, sources) |
| No source-citation requirement | Web search results not cited → cannot audit recommendation provenance | Add "cite each fact with URL from search results" directive |
| No max-age cutoff for "Actualités récentes" | LLM can dredge stale news as "récent" | Add explicit "last 7 days only" rule |
| No prior-briefing memory | Same signal repeated daily; no awareness of yesterday's call | Inject `${ticker.last_signal}` + `${ticker.last_signal_date}` (requires persistence layer) |
| No portfolio context (cost basis, weight, unrealized P&L) | "RENFORCER / ALLÉGER" given with zero position context | Inject `${position.cost_basis}`, `${position.weight_pct}`, `${position.unrealized_pct}` (portfolio.json needs schema bump) |
| No risk budget / max position size | "RENFORCER" signal can violate sizing rules silently | Inject `${risk.max_position_pct}` |
| Inline ticker array in workflow.json:26 diverges from `portfolio.json` / `watchlist.json` on disk | README documents the JSON files, code ignores them | Bug — separate ticket, but blocks prompt rework if macro vars live in JSON |
| No language directive | French inferred from prompt language only; brittle | Explicit `"Réponds en français"` line |
| `continueOnFail: true` on Claude API node, but no error sentinel in extractor | Failed call → empty briefing → silent skip, no alert | Add explicit error path |

---

## 6. Variable inventory — current vs proposed

```
CURRENT (3 vars):
  ${symbol}, ${sector}, ${date}

PROPOSED — macro block (~9 vars):
  ${macro.fed_funds}, ${macro.next_fomc}, ${macro.cut_prob},
  ${macro.cpi_yoy}, ${macro.pce_yoy},
  ${macro.vix}, ${macro.vix_regime},
  ${macro.curve_2s10s}, ${macro.dxy},
  ${macro.sector_perf}, ${macro.earnings_phase}

PROPOSED — ticker-level enrichment (~4 vars):
  ${ticker.next_earnings}, ${ticker.catalysts[]},
  ${ticker.last_signal}, ${ticker.last_signal_date}

PROPOSED — portfolio-level (portfolio branch only, ~3 vars):
  ${position.cost_basis}, ${position.weight_pct}, ${position.unrealized_pct}

PROPOSED — meta (~2 vars):
  ${year}, ${iso_date}
```

---

## 7. Risk-ranked gap priorities

| Rank | Gap | Impact | Effort |
|---|---|---|---|
| P0 | No macro snapshot (Fed, CPI, VIX) | Wrong signals in rate-shock weeks | Medium — needs new data source node |
| P0 | No structured output (JSON) | Blocks any analytics / dashboard / backtest | Low — prompt change + extractor change |
| P1 | No position context for portfolio branch | "ALLÉGER" / "RENFORCER" given blind | Medium — needs P&L tracker |
| P1 | Hardcoded `2026` in search query | Time bomb | Trivial |
| P1 | No `system` message / no prompt caching | Cost: every call full re-tokenization | Low |
| P2 | No source citation | Audit / trust | Low |
| P2 | No "last 7 days" cutoff on news | Stale news contamination | Trivial |
| P2 | No prior-signal memory | Decision churn / repeated signals | High (persistence layer) |
| P3 | Inline tickers vs JSON files drift | Maintainability | Low (separate ticket) |
| P3 | FR date ambiguity | Edge-case misread | Trivial |

---

## 8. Out of scope (flagged for follow-up tickets)

- Macro data source selection (FRED API, Alpha Vantage, manually-curated daily JSON?)
- Persistence layer for prior-signal memory
- Schema bump for `portfolio.json` to carry cost basis + weights
- Removing inline ticker arrays from `workflow.json:26` in favor of file reads
- Memory index correction (`MEMORY.md` claims 17-node Haiku 2-call pipeline; actual is 11-node Sonnet single call)

---

## 9. References

- `workflow.json:26` — `Lire tickers` (inline ticker source)
- `workflow.json:39` — `Préparer requête Claude` (prompt template, both branches)
- `workflow.json:52-90` — Claude API HTTP node config
- `workflow.json:92-103` — `Extraire briefing` (SKIP detection)
- `portfolio.json`, `watchlist.json` — disk tickers (currently unused by workflow)
- `README.md` — pipeline overview
