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
| S6 Macro snapshot enrichi | Not started | | |
| S7 Warren ciblé | Not started | | |
| S8 Orchestration & livraison | Not started | | |

## Last implementation notes

S5 consumes all S3 decisions so calm EOD observations can rearm a latch.
Only genuinely new alerts proceed beyond S5. S4 remains context-only for S3;
`squeeze_prone=True` is added as a synthetic S5 override type only when an S3
candidate already exists.
S4 is a context-only module and must not alter S3 candidate selection.
Missing, unsupported, quarantined, or invalid short-interest data produces an
unknown squeeze flag, never a false flag.
Do not use legacy n8n ticker list as the technical-analysis source of truth.

## Known risks

- Two ticker sources exist:
  - legacy `portfolio.json` / `watchlist.json`
  - new `market_intelligence/data/registry.json`
- Symbol ambiguity: MMED, PLX, ALTD, GRO, NUAI.
- Macro tickers with special symbols: ^VIX, ^TNX, DXY, OIL.
- Short history for recent IPOs.
- US/EU DST shift for scheduling.
