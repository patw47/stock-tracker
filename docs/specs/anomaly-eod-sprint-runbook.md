# Anomaly EOD — Sprint Runbook

## Rule

The full specification is context, not permission to implement everything.

Each Codex run must target exactly one sprint.

## Standard workflow

1. Start from updated `main`.
2. Create a sprint branch.
3. Read:
   - `docs/specs/anomaly-eod-detection.md`
   - `docs/specs/anomaly-eod-implementation-status.md`
   - existing S0 modules
4. Produce a short plan.
5. Map acceptance criteria to files/tests.
6. Implement only the target sprint.
7. Run pytest.
8. Update `anomaly-eod-implementation-status.md`.
9. Open PR.

## Branch naming

- `feat/anomaly-eod-s1-signals`
- `feat/anomaly-eod-s2-beta-gate`
- `feat/anomaly-eod-s3-alert-thresholds`
- etc.

## Prompt invariant

Use this sentence in every Codex prompt:

“S0 is already implemented. Do not reimplement Sprint 0. Consume the existing S0 foundation and implement only TARGET_SPRINT.”

## Current next sprint

TARGET_SPRINT: S5 — Dédup (machine à états hystérésis)

S5 must not:
- call Warren
- call Telegram
- implement MacroSnapshot
- rewrite candidate alerts
- rewrite short interest
- rewrite registry/fetch/normalization foundation
