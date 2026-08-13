"""Epic 4 Sprint 2 — Onboarding assisté d'un nouveau ticker.

Couvre les acceptance criteria côté module :
  - nouveau ticker classé speculative + single_factor, symbole validé avant écriture ;
  - registry_check passe immédiatement après l'onboard ;
  - symbole invalide → refus propre, aucun fichier modifié ;
  - idempotence (double onboard n'entraîne aucune duplication).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from market_intelligence import registry_check, ticker_onboard
from market_intelligence.ticker_onboard import (
    ALREADY_PRESENT,
    INVALID,
    NOT_PRESENT,
    OFFBOARDED,
    ONBOARDED,
    format_result,
    offboard_ticker,
    onboard_ticker,
)


@dataclass(frozen=True)
class _FakeValidation:
    status: str
    actual_name: str = ""
    reason: str = ""


@pytest.fixture
def data_files(tmp_path: Path):
    """Référentiels tmp avec un ticker existant cohérent (OLD).

    Depuis l'Epic 10 S4, trois fichiers d'ÉTAT (registre, classifications, liste à
    facteur unique) et un fichier de CONFIGURATION en lecture seule (la table des
    facteurs sectoriels), qu'aucun chemin automatique ne doit réécrire.
    """
    registry = tmp_path / "registry.json"
    classifications = tmp_path / "classifications.json"
    single_factors = tmp_path / "single_factor_symbols.json"
    sector_map = tmp_path / "sector_factors.json"
    registry.write_text(json.dumps({
        "portfolio_tickers": [{"symbol": "OLD", "api_symbol": "OLD", "expected_name": "Old Co"}],
        "macro_tickers": [],
        "factor_tickers": [],
        "alias_map": {},
    }), encoding="utf-8")
    classifications.write_text(
        json.dumps({"classifications": {"OLD": "calm"}}), encoding="utf-8"
    )
    single_factors.write_text(json.dumps({"single_factor_symbols": []}), encoding="utf-8")
    sector_map.write_text(json.dumps({
        "market_factor": "IWM",
        "correlation_threshold": 0.35,
        "sector_factors": {"OLD": ["XLK"]},
    }), encoding="utf-8")
    return registry, classifications, single_factors, sector_map


def _paths(data_files):
    """Chemins d'onboarding : les trois fichiers d'état + la config en lecture."""
    reg, cls, single, sector = data_files
    return {
        "registry_path": reg,
        "classifications_path": cls,
        "single_factors_path": single,
        "sector_factors_path": sector,
    }


def _state_paths(data_files):
    """Chemins d'offboarding : l'état seul — la config n'est jamais réécrite."""
    return {k: v for k, v in _paths(data_files).items() if k != "sector_factors_path"}


def _ok(monkeypatch, name="New Corp"):
    monkeypatch.setattr(
        ticker_onboard, "_validate_symbol",
        lambda sym: _FakeValidation(status="ok", actual_name=name),
    )


# --- symbole valide -------------------------------------------------------


def test_onboard_valid_generates_all_entries(monkeypatch, data_files):
    _ok(monkeypatch)
    reg, thr, fac, sector = data_files
    result = onboard_ticker("nvda", **_paths(data_files))

    assert result.status == ONBOARDED
    assert result.symbol == "NVDA"
    assert len(result.generated) == 3

    registry = json.loads(reg.read_text())
    entry = next(t for t in registry["portfolio_tickers"] if t["symbol"] == "NVDA")
    assert entry == {"symbol": "NVDA", "api_symbol": "NVDA", "expected_name": "New Corp"}
    assert json.loads(thr.read_text())["classifications"]["NVDA"] == "speculative"
    assert "NVDA" in json.loads(fac.read_text())["single_factor_symbols"]


def test_onboard_defaults_speculative_single_factor_never_maps(monkeypatch, data_files):
    _ok(monkeypatch)
    reg, thr, fac, sector = data_files
    onboard_ticker("NVDA", **_paths(data_files))

    assert "NVDA" in json.loads(fac.read_text())["single_factor_symbols"]
    # jamais de mapping ETF deviné — et la config n'est même pas ouverte en écriture
    assert "NVDA" not in json.loads(sector.read_text())["sector_factors"]
    assert json.loads(thr.read_text())["classifications"]["NVDA"] == "speculative"


def test_registry_check_passe_apres_onboard(monkeypatch, data_files):
    _ok(monkeypatch)
    reg, thr, fac, sector = data_files
    onboard_ticker("NVDA", **_paths(data_files))

    issues = registry_check.evaluate(
        {"portfolio.json": ["NVDA"]},
        registry_check.load_registry_symbols(reg),
        registry_check.load_classified_symbols(thr),
        registry_check.load_factor_covered_symbols(sector, fac),
    )
    assert [i for i in issues if i.severity == registry_check.BLOCKING] == []


