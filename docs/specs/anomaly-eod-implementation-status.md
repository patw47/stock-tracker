# Anomaly EOD Detection — Implementation Status

## Source of truth

Spec: docs/specs/anomaly-eod-detection.md

## Global invariants

- Existing Warren/news pipeline must remain intact.
- New EOD anomaly layer is complementary.
- No LLM in the anomaly detection critical path.
- Warren is called only after anomaly filtering + beta gate + dedup.
- MacroSnapshot is computed once per run and reused.
- Missing or ambiguous ticker data must be flagged, never silently accepted.
- S0 is already implemented. Future sprints must consume S0, not rewrite it.

## Current foundation

Sprint 0 implemented:
- `market_intelligence/fetch_eod.py`
- `market_intelligence/normalize_quality.py`
- `market_intelligence/registry_schema.py`
- `market_intelligence/data/registry.json`
- `market_intelligence/data/quarantine.json`
- `tests/market_intelligence/`

Sprint 0 correction PR:
- adds registry/quarantine files
- adds pandas/yfinance/pyarrow requirements
- runs pytest in CI

## Sprint status

| Sprint | Status | Branch/PR | Notes |
|---|---|---|---|
| S0 Fondation données & intégrité symboles | Done | PR #19 + PR #20 | Do not rewrite |
| S1 Signaux d'anomalie | Done | | Deterministic EOD anomaly signals |
| S2 Gate bêta | Done | | Market-model residual with optional sector factor |
| S3 Seuils & alertes candidates | Done | | Deterministic thresholds, short-history fallback, explicit quality decisions |
| S4 Short interest / squeeze | Done | | Yahoo context with explicit unknown coverage |
| S5 Dédup hystérésis | Done | | Persistent state, rearm, overrides, escalation, max-latch valve |
| S6 Macro snapshot enrichi | Done | | Deterministic macro snapshot cache; 10Y/IWM/OIL/VIX/DXY fields |
| S7 Warren ciblé | Done | | Post-dedup Warren research context with EDGAR, squeeze and macro |
| S8 Orchestration & livraison | Done | | EOD runner S0-S7 + n8n Layer B schedule/gated Telegram digest |

## Last implementation notes

S8 adds `market_intelligence/eod_orchestrator.py` as the deployment boundary for
the anomaly layer. It runs S0 fetch on the registry foundation, S1 signals, S2
beta gate, S3 candidate decisions, S4 short context, S5 dedup, one S6 macro
snapshot per run, and S7 Warren analysis only for post-dedup survivors.

The n8n workflow now keeps Layer A news intact and adds a separate Layer B EOD
branch scheduled at 22:30 Europe/Paris on weekdays. The branch calls the S8
runner, gates on `survivor_count > 0` and `should_send`, then reuses the
existing Telegram aggregate/split/send nodes for one digest. No-survivor days
stop before Telegram.

## Known risks

- Two ticker sources exist:
  - legacy `portfolio.json` / `watchlist.json`
  - new `market_intelligence/data/registry.json`
- Symbol ambiguity: MMED, PLX, ALTD, GRO, NUAI.
- Macro tickers with special symbols: ^VIX, ^TNX, DXY, OIL.
- Short history for recent IPOs.
- US/EU DST shift for scheduling.
