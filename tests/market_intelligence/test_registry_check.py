"""Epic 4 Sprint 1 — Validateur de cohérence des référentiels.

Couvre les acceptance criteria :
  - ticker runtime absent du registry → exit != 0, message explicite ;
  - classification manquante et mapping facteur manquant détectés ;
  - l'état actuel du repo (fichiers .example) passe la validation ;
  - le validateur signale (exit != 0) une divergence des fichiers runtime.
"""
from __future__ import annotations

import json
from pathlib import Path

from market_intelligence import registry_check
from market_intelligence.registry_check import BLOCKING, INFO, Issue, evaluate

# Référentiels de test partagés : 2 tickers cohérents (AAA, BBB).
REGISTRY = {"AAA", "BBB"}
CLASSIFIED = {"AAA", "BBB"}
FACTORS = {"AAA", "BBB"}


def _severities(issues: list[Issue], symbol: str) -> list[str]:
    return [i.severity for i in issues if i.symbol == symbol]


def _messages(issues: list[Issue]) -> str:
    return "\n".join(i.message for i in issues)


# --- evaluate() : logique pure -------------------------------------------


def test_ticker_absent_du_registry_est_bloquant():
    issues = evaluate({"portfolio.json": ["ZZZ"]}, REGISTRY, CLASSIFIED | {"ZZZ"}, FACTORS | {"ZZZ"})
    assert _severities(issues, "ZZZ") == [BLOCKING]
    assert "registry.json" in _messages(issues)
    assert "portfolio.json" in _messages(issues)


def test_watchlist_hors_registre_est_info_tier_tension():
    # Watchlist = tier tension (Layer C) : hors registre → info, jamais bloquant.
    issues = evaluate(
        {"watchlist.json": ["ZZZ"]}, REGISTRY, CLASSIFIED, FACTORS,
        light_labels=frozenset({"watchlist.json"}),
    )
    assert _severities(issues, "ZZZ") == [INFO]
    assert "tension" in _messages(issues)
    assert not [i for i in issues if i.severity == BLOCKING]


def test_classification_manquante_est_bloquant():
    issues = evaluate({"portfolio.json": ["AAA"]}, REGISTRY, set(), FACTORS)
    blocking = [i for i in issues if i.severity == BLOCKING]
    assert len(blocking) == 1
    assert "alert_thresholds.json" in blocking[0].message


def test_mapping_facteur_manquant_est_bloquant():
    issues = evaluate({"portfolio.json": ["AAA"]}, REGISTRY, CLASSIFIED, set())
    blocking = [i for i in issues if i.severity == BLOCKING]
    assert len(blocking) == 1
    assert "sector_factors.json" in blocking[0].message


def test_tout_coherent_ne_produit_aucun_probleme():
    issues = evaluate({"portfolio.json": ["AAA", "BBB"]}, REGISTRY, CLASSIFIED, FACTORS)
    assert issues == []


def test_entree_registry_orpheline_est_info_non_bloquant():
    # BBB dans le registry mais absent des listes runtime → info seulement.
    issues = evaluate({"portfolio.json": ["AAA"]}, REGISTRY, CLASSIFIED, FACTORS)
    assert _severities(issues, "BBB") == [INFO]
    assert not [i for i in issues if i.severity == BLOCKING]


def test_ticker_cumule_les_trois_manques():
    issues = evaluate({"portfolio.json": ["NEW"]}, REGISTRY, CLASSIFIED, FACTORS)
    assert _severities(issues, "NEW") == [BLOCKING, BLOCKING, BLOCKING]


# --- run_check() : exit codes + I/O fichiers -----------------------------


def _write(path: Path, symbols: list[str]) -> None:
    path.write_text(json.dumps({"tickers": [{"symbol": s} for s in symbols]}), encoding="utf-8")


def test_run_check_repo_examples_passe():
    # Sans arguments → résout portfolio.json|.example + watchlist.json|.example du repo.
    assert registry_check.run_check() == 0


def test_run_check_exit_non_zero_si_divergence(tmp_path, capsys):
    portfolio = tmp_path / "portfolio.json"
    watchlist = tmp_path / "watchlist.json"
    _write(portfolio, ["FAKE_TICKER"])   # diverge : inconnu partout → bloquant
    _write(watchlist, [])

    rc = registry_check.run_check(portfolio, watchlist)
    out = capsys.readouterr().out

    assert rc == 1
    assert "[BLOCKING]" in out
    assert "FAKE_TICKER" in out
    assert "registry.json" in out


def test_run_check_watchlist_hors_registre_ne_bloque_pas(tmp_path, capsys):
    # Le cas VPS réel : watchlist massive hors registre → tier tension, exit 0.
    portfolio = tmp_path / "portfolio.json"
    watchlist = tmp_path / "watchlist.json"
    _write(portfolio, ["BBAI"])
    _write(watchlist, ["NVDA", "TSLA", "FAKE_TICKER"])

    rc = registry_check.run_check(portfolio, watchlist)
    out = capsys.readouterr().out

    assert rc == 0
    assert "[BLOCKING]" not in out
    assert "tension" in out


def test_run_check_exit_zero_si_runtime_coherent(tmp_path):
    # Un sous-ensemble strict des tickers réels du repo reste cohérent.
    portfolio = tmp_path / "portfolio.json"
    watchlist = tmp_path / "watchlist.json"
    _write(portfolio, ["BBAI", "HIMS"])
    _write(watchlist, ["SMR"])
    assert registry_check.run_check(portfolio, watchlist) == 0


def test_main_retourne_exit_code(tmp_path):
    portfolio = tmp_path / "portfolio.json"
    watchlist = tmp_path / "watchlist.json"
    _write(portfolio, ["INCONNU"])
    _write(watchlist, [])
    rc = registry_check.main(["--portfolio", str(portfolio), "--watchlist", str(watchlist)])
    assert rc == 1
