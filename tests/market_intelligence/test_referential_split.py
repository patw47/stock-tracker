"""Epic 10 Sprint 4 — l'état du référentiel sort de git.

Deux garanties, une par critère d'acceptance :
  - une machine sans aucun fichier d'état démarre (configuration valide, état vide,
    reconstruction au premier passage du pont) ;
  - le chemin surveillé par la path unit de synchronisation ne contient plus les
    fichiers d'état.

Piège de chemin : les fichiers d'état sont désormais gitignorés, donc aucun de ces
tests ne peut s'appuyer sur ``git diff`` — il serait vide par construction, donc
vrai par vacuité. Ce qui est vérifié ici l'est par lecture directe des fichiers.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from market_intelligence import registry_check, ticker_onboard, v5_bridge

_UNIT = Path(__file__).resolve().parents[2] / "deploy" / "referential-sync.path"


@dataclass(frozen=True)
class _FakeValidation:
    status: str
    actual_name: str = ""
    reason: str = ""


@pytest.fixture
def bare_machine(tmp_path, monkeypatch):
    """Une machine fraîche : la configuration existe, aucun fichier d'état.

    C'est le scénario de reprise — déploiement sur une machine neuve, ou VPS dont
    le disque d'état a été perdu. Il doit être testé, pas supposé.
    """
    config = tmp_path / "sector_factors.json"
    config.write_text(
        json.dumps({
            "market_factor": "IWM",
            "correlation_threshold": 0.35,
            "sector_factors": {},
        }),
        encoding="utf-8",
    )
    state = tmp_path / "runtime" / "referential"  # volontairement PAS créé
    for module in (registry_check, v5_bridge, ticker_onboard):
        monkeypatch.setattr(module, "REGISTRY_PATH", state / "registry.json", raising=False)
        monkeypatch.setattr(
            module, "CLASSIFICATIONS_PATH", state / "classifications.json", raising=False
        )
        monkeypatch.setattr(
            module, "SINGLE_FACTORS_PATH", state / "single_factor_symbols.json",
            raising=False,
        )
        monkeypatch.setattr(module, "SECTOR_FACTORS_PATH", config, raising=False)
    monkeypatch.setattr(
        ticker_onboard, "_validate_symbol",
        lambda sym: _FakeValidation(status="ok", actual_name=f"{sym} Inc"),
    )
    return state


def test_a_machine_without_state_loads_config_and_reads_empty(bare_machine):
    """La configuration se charge, l'état est vide — aucune exception."""
    assert not bare_machine.exists()

    # Configuration : lue depuis son emplacement versionné, intacte.
    factors = json.loads(
        (registry_check.SECTOR_FACTORS_PATH).read_text(encoding="utf-8")
    )
    assert factors["market_factor"] == "IWM"

    # État : vide, pas une erreur.
    assert registry_check.load_registry_symbols() == set()
    assert registry_check.load_classified_symbols() == set()
    assert registry_check.load_factor_covered_symbols(
        registry_check.SECTOR_FACTORS_PATH, registry_check.SINGLE_FACTORS_PATH
    ) == set()


def test_a_reconciliation_rebuilds_the_state_from_nothing(bare_machine, tmp_path):
    """Premier passage du pont sur une machine nue : l'état se reconstruit."""
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps({"tickers": []}), encoding="utf-8")
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps({"tickers": []}), encoding="utf-8")

    result = v5_bridge.reconcile(
        {"ATNF": {"days_held": 3}},
        watchlist_path=watchlist,
        portfolio_path=portfolio,
    )

    assert result["added"] == ["ATNF"]
    assert result["referential"]["onboarded"] == 1
    # Les trois fichiers d'état existent maintenant, sous le nouvel emplacement.
    assert registry_check.load_registry_symbols() == {"ATNF"}
    assert registry_check.load_classified_symbols() == {"ATNF"}
    assert "ATNF" in registry_check.load_factor_covered_symbols(
        registry_check.SECTOR_FACTORS_PATH, registry_check.SINGLE_FACTORS_PATH
    )


def test_the_sync_path_unit_no_longer_covers_the_state_files():
    """Le chemin surveillé ne contient plus l'état — lecture de l'unité et du chemin."""
    watched = next(
        line.split("=", 1)[1].strip()
        for line in _UNIT.read_text(encoding="utf-8").splitlines()
        if line.startswith("PathChanged=")
    )

    state_dir = registry_check.STATE_DIR.resolve()
    for path in (
        registry_check.REGISTRY_PATH,
        registry_check.CLASSIFICATIONS_PATH,
        registry_check.SINGLE_FACTORS_PATH,
    ):
        assert path.resolve().is_relative_to(state_dir)
        # Le chemin surveillé est celui du VPS ; on compare les suffixes de dépôt.
        assert Path(watched).name not in path.parts

    # Ce qui reste surveillé est bien la moitié configuration, et elle existe.
    assert watched.endswith("market_intelligence/data")
    assert registry_check.ALERT_THRESHOLDS_PATH.parent.name == "data"
    assert registry_check.SECTOR_FACTORS_PATH.parent.name == "data"
    assert "runtime" in registry_check.STATE_DIR.parts  # l'état est ailleurs
