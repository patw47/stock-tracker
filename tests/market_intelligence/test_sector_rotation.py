from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from market_intelligence.sector_rotation import (
    SectorRotationResult,
    get_sector_rotation,
)


def _make_close_df(tickers: list[str], rows: list[list[float]]) -> pd.DataFrame:
    """Build a Close DataFrame matching yfinance multi-ticker shape."""
    dates = pd.date_range("2024-01-01", periods=len(rows), freq="B")
    return pd.DataFrame(rows, index=dates, columns=tickers)


def _make_hist(close_df: pd.DataFrame) -> pd.DataFrame:
    """Wrap a Close DataFrame into a MultiIndex hist DataFrame."""
    from pandas import MultiIndex

    close_df = close_df.copy()
    close_df.columns = MultiIndex.from_tuples(
        [("Close", c) for c in close_df.columns]
    )
    return close_df


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_sector_rotation_returns_entering_and_exiting():
    # SPY flat (+0%), IWM +0.5%, XLK +1%, XLE -1%, XLF +0.2%
    tickers = ["SPY", "IWM", "XLK", "XLE", "XLF"]
    rows = [
        [100.0, 50.0, 200.0, 60.0, 30.0],
        [100.0, 50.25, 202.0, 59.4, 30.06],  # day 2: SPY flat, IWM +0.5%, XLK +1%, XLE -1%, XLF +0.2%
    ]
    close_df = _make_close_df(tickers, rows)
    hist = _make_hist(close_df)

    with patch("market_intelligence.sector_rotation.yf") as mock_yf:
        mock_yf.download.return_value = hist
        result = get_sector_rotation()

    assert isinstance(result, SectorRotationResult)
    entering_tickers = [s.ticker for s in result.entering]
    exiting_tickers = [s.ticker for s in result.exiting]
    # XLK rel_1d ~ +1.0, XLF rel_1d ~ +0.2, XLE rel_1d ~ -1.0
    assert "XLK" in entering_tickers
    assert "XLE" in exiting_tickers


def test_sector_rotation_iwm_surperformance():
    # SPY flat, IWM +0.8% → IWM_rel_1d ~ +0.8 > threshold 0.3
    tickers = ["SPY", "IWM", "XLK"]
    rows = [
        [100.0, 50.0, 200.0],
        [100.0, 50.4, 200.0],
    ]
    close_df = _make_close_df(tickers, rows)
    hist = _make_hist(close_df)

    with patch("market_intelligence.sector_rotation.yf") as mock_yf:
        mock_yf.download.return_value = hist
        result = get_sector_rotation()

    assert result.small_caps_trend == "surperformance"
    assert result.iwm_rel_1d is not None
    assert result.iwm_rel_1d > 0


def test_sector_rotation_iwm_sous_performance():
    # IWM -0.8% vs SPY flat → sous-performance
    tickers = ["SPY", "IWM", "XLK"]
    rows = [
        [100.0, 50.0, 200.0],
        [100.0, 49.6, 200.0],
    ]
    close_df = _make_close_df(tickers, rows)
    hist = _make_hist(close_df)

    with patch("market_intelligence.sector_rotation.yf") as mock_yf:
        mock_yf.download.return_value = hist
        result = get_sector_rotation()

    assert result.small_caps_trend == "sous-performance"


def test_sector_rotation_iwm_neutre():
    # IWM +0.1% vs SPY flat → neutre (below threshold 0.3)
    tickers = ["SPY", "IWM", "XLK"]
    rows = [
        [100.0, 50.0, 200.0],
        [100.0, 50.05, 200.0],
    ]
    close_df = _make_close_df(tickers, rows)
    hist = _make_hist(close_df)

    with patch("market_intelligence.sector_rotation.yf") as mock_yf:
        mock_yf.download.return_value = hist
        result = get_sector_rotation()

    assert result.small_caps_trend == "neutre"


def test_sector_rotation_5d_relative_perf():
    # 6 rows to compute 5d pct change
    tickers = ["SPY", "IWM", "XLE"]
    rows = [
        [100.0, 50.0, 60.0],
        [100.0, 50.0, 60.0],
        [100.0, 50.0, 60.0],
        [100.0, 50.0, 60.0],
        [100.0, 50.0, 60.0],
        [101.0, 50.5, 63.0],  # SPY +1%, IWM +1%, XLE +5%
    ]
    close_df = _make_close_df(tickers, rows)
    hist = _make_hist(close_df)

    with patch("market_intelligence.sector_rotation.yf") as mock_yf:
        mock_yf.download.return_value = hist
        result = get_sector_rotation()

    xle_perfs = [s for s in result.entering if s.ticker == "XLE"]
    if xle_perfs:
        assert xle_perfs[0].rel_perf_5d is not None
        assert xle_perfs[0].rel_perf_5d > 0


# ---------------------------------------------------------------------------
# Degradation / failure paths
# ---------------------------------------------------------------------------

def test_sector_rotation_yfinance_exception():
    with patch("market_intelligence.sector_rotation.yf") as mock_yf:
        mock_yf.download.side_effect = RuntimeError("network error")
        result = get_sector_rotation()

    assert result.entering == ()
    assert result.exiting == ()
    assert "sector_fetch_failed" in result.data_issues


def test_sector_rotation_empty_dataframe():
    with patch("market_intelligence.sector_rotation.yf") as mock_yf:
        mock_yf.download.return_value = pd.DataFrame()
        result = get_sector_rotation()

    assert "sector_data_empty" in result.data_issues


def test_sector_rotation_spy_missing():
    # hist without SPY column
    tickers = ["IWM", "XLK"]
    rows = [[50.0, 200.0], [50.5, 202.0]]
    close_df = _make_close_df(tickers, rows)
    hist = _make_hist(close_df)

    with patch("market_intelligence.sector_rotation.yf") as mock_yf:
        mock_yf.download.return_value = hist
        result = get_sector_rotation()

    assert "sector_spy_missing" in result.data_issues
    # With no SPY, rel_perf_1d for sectors should all be None
    for s in [*result.entering, *result.exiting]:
        assert s.rel_perf_1d is None


def test_sector_rotation_partial_data_no_crash():
    # Only SPY available (no sectors, no IWM)
    tickers = ["SPY"]
    rows = [[100.0], [101.0]]
    close_df = _make_close_df(tickers, rows)
    hist = _make_hist(close_df)

    with patch("market_intelligence.sector_rotation.yf") as mock_yf:
        mock_yf.download.return_value = hist
        result = get_sector_rotation()

    assert isinstance(result, SectorRotationResult)
    assert result.entering == ()
    assert result.exiting == ()


def test_sector_rotation_no_llm_imports():
    """sector_rotation must not import any LLM library."""
    import importlib
    import sys

    # Reload to inspect fresh module
    mod_name = "market_intelligence.sector_rotation"
    if mod_name in sys.modules:
        mod = sys.modules[mod_name]
    else:
        mod = importlib.import_module(mod_name)

    import ast
    import inspect

    source = inspect.getsource(mod)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                assert "anthropic" not in (name or ""), f"LLM import found: {name}"
                assert "openai" not in (name or ""), f"LLM import found: {name}"
