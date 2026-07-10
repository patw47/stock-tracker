# Layer B — signal quality (ablation)

EOD anomaly detection, 2022-01-01 → 2026-05-31, 1105 trading days, 16 tickers (53 months). Generated 2026-07-10.

**Read deltas, not levels.** The ticker universe is selection-biased; all arms share that bias so between-arm gaps are meaningful, absolute levels are not a performance claim. Attention detection ≠ tradable edge. Caveats at the bottom.

## Bottom line

- **Selectivity, not edge.** The full pipeline fires 30 alerts/month vs the naive >5%-move rule's 79 (−62%), at a J+5 directional hit rate of 44% vs 49% — it **does NOT beat** naive on direction. All arms sit at 41–49% (≈ coin flip): this measures attention, not direction.
- **Hysteresis earns its keep.** The candidates it suppresses have a J+5 hit rate +10 pts below the alerts it lets through (n=311) — it removes noise.
- **The beta gate is factor-hygiene, not a quality filter.** It re-scores rather than filters — removes 19% of raw-z alerts, net +340 — and the raw-z arm's J+5 hit rate (47%) is ≥ the gated one (44%); the alerts the gate adds underperform (37% vs 44%).

## Headline (deltas)

| metric | value |
|---|---|
| Beta gate: raw-z alerts it removed | 19% (net alert change +340) |
| Hysteresis: candidates it removed | 17% |
| J+5 hit-rate: sent (FULL) − beta-suppressed | +0 pts |
| J+5 hit-rate: sent (FULL) − gate-added | +7 pts |
| J+5 hit-rate: sent (FULL) − hyst-suppressed | +10 pts |
| J+5 hit-rate: sent (FULL) − naive baseline | -5 pts |
| Verdict beta gate (vs suppressed) | no clean separation — beta-suppressed ≈ sent on J+5 hit rate |
| Verdict beta gate (vs added) | gate DILUTES — its net-new alerts underperform sent by 7 pts J+5 hit |
| Verdict hysteresis | removes NOISE — hyst-suppressed underperform sent by 10 pts J+5 hit |

## Funnel — production path

| stage | speculative | calm | all |
|---|--:|--:|--:|
| trading days | | | 1105 |
| ticker-days scanned | 14604 | 1164 | 15768 |
| candidates after beta gate (S2/S3) | 1746 | 130 | 1876 |
| sent after hysteresis (S5) | 1447 | 118 | 1565 |

## Ablation — alert volume

Counterfactual arms over the identical universe/window. The beta gate is a re-scoring (raw-z → residual-z), not a nested filter, so it both removes and adds alerts; the set differences below isolate each effect.

| arm / set | alerts | alerts/month |
|---|--:|--:|
| NAIVE (\|move\|>5%) | 4164 | 78.57 |
| NO_BETA candidates (raw-z gate, hyst on) | 1225 | 23.11 |
| NO_HYST (all candidates, residual-z, no hyst) | 1876 | 35.40 |
| FULL (sent) | 1565 | 29.53 |
| beta_suppressed = NO_BETA \ FULL (gate removed) | 234 | 4.42 |
| beta_added = FULL \ NO_BETA (gate created) | 574 | 10.83 |
| hyst_suppressed = NO_HYST \ FULL (hyst removed) | 311 | 5.87 |

## Signal quality — signed forward returns (sign = detected direction)

### Sent alerts (FULL)

| class | horizon | n | median | mean | hit rate | P(\|r\|>5%) |
|---|---|--:|--:|--:|--:|--:|
| speculative | J+1 | 1447 | -0.00% | -0.01% | 40% | 35% |
| speculative | J+5 | 1447 | -0.09% | +0.09% | 44% | 57% |
| speculative | J+20 | 1447 | +0.00% | +3.67% | 49% | 68% |
| calm | J+1 | 118 | +0.09% | +0.03% | 52% | 3% |
| calm | J+5 | 118 | -0.27% | +0.02% | 45% | 19% |
| calm | J+20 | 118 | -0.25% | -0.08% | 48% | 47% |
| all | J+1 | 1565 | -0.00% | -0.01% | 41% | 33% |
| all | J+5 | 1565 | -0.09% | +0.09% | 44% | 54% |
| all | J+20 | 1565 | +0.00% | +3.39% | 49% | 66% |

### Beta-suppressed (raw-z fired, beta gate removed)

| class | horizon | n | median | mean | hit rate | P(\|r\|>5%) |
|---|---|--:|--:|--:|--:|--:|
| speculative | J+1 | 192 | +0.00% | +0.08% | 49% | 35% |
| speculative | J+5 | 192 | -0.74% | -1.40% | 45% | 69% |
| speculative | J+20 | 192 | -0.50% | +3.34% | 47% | 80% |
| calm | J+1 | 42 | -0.52% | -0.21% | 38% | 0% |
| calm | J+5 | 42 | -0.54% | -0.37% | 38% | 19% |
| calm | J+20 | 42 | -0.03% | +1.20% | 50% | 52% |
| all | J+1 | 234 | +0.00% | +0.03% | 47% | 29% |
| all | J+5 | 234 | -0.63% | -1.22% | 44% | 60% |
| all | J+20 | 234 | -0.28% | +2.95% | 48% | 75% |

### Beta-added (beta gate fired, raw-z would not)

