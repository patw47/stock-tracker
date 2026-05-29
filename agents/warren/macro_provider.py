from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import requests

from agents.warren.models import MacroContext, MacroSnapshot

logger = logging.getLogger(__name__)

_FALLBACK_SNAPSHOT: MacroSnapshot = MacroSnapshot(
    policy_rate=5.25,
    cpi_yoy=3.5,
    ten_year_yield=4.5,
    two_year_yield=4.8,
    yield_curve_spread_10y2y=-0.3,
    vix=18.0,
    market_regime="neutral",
    as_of=datetime(2025, 1, 1, tzinfo=timezone.utc),
)


class MacroProviderError(Exception):
    """Base error raised by MacroContextProvider implementations."""


class MacroContextProvider(ABC):
    """Async source of MacroContext snapshots.

    Implementations are expected to be safe to call from an event loop and
    to return a MacroContext with as many fields populated as the upstream
    data allows. Missing fields stay None rather than raising.
    """

    @abstractmethod
    async def fetch(self) -> MacroContext:
        """Return the latest MacroContext snapshot."""


def _fetch_fred_series(series_id: str) -> float | None:
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        response = requests.get(
            url,
            timeout=8,
            headers={"User-Agent": "stock-tracker/1.0"},
        )
        response.raise_for_status()
        lines = response.text.splitlines()
        for line in reversed(lines[1:]):
            parts = line.split(",")
            if len(parts) < 2:
                continue
            value = parts[1].strip()
            if value and value != ".":
                return float(value)
        return None
    except Exception as exc:
        logger.warning("Failed to fetch FRED series %s: %s", series_id, exc)
        return None


def _derive_market_regime(vix: float | None, spread_10y2y: float | None) -> str:
    if vix is None:
        return "unknown"
    if vix >= 35:
        return "crisis"
    if vix >= 25:
        return "risk_off"
    if vix <= 15 and (spread_10y2y is None or spread_10y2y >= 0):
        return "risk_on"
    return "neutral"


def get_snapshot() -> MacroSnapshot:
    """Fetch live macro indicators from FRED and return a MacroSnapshot.

    Falls back to a hardcoded 2025-era snapshot if all five FRED fetches fail.
    """
    fed_rate = _fetch_fred_series("FEDFUNDS")
    cpi = _fetch_fred_series("CPIAUCSL_PC1")
    yield_10y = _fetch_fred_series("DGS10")
    yield_2y = _fetch_fred_series("DGS2")
    vix = _fetch_fred_series("VIXCLS")

    if yield_10y is not None and yield_2y is not None:
        spread: float | None = round(yield_10y - yield_2y, 4)
    else:
        spread = None

    regime = _derive_market_regime(vix, spread)

    if all(v is None for v in (fed_rate, cpi, yield_10y, yield_2y, vix)):
        logger.warning("All FRED fetches failed; returning fallback snapshot")
        return _FALLBACK_SNAPSHOT

    return MacroSnapshot(
        policy_rate=fed_rate if fed_rate is not None else _FALLBACK_SNAPSHOT.policy_rate,
        cpi_yoy=cpi if cpi is not None else _FALLBACK_SNAPSHOT.cpi_yoy,
        ten_year_yield=yield_10y if yield_10y is not None else _FALLBACK_SNAPSHOT.ten_year_yield,
        two_year_yield=yield_2y if yield_2y is not None else _FALLBACK_SNAPSHOT.two_year_yield,
        yield_curve_spread_10y2y=spread if spread is not None else _FALLBACK_SNAPSHOT.yield_curve_spread_10y2y,
        vix=vix if vix is not None else _FALLBACK_SNAPSHOT.vix,
        market_regime=regime,
        as_of=datetime.now(tz=timezone.utc),
    )
