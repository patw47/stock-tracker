.PHONY: results results-check

# Regenerate docs/RESULTS.md + docs/data/backtest_<date>.json from scratch.
# Deterministic given fetched prices, read-only, no prod state, no LLM.
# Optional: FRAMES_CACHE=path to pin/reuse a fetched-price snapshot.
results:
	python3 scripts/generate_results.py $(if $(FRAMES_CACHE),--frames-cache $(FRAMES_CACHE),)

# Invariant asserts (aggregation, return sign, set differences). No network.
results-check:
	python3 scripts/generate_results.py --self-check
