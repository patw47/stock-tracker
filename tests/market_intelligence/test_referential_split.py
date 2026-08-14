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

import importlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from market_intelligence import registry_check, ticker_onboard, v5_bridge


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


def test_check_referential_passes_on_a_bare_machine_but_still_guards_a_real_one(
    bare_machine, tmp_path, capsys
):
    """Sans état : rien à vérifier, exit 0. Avec un état incohérent : toujours bloquant."""
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps({"tickers": [{"symbol": "BBAI"}]}), encoding="utf-8")
    watchlist = tmp_path / "watchlist.json"
    watchlist.write_text(json.dumps({"tickers": []}), encoding="utf-8")

    assert registry_check.run_check(portfolio, watchlist) == 0
    assert "aucun état de référentiel" in capsys.readouterr().out

    # Le garde-fou n'a pas disparu : un état PRÉSENT mais incohérent reste bloquant.
    registry_check.REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    registry_check.REGISTRY_PATH.write_text(
        json.dumps({"portfolio_tickers": []}), encoding="utf-8"
    )
    assert registry_check.run_check(portfolio, watchlist) == 1
    assert "BBAI" in capsys.readouterr().out


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


def test_no_deploy_unit_commits_the_referential_any_more():
    """L'état vit hors de git, et plus aucune unité ne l'y remet.

    Avant l'Epic 10 S4, ``referential-sync.path`` surveillait
    ``market_intelligence/data/`` pour committer les ajouts faits depuis Telegram.
    Le S4 a déplacé exactement ces fichiers hors du dossier surveillé ; l'unité,
    devenue muette, a été supprimée à la clôture de l'epic (dette 2). Ce test tient
    les deux moitiés de la garantie : l'état est bien sous ``runtime/``, et rien
    dans ``deploy/`` ne rejoue un commit automatique du référentiel.
    """
    deploy_dir = Path(__file__).resolve().parents[2] / "deploy"
    assert not list(deploy_dir.glob("referential-sync.*"))
    for script in deploy_dir.glob("*.sh"):
        assert "git add market_intelligence/data" not in script.read_text(encoding="utf-8")

    state_dir = registry_check.STATE_DIR.resolve()
    for path in (
        registry_check.REGISTRY_PATH,
        registry_check.CLASSIFICATIONS_PATH,
        registry_check.SINGLE_FACTORS_PATH,
    ):
        assert path.resolve().is_relative_to(state_dir)

    assert "runtime" in registry_check.STATE_DIR.parts  # l'état est ailleurs
    # La moitié configuration, elle, reste versionnée à sa place.
    assert registry_check.ALERT_THRESHOLDS_PATH.parent.name == "data"
    assert registry_check.SECTOR_FACTORS_PATH.parent.name == "data"


def test_migration_carries_the_state_and_leaves_the_configuration_behind(tmp_path, monkeypatch):
    """Dette Epic 10 S4 : le script de migration ne recopie plus les clés inertes.

    L'ancien ``registry.json`` mêlait les deux moitiés. Copié tel quel, il produisait
    un état porteur de ``macro_tickers``/``factor_tickers``/``alias_map`` que plus
    aucun lecteur ne lit à cet endroit — deux formes du même fichier, dont l'une
    éditable sans le moindre effet.
    """
    migrate = importlib.import_module("scripts.migrate_referential_state")

    legacy = tmp_path / "registry.json"
    legacy.write_text(json.dumps({
        "portfolio_tickers": [{"symbol": "BBAI", "api_symbol": "BBAI"}],
        "macro_tickers": [{"symbol": "IWM", "api_symbol": "IWM"}],
        "factor_tickers": [{"symbol": "XLK", "api_symbol": "XLK"}],
        "alias_map": {"DXY": "DX-Y.NYB"},
    }), encoding="utf-8")

    state = tmp_path / "runtime" / "registry.json"
    monkeypatch.setattr(migrate, "LEGACY_REGISTRY", legacy)
    monkeypatch.setattr(migrate, "REGISTRY_PATH", state)
    monkeypatch.setattr(migrate, "CLASSIFICATIONS_PATH", tmp_path / "runtime" / "classifications.json")
    monkeypatch.setattr(migrate, "SINGLE_FACTORS_PATH", tmp_path / "runtime" / "single_factor_symbols.json")
    monkeypatch.setattr(migrate, "ALERT_THRESHOLDS_PATH", tmp_path / "alert_thresholds.json")
    monkeypatch.setattr(migrate, "SECTOR_FACTORS_PATH", tmp_path / "sector_factors.json")

    assert migrate.migrate() == 0

    migrated = json.loads(state.read_text(encoding="utf-8"))
    assert migrated == {"portfolio_tickers": [{"symbol": "BBAI", "api_symbol": "BBAI"}]}
    for key in ("macro_tickers", "factor_tickers", "alias_map"):
        assert key not in migrated
    # Non destructif : l'ancien fichier garde tout, la config reste récupérable.
    assert "alias_map" in json.loads(legacy.read_text(encoding="utf-8"))
