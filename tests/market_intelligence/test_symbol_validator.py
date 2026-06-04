from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from market_intelligence.registry_schema import QuarantineEntry, TickerEntry
from market_intelligence.symbol_validator import validate_ticker, run_validation


def _ticker_mock(info: dict) -> MagicMock:
    m = MagicMock()
    m.info = info
    return m


def _side_effect_by_symbol(symbol: str) -> MagicMock:
    mapping = {
        "AAPL": {"longName": "Apple Inc."},
        "^VIX": {"longName": "CBOE Volatility Index"},
    }
    return _ticker_mock(mapping.get(symbol, {}))


class TestValidateTicker:
    def test_ok_when_name_matches(self):
        entry = TickerEntry(symbol="AAPL", api_symbol="AAPL", expected_name="Apple")
        with patch("yfinance.Ticker", return_value=_ticker_mock({"longName": "Apple Inc."})):
            result = validate_ticker(entry)
        assert result.status == "ok"
        assert result.actual_name == "Apple Inc."

    def test_not_found_when_info_empty(self):
        entry = TickerEntry(symbol="FAKE", api_symbol="FAKE", expected_name="Fake Corp")
        with patch("yfinance.Ticker", return_value=_ticker_mock({})):
            result = validate_ticker(entry)
        assert result.status == "not_found"
        assert result.actual_name == ""

    def test_api_error_on_exception(self):
        entry = TickerEntry(symbol="ERR", api_symbol="ERR", expected_name="Error Corp")
        with patch("yfinance.Ticker", side_effect=Exception("timeout")):
            result = validate_ticker(entry)
        assert result.status == "api_error"
        assert "timeout" in result.reason

    def test_name_mismatch(self):
        entry = TickerEntry(symbol="TSLA", api_symbol="TSLA", expected_name="Walmart")
        with patch("yfinance.Ticker", return_value=_ticker_mock({"longName": "Tesla Inc."})):
            result = validate_ticker(entry)
        assert result.status == "name_mismatch"

    def test_caret_symbol_passed_as_is(self):
        entry = TickerEntry(symbol="^VIX", api_symbol="^VIX", expected_name="CBOE Volatility")
        with patch("yfinance.Ticker") as mock_cls:
            mock_cls.return_value.info = {"longName": "CBOE Volatility Index"}
            validate_ticker(entry)
        mock_cls.assert_called_once_with("^VIX")


class TestRunValidation:
    @patch("market_intelligence.symbol_validator.load_registry")
    @patch("market_intelligence.symbol_validator.load_quarantine")
    @patch("market_intelligence.symbol_validator.append_quarantine")
    @patch("yfinance.Ticker")
    def test_quarantines_non_ok_symbols(
        self, mock_yf, mock_append, mock_lq, mock_lr, minimal_registry
    ):
        mock_lr.return_value = minimal_registry
        mock_lq.return_value = []
        mock_yf.side_effect = _side_effect_by_symbol

        run_validation()

        appended = {call.args[0].symbol for call in mock_append.call_args_list}
        assert "FAKE" in appended
        assert "AAPL" not in appended
        assert "^VIX" not in appended

    @patch("market_intelligence.symbol_validator.load_registry")
    @patch("market_intelligence.symbol_validator.load_quarantine")
    @patch("market_intelligence.symbol_validator.append_quarantine")
    @patch("yfinance.Ticker")
    def test_idempotent_no_duplicate_quarantine(
        self, mock_yf, mock_append, mock_lq, mock_lr, minimal_registry
    ):
        mock_lr.return_value = minimal_registry
        mock_lq.return_value = [
            QuarantineEntry(symbol="FAKE", reason="not found", timestamp="2024-01-01T00:00:00+00:00")
        ]
        mock_yf.side_effect = _side_effect_by_symbol

        run_validation()

        appended = {call.args[0].symbol for call in mock_append.call_args_list}
        assert "FAKE" not in appended
