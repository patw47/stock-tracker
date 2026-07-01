"""Regression guard for PM-0001: sector-factor ETFs must be fetchable.

The beta gate neutralises portfolio names against sector-factor ETFs listed in
sector_factors.json. Those ETFs are only fetched if they belong to the registry's
fetch set (all_tickers). They must NOT be portfolio tickers (else they'd generate
alerts) nor macro tickers (else they'd pollute the macro snapshot completeness check).
"""

from __future__ import annotations

import json
from pathlib import Path

from market_intelligence.registry_schema import load_registry

_DATA = Path(__file__).parent.parent.parent / "market_intelligence" / "data"
_SECTOR_FACTORS = _DATA / "sector_factors.json"


def test_factor_tickers_loaded() -> None:
    registry = load_registry()
    symbols = {entry.symbol for entry in registry.factor_tickers}
    expected = {"NUKZ", "QTUM", "PHO", "FIW", "MOO", "IHI", "XLV", "XBI", "BOTZ", "KARS"}
    assert expected <= symbols


def test_factors_fetchable_but_not_alertable() -> None:
    registry = load_registry()
    fetchable = {entry.symbol for entry in registry.all_tickers()}
    portfolio = {entry.symbol for entry in registry.portfolio_tickers}
    macro = {entry.symbol for entry in registry.macro_tickers}
    for symbol in ("XLV", "XBI", "NUKZ", "BOTZ"):
        assert symbol in fetchable  # fetched → factor frame available
        assert symbol not in portfolio  # never a candidate alert
        assert symbol not in macro  # never in the macro snapshot check


def test_every_referenced_sector_factor_is_fetchable() -> None:
    """Every ETF referenced by the beta gate config must be in the fetch set."""
    registry = load_registry()
    fetchable = {entry.symbol for entry in registry.all_tickers()}
    config = json.loads(_SECTOR_FACTORS.read_text(encoding="utf-8"))
    referenced = {config["market_factor"]}
    for factors in config["sector_factors"].values():
        referenced.update(factors)
    missing = referenced - fetchable
    assert not missing, f"factor ETFs referenced but not fetchable: {sorted(missing)}"