# --- symbole invalide -----------------------------------------------------


def test_onboard_invalid_ne_modifie_aucun_fichier(monkeypatch, data_files):
    monkeypatch.setattr(
        ticker_onboard, "_validate_symbol",
        lambda sym: _FakeValidation(status="not_found", reason="No name"),
    )
    reg, thr, fac, sector = data_files
    before = {p: p.read_text() for p in (reg, thr, fac)}

    result = onboard_ticker("FAKE", **_paths(data_files))

    assert result.status == INVALID
    assert "invalide" in result.reason.lower()
    for p, content in before.items():
        assert p.read_text() == content  # aucun octet modifié


def test_symbole_valide_avant_ecriture(monkeypatch, data_files):
    calls = []
    monkeypatch.setattr(
        ticker_onboard, "_validate_symbol",
        lambda sym: calls.append(sym) or _FakeValidation(status="ok", actual_name="X"),
    )
    onboard_ticker("ABC", **_paths(data_files))
    assert calls == ["ABC"]  # validation appelée exactement une fois, en amont


# --- idempotence ----------------------------------------------------------


def test_onboard_idempotent(monkeypatch, data_files):
    _ok(monkeypatch)
    reg, thr, fac, sector = data_files
    onboard_ticker("NVDA", **_paths(data_files))
    snapshot = {p: p.read_text() for p in (reg, thr, fac)}

    result2 = onboard_ticker("NVDA", **_paths(data_files))

    assert result2.status == ALREADY_PRESENT
    for p, content in snapshot.items():
        assert p.read_text() == content  # 2e passe: rien réécrit
    registry = json.loads(reg.read_text())
    assert sum(1 for t in registry["portfolio_tickers"] if t["symbol"] == "NVDA") == 1
    assert json.loads(fac.read_text())["single_factor_symbols"].count("NVDA") == 1


# --- rendu ----------------------------------------------------------------


def test_format_result_variants(monkeypatch, data_files):
    _ok(monkeypatch)
    onboarded = onboard_ticker("NVDA", **_paths(data_files))
    text = format_result(onboarded)
    assert "NVDA" in text and "single_factor" in text and "manuellement" in text.lower()

    already = onboard_ticker("NVDA", **_paths(data_files))
    assert "déjà" in format_result(already).lower()

    invalid = ticker_onboard.OnboardResult(symbol="Z", status=INVALID, reason="bad")
    assert "refusé" in format_result(invalid).lower()


# --- offboarding (retrait des référentiels) --------------------------------


def test_offboard_retire_toutes_les_entrees(monkeypatch, data_files):
    _ok(monkeypatch)
    reg, thr, fac, sector = data_files
    onboard_ticker("NVDA", **_paths(data_files))

    result = offboard_ticker("nvda", **_state_paths(data_files))

    assert result.status == OFFBOARDED
    assert result.symbol == "NVDA"
    assert len(result.generated) == 3
    registry = json.loads(reg.read_text())
    assert all(t["symbol"] != "NVDA" for t in registry["portfolio_tickers"])
    assert "NVDA" not in json.loads(thr.read_text())["classifications"]
    assert "NVDA" not in json.loads(fac.read_text())["single_factor_symbols"]


def test_offboard_ne_touche_jamais_la_config_sectorielle(data_files):
    """Epic 10 S4 : l'offboarding retire l'état, jamais la configuration.

    OLD a un mapping ETF (`sector_factors`), qui est une décision prise en PR. Le
    retirer automatiquement rendrait l'invariant de configuration rouge au premier
    ticker sorti de cohorte. Un mapping sans entrée de registre est inerte : il dit
    seulement quel facteur *s'appliquerait* si le titre revenait.
    """
    reg, thr, fac, sector = data_files
    config_before = sector.read_text()

    result = offboard_ticker("OLD", **_state_paths(data_files))

    assert result.status == OFFBOARDED
    assert all(t["symbol"] != "OLD" for t in json.loads(reg.read_text())["portfolio_tickers"])
    assert "OLD" not in json.loads(thr.read_text())["classifications"]
    assert sector.read_text() == config_before  # tolérance nulle sur la config


def test_offboard_absent_ne_touche_rien(data_files):
    reg, thr, fac, sector = data_files
    snapshot = (reg.read_text(), thr.read_text(), fac.read_text())

    result = offboard_ticker("GHOST", **_state_paths(data_files))

    assert result.status == NOT_PRESENT
    assert result.generated == ()
    assert (reg.read_text(), thr.read_text(), fac.read_text()) == snapshot
    assert "rien à retirer" in format_result(result)
