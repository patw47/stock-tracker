from __future__ import annotations

import pandas as pd
import pytest

from market_intelligence.registry_schema import Registry, TickerEntry


@pytest.fixture
def minimal_registry():
    return Registry(
        portfolio_tickers=(
            TickerEntry(symbol="AAPL", api_symbol="AAPL", expected_name="Apple"),
            TickerEntry(symbol="FAKE", api_symbol="FAKE", expected_name="Fake Corp"),
        ),
        macro_tickers=(
            TickerEntry(symbol="^VIX", api_symbol="^VIX", expected_name="CBOE Volatility"),
        ),
        alias_map={"DXY": "DX-Y.NYB"},
    )


@pytest.fixture
def sample_ohlcv_df():
    dates = pd.date_range("2024-01-01", periods=65, freq="B")
    return pd.DataFrame(
        {
            "Open": 100.0,
            "High": 105.0,
            "Low": 95.0,
            "Close": 102.0,
            "Volume": 1_000_000,
        },
        index=dates,
    )


@pytest.fixture
def short_ohlcv_df():
    dates = pd.date_range("2024-01-01", periods=30, freq="B")
    return pd.DataFrame(
        {
            "Open": 100.0,
            "High": 105.0,
            "Low": 95.0,
            "Close": 102.0,
            "Volume": 1_000_000,
        },
        index=dates,
    )


@pytest.fixture
def empty_ohlcv_df():
    return pd.DataFrame()
