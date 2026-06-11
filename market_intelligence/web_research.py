from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final

import yfinance as yf

from market_intelligence.registry_schema import TickerEntry
from market_intelligence.warren_alert_research import ResearchItem

logger = logging.getLogger(__name__)

_MAX_TICKER_ARTICLES: Final[int] = 5
_MAX_SECTOR_ARTICLES: Final[int] = 3
_SECTOR_FACTORS_PATH: Final[Path] = Path(__file__).parent / "data" / "sector_factors.json"


def _sector_etf_for(symbol: str) -> str | None:
    """Return first sector ETF from sector_factors.json for symbol, or None if absent."""
    try:
        data: dict = json.loads(_SECTOR_FACTORS_PATH.read_text())
        etfs: list[str] = data.get("sector_factors", {}).get(symbol, [])
        return etfs[0] if etfs else None
    except Exception:
        logger.warning("sector_factors load failed for %s", symbol)
        return None


def _articles_to_items(articles: list[dict], max_count: int) -> tuple[ResearchItem, ...]:
    items: list[ResearchItem] = []
    for art in articles[:max_count]:
        title: str = art.get("title", "")
        if not title:
            continue
        items.append(
            ResearchItem(
                source=art.get("publisher", "yahoo_finance"),
                title=title,
                url=art.get("link") or art.get("url"),
                summary=art.get("summary", "") or "",
            )
        )
    return tuple(items)


def fetch_ticker_news(entry: TickerEntry) -> tuple[ResearchItem, ...]:
    """Fetch up to 5 recent product news via Yahoo Finance for a ticker."""
    try:
        articles: list[dict] = yf.Ticker(entry.api_symbol).news or []
    except Exception:
        logger.warning("news_fetch_error for %s", entry.api_symbol)
        return ()
    if not articles:
        logger.debug("news_fetch_empty for %s", entry.api_symbol)
        return ()
    return _articles_to_items(articles, _MAX_TICKER_ARTICLES)


def fetch_sector_etf_news(entry: TickerEntry, etf_symbol: str) -> tuple[ResearchItem, ...]:
    """Fetch up to 3 sector news items via Yahoo Finance for an ETF symbol."""
    try:
        articles: list[dict] = yf.Ticker(etf_symbol).news or []
    except Exception:
        logger.warning(
            "news_fetch_error for sector ETF %s (ticker=%s)", etf_symbol, entry.api_symbol
        )
        return ()
    if not articles:
        logger.debug(
            "news_fetch_empty for sector ETF %s (ticker=%s)", etf_symbol, entry.api_symbol
        )
        return ()
    return _articles_to_items(articles, _MAX_SECTOR_ARTICLES)


def fetch_sector_news_for_entry(entry: TickerEntry) -> tuple[ResearchItem, ...]:
    """Fetch sector ETF news; returns () silently if ticker has no sector ETF mapping."""
    etf = _sector_etf_for(entry.symbol)
    if etf is None:
        return ()
    return fetch_sector_etf_news(entry, etf)
