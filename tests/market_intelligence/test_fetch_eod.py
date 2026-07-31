from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd

from market_intelligence.fetch_eod import fetch_all
from market_intelligence.registry_schema import QuarantineEntry, Registry, TickerEntry


def _make_ohlcv_df(n: int = 5) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=n, freq="B")
    return pd.DataFrame(
        {"Open": 100.0, "High": 105.0, "Low": 95.0, "Close": 102.0, "Volume": 1_000_000},
        index=dates,
    )


def _twelve_data_response(n: int = 3) -> MagicMock:
    values = [
        {
            "datetime": f"2024-01-{i + 2:02d}",
            "open": "100",
            "high": "105",
            "low": "95",
            "close": "102",
            "volume": "1000000",
        }
        for i in range(n)
    ]
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"status": "ok", "values": values}
    return resp


class TestFetchAll:
    @patch("market_intelligence.fetch_eod.load_registry")
    @patch("market_intelligence.fetch_eod.load_quarantine")
    @patch("yfinance.download")
    def test_keyed_by_canonical_symbol(self, mock_dl, mock_lq, mock_lr, minimal_registry):
        mock_lr.return_value = minimal_registry
        mock_lq.return_value = []
        mock_dl.return_value = _make_ohlcv_df()

        result = fetch_all()

        assert "AAPL" in result
        assert "FAKE" in result
        assert "^VIX" in result

    @patch("market_intelligence.fetch_eod.load_registry")
    @patch("market_intelligence.fetch_eod.load_quarantine")
    @patch("yfinance.download")
    def test_dxy_translated_to_api_symbol(self, mock_dl, mock_lq, mock_lr):
        dxy_registry = Registry(
            portfolio_tickers=(
                TickerEntry(symbol="DXY", api_symbol="DX-Y.NYB", expected_name="US Dollar Index"),
            ),
            macro_tickers=(),
            alias_map={"DXY": "DX-Y.NYB"},
        )
        mock_lr.return_value = dxy_registry
        mock_lq.return_value = []
        mock_dl.return_value = _make_ohlcv_df()

        fetch_all()

        mock_dl.assert_called_once_with(
            "DX-Y.NYB",
            period="60d",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )

    @patch("market_intelligence.config.get_twelve_data_api_key", return_value="test_key")
    @patch("market_intelligence.fetch_eod.requests.get")
    @patch("yfinance.download")
    @patch("market_intelligence.fetch_eod.load_quarantine")
    @patch("market_intelligence.fetch_eod.load_registry")
    def test_fallback_to_twelve_data_when_yfinance_empty(
        self, mock_lr, mock_lq, mock_dl, mock_get, _mock_key, minimal_registry
    ):
        mock_lr.return_value = minimal_registry
        mock_lq.return_value = []
        mock_dl.return_value = pd.DataFrame()
        mock_get.return_value = _twelve_data_response(3)

        result = fetch_all()

        assert mock_get.called
        assert any(not df.empty for df in result.values())

    @patch("market_intelligence.config.get_twelve_data_api_key", return_value="test_key")
    @patch("market_intelligence.fetch_eod.requests.get", side_effect=Exception("network error"))
    @patch("yfinance.download")
    @patch("market_intelligence.fetch_eod.load_quarantine")
    @patch("market_intelligence.fetch_eod.load_registry")
    def test_both_sources_fail_returns_empty_df(
        self, mock_lr, mock_lq, mock_dl, _mock_get, _mock_key, minimal_registry
    ):
        mock_lr.return_value = minimal_registry
        mock_lq.return_value = []
        mock_dl.return_value = pd.DataFrame()

        result = fetch_all()

        for symbol, df in result.items():
            assert isinstance(df, pd.DataFrame), f"{symbol} not a DataFrame"
            assert df.empty, f"{symbol} should be empty"

    @patch("market_intelligence.fetch_eod.load_registry")
    @patch("market_intelligence.fetch_eod.load_quarantine")
    @patch("yfinance.download")
    def test_quarantined_symbols_absent(self, mock_dl, mock_lq, mock_lr, minimal_registry):
        mock_lr.return_value = minimal_registry
        mock_lq.return_value = [
            QuarantineEntry(symbol="FAKE", reason="not found", timestamp="2024-01-01T00:00:00+00:00")
        ]
        mock_dl.return_value = _make_ohlcv_df()

        result = fetch_all()

        assert "FAKE" not in result
        assert "AAPL" in result
