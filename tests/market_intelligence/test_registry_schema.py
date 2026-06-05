from __future__ import annotations

from market_intelligence.registry_schema import load_registry


def test_registry_contains_sprint_6_macro_tickers() -> None:
    registry = load_registry()
    symbols = {entry.symbol for entry in registry.macro_tickers}

    assert {"IWM", "^TNX", "OIL"}.issubset(symbols)
    assert registry.resolve_api_symbol("OIL") == "CL=F"
    assert registry.resolve_api_symbol("DXY") == "DX-Y.NYB"
