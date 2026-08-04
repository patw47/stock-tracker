.PHONY: results results-check test lint validate-json

# Entrypoints de verification : la CI appelle ces cibles, pas les outils
# directement, pour que poste local et CI executent strictement la meme chose.
test:
	python3 -m pytest -q

lint:
	python3 -m ruff check .

validate-json:
	python3 -m json.tool workflow.json > /dev/null
	python3 -m json.tool portfolio.example.json > /dev/null
	python3 -m json.tool watchlist.example.json > /dev/null
	@echo "All JSON files valid."


# Regenerate docs/RESULTS.md + docs/data/backtest_<date>.json from scratch.
# Deterministic given fetched prices, read-only, no prod state, no LLM.
# Optional: FRAMES_CACHE=path to pin/reuse a fetched-price snapshot.
results:
	python3 scripts/generate_results.py $(if $(FRAMES_CACHE),--frames-cache $(FRAMES_CACHE),)

# Invariant asserts (aggregation, return sign, set differences). No network.
results-check:
	python3 scripts/generate_results.py --self-check
