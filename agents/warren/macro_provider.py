from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import requests

from agents.warren.models import MacroContext

logger = logging.getLogger(__name__)

_FALLBACK_SNAPSHOT: MacroContext = MacroContext(
    policy_rate=5.25,
    cpi_yoy=3.5,
    pce_yoy=2.8,
    ten_year_yield=4.5,
    two_year_yield=4.8,
    yield_curve_spread_10y2y=-0.3,
    vix=18.0,
    hy_spread=3.5,
    ig_spread=1.0,
    dollar_index=104.0,
    unemployment_rate=4.1,
    spx_level=5300.0,
    spx_pct_change_1m=None,
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


def _fetch_fred_series_last_n(series_id: str, n: int) -> list[float]:
    """Return up to n most recent non-null values from a FRED series, oldest first."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
    try:
        response = requests.get(
            url,
            timeout=8,
            headers={"User-Agent": "stock-tracker/1.0"},
        )
        response.raise_for_status()
        values: list[float] = []
        for line in reversed(response.text.splitlines()[1:]):
            parts = line.split(",")
            if len(parts) < 2:
                continue
            raw = parts[1].strip()
            if raw and raw != ".":
                values.append(float(raw))
            if len(values) >= n:
                break
        values.reverse()
        return values
    except Exception as exc:
        logger.warning("Failed to fetch FRED series %s (last %d): %s", series_id, n, exc)
        return []


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


def get_snapshot() -> MacroContext:
    """Fetch live macro indicators from FRED and return a MacroContext.

    Live FRED series:
      FEDFUNDS      — Fed funds effective rate
      CPIAUCSL_PC1  — CPI headline YoY
      PCEPILFE_PC1  — Core PCE YoY (pre-computed series; silently None if unavailable)
      DGS10         — 10Y Treasury yield
      DGS2          — 2Y Treasury yield
      VIXCLS        — CBOE VIX closing
      BAMLH0A0HYM2  — ICE BofA HY OAS (credit risk proxy)
      BAMLC0A0CM    — ICE BofA IG OAS
      DTWEXBGS      — Trade-weighted broad dollar index
      UNRATE        — Civilian unemployment rate
      SP500         — S&P 500 level + ~22-day return

    Falls back to hardcoded 2025-era values if all five primary FRED fetches fail.
    """
    fed_rate = _fetch_fred_series("FEDFUNDS")
    cpi = _fetch_fred_series("CPIAUCSL_PC1")
    # PCEPILFE_PC1 is not guaranteed to exist as a pre-stored FRED CSV series;
    # _fetch_fred_series returns None silently if the request fails.
    pce = _fetch_fred_series("PCEPILFE_PC1")
    yield_10y = _fetch_fred_series("DGS10")
    yield_2y = _fetch_fred_series("DGS2")
    vix = _fetch_fred_series("VIXCLS")
    hy_spread = _fetch_fred_series("BAMLH0A0HYM2")
    ig_spread = _fetch_fred_series("BAMLC0A0CM")
    dollar_index = _fetch_fred_series("DTWEXBGS")
    unemployment = _fetch_fred_series("UNRATE")

    spx_history = _fetch_fred_series_last_n("SP500", 22)
    spx_level = spx_history[-1] if spx_history else None
    spx_1m: float | None = None
    if len(spx_history) >= 2:
        spx_1m = round((spx_history[-1] / spx_history[0] - 1) * 100, 2)

    if yield_10y is not None and yield_2y is not None:
        spread: float | None = round(yield_10y - yield_2y, 4)
    else:
        spread = None

    regime = _derive_market_regime(vix, spread)

    if all(v is None for v in (fed_rate, cpi, yield_10y, yield_2y, vix)):
        logger.warning("All primary FRED fetches failed; returning fallback snapshot")
        return _FALLBACK_SNAPSHOT

    fb = _FALLBACK_SNAPSHOT
    return MacroContext(
        policy_rate=fed_rate if fed_rate is not None else fb.policy_rate,
        cpi_yoy=cpi if cpi is not None else fb.cpi_yoy,
        pce_yoy=pce,
        ten_year_yield=yield_10y if yield_10y is not None else fb.ten_year_yield,
        two_year_yield=yield_2y if yield_2y is not None else fb.two_year_yield,
        yield_curve_spread_10y2y=spread if spread is not None else fb.yield_curve_spread_10y2y,
        vix=vix if vix is not None else fb.vix,
        hy_spread=hy_spread if hy_spread is not None else fb.hy_spread,
        ig_spread=ig_spread if ig_spread is not None else fb.ig_spread,
        dollar_index=dollar_index if dollar_index is not None else fb.dollar_index,
        unemployment_rate=unemployment if unemployment is not None else fb.unemployment_rate,
        spx_level=spx_level if spx_level is not None else fb.spx_level,
        spx_pct_change_1m=spx_1m,
        market_regime=regime,
        as_of=datetime.now(tz=timezone.utc),
    )
