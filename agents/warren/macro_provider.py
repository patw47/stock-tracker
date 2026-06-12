from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import requests

try:
    import anthropic as anthropic
except ImportError:  # optional at import time; patched in tests
    anthropic = None  # type: ignore[assignment]

from agents.warren.models import MacroContext, MacroSnapshot, UpcomingEvent

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


_PERCENT_RE = re.compile(r'\d+(\.\d+)?%')
_MACRO_SEARCH_QUERY = "markets macro today Fed inflation geopolitics 2026"

_EXTRACT_PROMPT = """Based on the macro context above, return a JSON object with exactly these fields:
{
  "fed_stance": "hawkish" or "dovish" or "neutral",
  "dollar_signal": "short qualitative description of USD direction, no percentages",
  "geopolitical_notes": "summary of active geopolitical risks, no percentages",
  "overall_sentiment": "risk-on" or "risk-off" or "neutral",
  "upcoming_events": [{"name": "event name", "date": "YYYY-MM-DD or descriptive date"}],
  "rate_expectations": "qualitative FedWatch-style description of market rate expectations (cuts or hikes expected), no percentages — null if unknown",
  "ipos": "recent or upcoming notable IPOs and their sector — null if none found",
  "hot_sectors": "sectors showing notable momentum or investor interest today — null if unclear",
  "fear_greed": "Fear & Greed index numeric value or qualitative reading if mentioned — null if not found",
  "notable_rumors": "notable unconfirmed market rumors, each prefixed with RUMEUR: — null if none"
}
Return ONLY valid JSON, no other text."""


def _sanitize_qualitative(text: str) -> str:
    """Strip bare percentage strings from a qualitative field."""
    return _PERCENT_RE.sub("", text).strip()


def _search_macro_raw() -> str:
    """Perform web search for macro context using anthropic web_search tool."""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
        messages=[{
            "role": "user",
            "content": (
                f"Search for: {_MACRO_SEARCH_QUERY}\n\n"
                "Summarize the current macro environment: Fed stance, dollar direction, "
                "geopolitical risks, and upcoming market-moving events."
            ),
        }],
    )
    return "\n".join(
        block.text for block in response.content if hasattr(block, "text") and block.text
    )


def _extract_snapshot_from_text(context_text: str) -> MacroSnapshot:
    """Call LLM to extract structured MacroSnapshot fields from context text."""
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": f"Macro context:\n{context_text}\n\n{_EXTRACT_PROMPT}",
        }],
    )
    raw_text = response.content[0].text if response.content else "{}"
    m = re.search(r'\{.*\}', raw_text, re.DOTALL)
    raw: dict = json.loads(m.group() if m else raw_text)

    dollar_signal: str = raw.get("dollar_signal") or "dollar direction uncertain"
    geopolitical_notes: str = raw.get("geopolitical_notes") or "no major geopolitical disruptions noted"

    if _PERCENT_RE.search(dollar_signal):
        dollar_signal = _sanitize_qualitative(dollar_signal) or "dollar direction uncertain"
    if _PERCENT_RE.search(geopolitical_notes):
        geopolitical_notes = _sanitize_qualitative(geopolitical_notes) or "geopolitical tensions persist"

    upcoming_events = [
        UpcomingEvent(name=e["name"], date=e["date"])
        for e in raw.get("upcoming_events", [])
        if isinstance(e, dict) and "name" in e and "date" in e
    ]

    def _safe_str(key: str) -> str | None:
        val = raw.get(key)
        return str(val).strip() if val and str(val).strip() else None

    return MacroSnapshot(
        fed_stance=raw.get("fed_stance", "neutral"),
        dollar_signal=dollar_signal,
        geopolitical_notes=geopolitical_notes,
        overall_sentiment=raw.get("overall_sentiment", "neutral"),
        upcoming_events=upcoming_events,
        rate_expectations=_safe_str("rate_expectations"),
        ipos=_safe_str("ipos"),
        hot_sectors=_safe_str("hot_sectors"),
        fear_greed=_safe_str("fear_greed"),
        notable_rumors=_safe_str("notable_rumors"),
    )


async def fetch_macro_snapshot() -> MacroSnapshot:
    """Fetch qualitative macro snapshot via web search and LLM extraction.

    Searches for current macro conditions, then uses an LLM to extract
    directional signals into a MacroSnapshot. Sanitizes dollar_signal and
    geopolitical_notes to remove bare percentage values.
    """
    context_text = _search_macro_raw()
    return _extract_snapshot_from_text(context_text)


def get_market_closes() -> dict[str, float | None]:
    """Fetch IWM and crude oil last close + daily pct change via yfinance."""
    result: dict[str, float | None] = {
        "iwm_close": None,
        "iwm_pct_1d": None,
        "oil_close": None,
        "oil_pct_1d": None,
    }
    try:
        import yfinance as yf  # already a project dependency
        for key_prefix, ticker in (("iwm", "IWM"), ("oil", "CL=F")):
            try:
                hist = yf.download(
                    ticker,
                    period="5d",
                    interval="1d",
                    progress=False,
                    auto_adjust=True,
                )
                if hist.empty:
                    continue
                if isinstance(hist.columns, __import__("pandas").MultiIndex):
                    hist.columns = hist.columns.droplevel(1)
                if "Close" not in hist.columns:
                    continue
                closes = hist["Close"].dropna()
                if len(closes) < 1:
                    continue
                last = float(closes.iloc[-1])
                result[f"{key_prefix}_close"] = last
                if len(closes) >= 2:
                    result[f"{key_prefix}_pct_1d"] = round(
                        (closes.iloc[-1] / closes.iloc[-2] - 1) * 100, 2
                    )
            except Exception as exc:
                logger.warning("yfinance fetch failed for %s: %s", ticker, exc)
    except ImportError:
        logger.warning("yfinance not available; skipping market closes")
    return result
