from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from typing import Final, Literal

import requests

from market_intelligence.registry_schema import TickerEntry

logger = logging.getLogger(__name__)

MarketStatus = Literal["active", "inactive", "halted", "unknown"]

_FINRA_HALT_URL: Final[str] = (
    "https://api.finra.org/data/group/otcMarket/name/haltedSecurities"
)
_NASDAQ_SSR_URL: Final[str] = (
    "https://www.nasdaqtrader.com/dynamic/symdir/shortsalecircuitbreaker.htm"
)
_HTTP_TIMEOUT: Final[int] = 10

_finra_halted_cache: set[str] | None = None
_finra_fetch_failed: bool = False
_nasdaq_ssr_cache: set[str] | None = None
_nasdaq_fetch_failed: bool = False


@dataclass(frozen=True)
class MarketStructureStatus:
    """Represent explicit halt and short-sale-restriction status."""

    halt_status: MarketStatus
    ssr_status: MarketStatus
    data_issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return asdict(self)


class _SSRTableParser(HTMLParser):
    """Extract first-column symbols from NASDAQ SSR HTML table."""

    def __init__(self) -> None:
        super().__init__()
        self._td_index: int = 0
        self._capture: bool = False
        self.symbols: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._td_index = 0
        elif tag == "td":
            self._td_index += 1
            self._capture = self._td_index == 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "td":
            self._capture = False

    def handle_data(self, data: str) -> None:
        if self._capture:
            symbol = data.strip()
            if symbol and symbol.upper() != "SYMBOL":
                self.symbols.append(symbol.upper())


def _fetch_finra_halts() -> set[str]:
    """Fetch current FINRA halt list; raise on any error."""
    response = requests.get(_FINRA_HALT_URL, timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    data = response.json()
    return {item["issueSymbol"].upper() for item in data if "issueSymbol" in item}


def _fetch_nasdaq_ssr() -> set[str]:
    """Fetch current NASDAQ SSR list; raise on any error."""
    response = requests.get(_NASDAQ_SSR_URL, timeout=_HTTP_TIMEOUT)
    response.raise_for_status()
    parser = _SSRTableParser()
    parser.feed(response.text)
    return set(parser.symbols)


def _get_finra_halted() -> set[str] | None:
    """Return cached FINRA halts, fetching once per process. None on failure."""
    global _finra_halted_cache, _finra_fetch_failed
    if _finra_fetch_failed:
        return None
    if _finra_halted_cache is not None:
        return _finra_halted_cache
    try:
        _finra_halted_cache = _fetch_finra_halts()
        logger.info("FINRA halts fetched: %d symbols", len(_finra_halted_cache))
        return _finra_halted_cache
    except Exception as exc:
        logger.warning("FINRA halt fetch failed: %s", exc)
        _finra_fetch_failed = True
        return None


def _get_nasdaq_ssr() -> set[str] | None:
    """Return cached NASDAQ SSR, fetching once per process. None on failure."""
    global _nasdaq_ssr_cache, _nasdaq_fetch_failed
    if _nasdaq_fetch_failed:
        return None
    if _nasdaq_ssr_cache is not None:
        return _nasdaq_ssr_cache
    try:
        _nasdaq_ssr_cache = _fetch_nasdaq_ssr()
        logger.info("NASDAQ SSR fetched: %d symbols", len(_nasdaq_ssr_cache))
        return _nasdaq_ssr_cache
    except Exception as exc:
        logger.warning("NASDAQ SSR fetch failed: %s", exc)
        _nasdaq_fetch_failed = True
        return None


def fetch_market_status(entry: TickerEntry) -> MarketStructureStatus:
    """Fetch halt and SSR status for a ticker from FINRA and NASDAQ."""
    symbol = entry.symbol.upper()
    issues: list[str] = []

    finra = _get_finra_halted()
    if finra is None:
        halt_status: MarketStatus = "unknown"
        issues.append("finra_halt_fetch_error")
    elif symbol in finra:
        halt_status = "halted"
    else:
        halt_status = "active"

    nasdaq = _get_nasdaq_ssr()
    if nasdaq is None:
        ssr_status: MarketStatus = "unknown"
        issues.append("ssr_fetch_error")
    elif symbol in nasdaq:
        ssr_status = "halted"
    else:
        ssr_status = "active"

    return MarketStructureStatus(
        halt_status=halt_status,
        ssr_status=ssr_status,
        data_issues=tuple(issues),
    )
