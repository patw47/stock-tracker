from __future__ import annotations

from unittest.mock import MagicMock, patch

from market_intelligence.candidate_alerts import CandidateAlert
from market_intelligence.dedup_hysteresis import DeduplicatedAlert
from market_intelligence.edgar_form4 import EdgarForm4Result
from market_intelligence.macro_snapshot import MacroEnrichedAlert, MacroSnapshot
from market_intelligence.registry_schema import Registry, TickerEntry
from market_intelligence.warren_alert_research import ResearchItem, _empty_research, analyze_alerts
from market_intelligence.web_research import (
    fetch_sector_etf_news,
    fetch_sector_news_for_entry,
    fetch_ticker_news,
)


def _entry(symbol: str = "RGTI", api_symbol: str | None = None) -> TickerEntry:
    return TickerEntry(
        symbol=symbol,
        api_symbol=api_symbol or symbol,
        expected_name=f"{symbol} Corp",
    )


def _articles(count: int) -> list[dict]:
    return [
        {
            "title": f"Article {i}",
            "link": f"https://finance.yahoo.com/news/article-{i}",
            "publisher": "Yahoo Finance",
            "summary": "",
        }
        for i in range(count)
    ]


def _rgti_registry() -> Registry:
    return Registry(
        portfolio_tickers=(
            TickerEntry(symbol="RGTI", api_symbol="RGTI", expected_name="Rigetti Computing"),
        ),
        macro_tickers=(),
        alias_map={},
    )


def _rgti_enriched() -> MacroEnrichedAlert:
    candidate = CandidateAlert(
        ticker="RGTI",
        as_of="2026-06-02",
        classification="speculative",
        eligible=True,
        is_candidate=True,
        direction="up",
        signal_types=("residual_z",),
        z_resid=3.0,
        residual_threshold=2.5,
        short_history_fallback_applied=False,
        data_issues=(),
    )
    alert = DeduplicatedAlert(
        candidate=candidate,
        squeeze_prone=None,
        fire_reason="initial",
        signal_types=("residual_z",),
    )
    return MacroEnrichedAlert(
        alert=alert,
        macro_snapshot=MacroSnapshot(
            as_of="2026-06-02",
            ten_year_yield=4.5,
            iwm_close=204.0,
            iwm_pct_change=2.0,
            oil_close=78.0,
            oil_pct_change=4.0,
            vix_close=19.0,
            dxy_close=103.5,
            data_issues=(),
        ),
    )


def _no_edgar(entry: TickerEntry) -> EdgarForm4Result:
    return EdgarForm4Result(ticker=entry.symbol, cik=None, filings=(), data_issues=())


# --- fetch_ticker_news ---


@patch("market_intelligence.web_research.yf.Ticker")
def test_fetch_ticker_news_returns_at_most_five_items(mock_ticker: MagicMock) -> None:
    mock_ticker.return_value.news = _articles(10)
    result = fetch_ticker_news(_entry("RGTI"))
    assert len(result) == 5
    assert all(isinstance(item, ResearchItem) for item in result)
    assert all(item.title and item.url and item.source for item in result)


@patch("market_intelligence.web_research.yf.Ticker")
def test_fetch_ticker_news_uses_api_symbol(mock_ticker: MagicMock) -> None:
    mock_ticker.return_value.news = _articles(1)
    fetch_ticker_news(_entry("RGTI"))
    mock_ticker.assert_called_once_with("RGTI")


@patch("market_intelligence.web_research.yf.Ticker")
def test_fetch_ticker_news_empty_news_returns_empty_tuple(mock_ticker: MagicMock) -> None:
    mock_ticker.return_value.news = []
    assert fetch_ticker_news(_entry("RGTI")) == ()


@patch("market_intelligence.web_research.yf.Ticker")
def test_fetch_ticker_news_exception_returns_empty_tuple(mock_ticker: MagicMock) -> None:
    mock_ticker.side_effect = Exception("network error")
    assert fetch_ticker_news(_entry("RGTI")) == ()


# --- fetch_sector_etf_news ---


@patch("market_intelligence.web_research.yf.Ticker")
def test_fetch_sector_etf_news_returns_at_most_three_items(mock_ticker: MagicMock) -> None:
    mock_ticker.return_value.news = _articles(10)
    result = fetch_sector_etf_news(_entry("RGTI"), "QTUM")
    assert len(result) == 3
    assert all(isinstance(item, ResearchItem) for item in result)


@patch("market_intelligence.web_research.yf.Ticker")
def test_fetch_sector_etf_news_empty_news_returns_empty_tuple(mock_ticker: MagicMock) -> None:
    mock_ticker.return_value.news = []
    assert fetch_sector_etf_news(_entry("RGTI"), "QTUM") == ()


@patch("market_intelligence.web_research.yf.Ticker")
def test_fetch_sector_etf_news_exception_returns_empty_tuple(mock_ticker: MagicMock) -> None:
    mock_ticker.side_effect = Exception("yf down")
    assert fetch_sector_etf_news(_entry("RGTI"), "QTUM") == ()


# --- fetch_sector_news_for_entry ---


@patch("market_intelligence.web_research.yf.Ticker")
def test_fetch_sector_news_for_entry_unknown_symbol_no_yf_call(mock_ticker: MagicMock) -> None:
    result = fetch_sector_news_for_entry(_entry("ZZZZ"))
    assert result == ()
    mock_ticker.assert_not_called()


@patch("market_intelligence.web_research.yf.Ticker")
def test_fetch_sector_news_for_entry_single_factor_symbol_returns_empty(
    mock_ticker: MagicMock,
) -> None:
    # VUZI is in single_factor_symbols — no sector ETF
    result = fetch_sector_news_for_entry(_entry("VUZI"))
    assert result == ()
    mock_ticker.assert_not_called()


@patch("market_intelligence.web_research.yf.Ticker")
def test_fetch_sector_news_for_entry_rgti_maps_to_qtum(mock_ticker: MagicMock) -> None:
    mock_ticker.return_value.news = _articles(5)
    result = fetch_sector_news_for_entry(_entry("RGTI"))
    mock_ticker.assert_called_once_with("QTUM")
    assert len(result) <= 3


# --- analyze_alerts default wiring ---


@patch("market_intelligence.web_research.yf.Ticker")
def test_analyze_alerts_without_explicit_fetcher_uses_web_research_not_empty(
    mock_ticker: MagicMock,
) -> None:
    mock_ticker.return_value.news = _articles(2)
    analyses = analyze_alerts(
        (_rgti_enriched(),),
        registry=_rgti_registry(),
        edgar_fetcher=_no_edgar,
        warren_client=lambda prompt: "ok",
    )
    assert len(analyses) == 1
    assert analyses[0].context.product_research != ()


def test_analyze_alerts_default_product_fetcher_is_not_empty_research() -> None:
    import inspect

    sig = inspect.signature(analyze_alerts)
    default = sig.parameters["product_research_fetcher"].default
    assert default is not _empty_research


def test_analyze_alerts_default_sector_fetcher_is_not_empty_research() -> None:
    import inspect

    sig = inspect.signature(analyze_alerts)
    default = sig.parameters["sector_research_fetcher"].default
    assert default is not _empty_research
