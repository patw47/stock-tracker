.PHONY: results results-check test lint validate-json check-referential test-deploy check-config-invariant

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

check-referential:
	python3 -m market_intelligence.registry_check

# Invariant de migration (Epic 10 S4) : la moitie CONFIGURATION du referentiel
# (seuils + table des facteurs sectoriels) doit etre identique a sa reference
# figee avant le deplacement. Tolerance nulle. Ne porte jamais sur l'etat, qui
# change tous les jours par construction.
check-config-invariant:
	python3 scripts/check_config_invariant.py

# Verdict de deploiement (deploy/deploy_verdict.py), testable sans VPS.
test-deploy:
	python3 -m pytest -q tests/deploy


# Regenerate docs/RESULTS.md + docs/data/backtest_<date>.json from scratch.
# Deterministic given fetched prices, read-only, no prod state, no LLM.
# Optional: FRAMES_CACHE=path to pin/reuse a fetched-price snapshot.
results:
	python3 scripts/generate_results.py $(if $(FRAMES_CACHE),--frames-cache $(FRAMES_CACHE),)

# Invariant asserts (aggregation, return sign, set differences). No network.
results-check:
	python3 scripts/generate_results.py --self-check