| class | horizon | n | median | mean | hit rate | P(\|r\|>5%) |
|---|---|--:|--:|--:|--:|--:|
| speculative | J+1 | 510 | +0.00% | -0.28% | 33% | 22% |
| speculative | J+5 | 510 | -0.09% | -0.43% | 38% | 39% |
| speculative | J+20 | 510 | -0.00% | +0.04% | 45% | 50% |
| calm | J+1 | 64 | -0.08% | -0.19% | 48% | 2% |
| calm | J+5 | 64 | -0.75% | -0.41% | 36% | 22% |
| calm | J+20 | 64 | -2.01% | -1.36% | 41% | 47% |
| all | J+1 | 574 | +0.00% | -0.27% | 35% | 20% |
| all | J+5 | 574 | -0.10% | -0.43% | 37% | 37% |
| all | J+20 | 574 | -0.09% | -0.12% | 45% | 49% |

### Dedup-gated candidates (suppressed by hysteresis)

| class | horizon | n | median | mean | hit rate | P(\|r\|>5%) |
|---|---|--:|--:|--:|--:|--:|
| speculative | J+1 | 299 | -0.00% | +0.17% | 35% | 28% |
| speculative | J+5 | 299 | -0.00% | -0.37% | 33% | 42% |
| speculative | J+20 | 299 | -0.00% | +1.82% | 32% | 47% |
| calm | J+1 | 12 | *sample too small* | | | |
| calm | J+5 | 12 | *sample too small* | | | |
| calm | J+20 | 12 | *sample too small* | | | |
| all | J+1 | 311 | -0.00% | +0.15% | 35% | 27% |
| all | J+5 | 311 | -0.00% | -0.36% | 34% | 41% |
| all | J+20 | 311 | -0.00% | +1.76% | 33% | 48% |

### Naive baseline (|move| > 5%)

| class | horizon | n | median | mean | hit rate | P(\|r\|>5%) |
|---|---|--:|--:|--:|--:|--:|
| speculative | J+1 | 4137 | +0.00% | +0.00% | 48% | 41% |
| speculative | J+5 | 4137 | -0.00% | -0.12% | 49% | 71% |
| speculative | J+20 | 4137 | +0.20% | +1.63% | 50% | 86% |
| calm | J+1 | 27 | *sample too small* | | | |
| calm | J+5 | 27 | *sample too small* | | | |
| calm | J+20 | 27 | *sample too small* | | | |
| all | J+1 | 4164 | +0.00% | +0.00% | 48% | 41% |
| all | J+5 | 4164 | -0.09% | -0.12% | 49% | 70% |
| all | J+20 | 4164 | +0.31% | +1.63% | 50% | 86% |

## Baselines — J+5, all tickers

| arm | n | median | mean | hit rate | P(\|r\|>5%) |
|---|--:|--:|--:|--:|--:|
| naive >5% | 4164 | -0.09% | -0.12% | 49% | 70% |
| no-gate (raw \|z\|) | 1225 | -0.20% | +0.01% | 47% | 63% |
| no-hysteresis | 1876 | +0.00% | +0.01% | 42% | 52% |
| full pipeline | 1565 | -0.09% | +0.09% | 44% | 54% |

## Threshold grid — residual-z, hysteresis on (precision/recall)

| threshold | alerts/month | J+5 hit rate | J+5 median signed |
|--:|--:|--:|--:|
| 1.5 | 59.38 | 46% | -0.04% |
| 2.0 | 39.17 | 45% | -0.09% |
| 2.5 | 28.83 | 44% | -0.09% |
| 3.0 | 23.58 | 44% | -0.07% |
| 3.5 | 20.36 | 44% | -0.09% |

## Caveats

- **Selection bias**: the ticker universe was hand-picked in the present with knowledge of which names became interesting. Absolute levels overstate performance. All four arms share the identical universe/window, so between-arm DELTAS are the valid readout; absolute hit rates are not a performance claim.
- **Sample size**: any cell with n<30 is labelled *sample too small* and shows no percentages. The `calm` class is only 2 tickers (MMED, XYL) and MMED has <90 bars — calm cells are near-empty by construction; the speculative class carries the signal.
- **Multiple comparisons**: the threshold grid reports 5 thresholds; the best-looking cell is optimistically biased. Do not read the max as the expected out-of-sample value.
- **Survivorship**: quarantine is currently empty, but yfinance returns only live symbols — any renamed/delisted ticker returns empty and is silently absent. Recent IPOs (GRO 2024-11, MMED 2026-03) contribute only from their inception + beta-gate warmup.
- **What this measures**: attention/anomaly detection and the short-horizon signed drift that follows it. What it does NOT measure: tradability, transaction costs, slippage, borrow, capacity, or a live trading edge. Direction is a z-sign proxy, not a forecast.
- **Determinism**: deterministic given the fetched prices. yfinance auto-adjusted closes can drift as new corporate actions post; re-fetching later may move marginal cells. The committed docs/data/backtest_<date>.json is the pinned snapshot.
- **Beta gate is not a separate filter**: it is the residual-z computation itself. 'Beta-gated candidates' are reconstructed as alerts(NO_BETA) \ alerts(FULL) — raw-z crossers the residualization killed. NO_BETA is FULL with return_robust_z substituted for z_resid, everything else identical, so the difference isolates the beta gate.
- **Prod-safety**: ephemeral dedup state in a temp dir; no prod state file, runs.jsonl, outcomes.jsonl or LLM is ever touched. ANOMALY_DEDUP_READONLY is intentionally unset (it would freeze hysteresis and void the NO_HYST contrast).
