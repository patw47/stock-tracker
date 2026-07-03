from __future__ import annotations

import inspect
from unittest.mock import Mock, patch

import pytest

import market_intelligence.market_status as ms
from market_intelligence.market_status import fetch_market_status
from market_intelligence.registry_schema import TickerEntry
from market_intelligence.warren_alert_research import analyze_alerts


def _entry(symbol: str = "AAPL") -> TickerEntry:
    return TickerEntry(symbol=symbol, api_symbol=symbol, expected_name="Test Corp")


def _xml_response(text: str) -> Mock:
    r = Mock()
    r.raise_for_status.return_value = None
    r.content = text.encode("utf-8")
    return r


def _html_response(text: str) -> Mock:
    r = Mock()
    r.raise_for_status.return_value = None
    r.text = text
    return r


def _halt_rss(halted: list[str], resumed: list[str] | None = None) -> str:
    """Build a NASDAQ Trader tradehalts RSS. `resumed` symbols carry a resumption
    trade time and must NOT be treated as currently halted."""
    items = [
        f"<item><ndaq:IssueSymbol>{s}</ndaq:IssueSymbol>"
        f"<ndaq:ReasonCode>LUDP</ndaq:ReasonCode></item>"
        for s in halted
    ]
    items += [
        f"<item><ndaq:IssueSymbol>{s}</ndaq:IssueSymbol>"
        f"<ndaq:ReasonCode>LUDP</ndaq:ReasonCode>"
        f"<ndaq:ResumptionTradeTime>09:35:00</ndaq:ResumptionTradeTime></item>"
        for s in (resumed or [])
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss xmlns:ndaq="urn:nasdaq"><channel>' + "".join(items) + "</channel></rss>"
    )


def _nasdaq_html(symbols: list[str]) -> str:
    rows = "\n".join(f"<tr><td>{s}</td><td>Corp</td></tr>" for s in symbols)
    return f"<table><tr><td>Symbol</td><td>Security</td></tr>\n{rows}\n</table>"


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    ms._finra_halted_cache = None
    ms._finra_fetch_failed = False
    ms._nasdaq_ssr_cache = None
    ms._nasdaq_fetch_failed = False
    yield


# --- FINRA halt status ---


def test_finra_ticker_present_returns_halted() -> None:
    with patch("market_intelligence.market_status.requests.get") as mock_get:
        mock_get.side_effect = [
            _xml_response(_halt_rss(["AAPL", "GME"])),
            _html_response(_nasdaq_html([])),
        ]
        result = fetch_market_status(_entry("AAPL"))
    assert result.halt_status == "halted"


def test_finra_ticker_absent_fetch_ok_returns_active() -> None:
    with patch("market_intelligence.market_status.requests.get") as mock_get:
        mock_get.side_effect = [
            _xml_response(_halt_rss(["GME"])),
            _html_response(_nasdaq_html([])),
        ]
        result = fetch_market_status(_entry("AAPL"))
    assert result.halt_status == "active"


def test_finra_resumed_symbol_not_halted() -> None:
    """A symbol present in the feed but already resumed is not currently halted."""
    with patch("market_intelligence.market_status.requests.get") as mock_get:
        mock_get.side_effect = [
            _xml_response(_halt_rss(halted=["GME"], resumed=["AAPL"])),
            _html_response(_nasdaq_html([])),
        ]
        result = fetch_market_status(_entry("AAPL"))
    assert result.halt_status == "active"


def test_finra_fetch_failure_returns_unknown_with_data_issue() -> None:
    with patch("market_intelligence.market_status.requests.get") as mock_get:
        mock_get.side_effect = [
            Exception("connection timeout"),
            _html_response(_nasdaq_html([])),
        ]
        result = fetch_market_status(_entry("AAPL"))
    assert result.halt_status == "unknown"
    assert "finra_halt_fetch_error" in result.data_issues


# --- NASDAQ SSR status ---


def test_nasdaq_ticker_present_returns_halted() -> None:
    with patch("market_intelligence.market_status.requests.get") as mock_get:
        mock_get.side_effect = [
            _xml_response(_halt_rss([])),
            _html_response(_nasdaq_html(["AAPL", "SPY"])),
        ]
        result = fetch_market_status(_entry("AAPL"))
    assert result.ssr_status == "halted"


def test_nasdaq_ticker_absent_fetch_ok_returns_active() -> None:
    with patch("market_intelligence.market_status.requests.get") as mock_get:
        mock_get.side_effect = [
            _xml_response(_halt_rss([])),
            _html_response(_nasdaq_html(["GME"])),
        ]
        result = fetch_market_status(_entry("AAPL"))
    assert result.ssr_status == "active"


def test_nasdaq_fetch_failure_returns_unknown_with_data_issue() -> None:
    with patch("market_intelligence.market_status.requests.get") as mock_get:
        mock_get.side_effect = [
            _xml_response(_halt_rss([])),
            Exception("500 server error"),
        ]
        result = fetch_market_status(_entry("AAPL"))
    assert result.ssr_status == "unknown"
    assert "ssr_fetch_error" in result.data_issues


# --- Independence ---


def test_finra_failure_does_not_block_nasdaq_fetch() -> None:
    with patch("market_intelligence.market_status.requests.get") as mock_get:
        mock_get.side_effect = [
            Exception("finra down"),
            _html_response(_nasdaq_html(["AAPL"])),
        ]
        result = fetch_market_status(_entry("AAPL"))
    assert result.halt_status == "unknown"
    assert result.ssr_status == "halted"
    assert mock_get.call_count == 2


# --- Cache ---


def test_cache_http_called_once_per_source_across_multiple_tickers() -> None:
    with patch("market_intelligence.market_status.requests.get") as mock_get:
        mock_get.side_effect = [
            _xml_response(_halt_rss(["GME"])),
            _html_response(_nasdaq_html(["AAPL"])),
        ]
        fetch_market_status(_entry("AAPL"))
        fetch_market_status(_entry("GME"))
        fetch_market_status(_entry("SPY"))
    assert mock_get.call_count == 2


# --- analyze_alerts default ---


def test_analyze_alerts_uses_fetch_market_status_by_default() -> None:
    sig = inspect.signature(analyze_alerts)
    default = sig.parameters["market_status_fetcher"].default
    assert default is fetch_market_status
