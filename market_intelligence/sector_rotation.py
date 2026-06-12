from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Sector ETFs tracked for rotation analysis, with French display names
_SECTOR_ETFS: dict[str, str] = {
    "XLK": "Technologie",
    "XLE": "Énergie",
    "XLF": "Finance",
    "XBI": "Biotechnologie",
    "XLV": "Santé",
    "IHI": "Dispositifs médicaux",
    "NUKZ": "Nucléaire",
    "QTUM": "Quantique",
    "PHO": "Eau",
    "FIW": "Infrastructure eau",
    "MOO": "Agriculture",
    "BOTZ": "Robotique/IA",
    "KARS": "Véhicules électriques",
}

_BENCHMARK = "SPY"
_SMALL_CAPS = "IWM"
# Threshold in percentage points to declare a small-caps trend vs. SPY
_SMALL_CAPS_THRESHOLD = 0.30


@dataclass(frozen=True)
class SectorPerf:
    """Relative performance of one sector ETF vs SPY benchmark."""

    ticker: str
    name: str
    rel_perf_1d: float | None
    rel_perf_5d: float | None


@dataclass(frozen=True)
class SectorRotationResult:
    """Top entering / exiting sectors and small-caps appetite gauge."""

    entering: tuple[SectorPerf, ...]
    exiting: tuple[SectorPerf, ...]
    iwm_rel_1d: float | None
    small_caps_trend: str
    data_issues: tuple[str, ...]


def _pct_change_nd(series: pd.Series, n: int) -> float | None:
    """Return n-day percent change for the last observation in series."""
    vals = series.dropna()
    if len(vals) < n + 1:
        return None
    prev = float(vals.iloc[-(n + 1)])
    last = float(vals.iloc[-1])
    if prev == 0.0:
        return None
    return round((last / prev - 1) * 100, 4)


def get_sector_rotation() -> SectorRotationResult:
    """Compute relative sector performance vs SPY for 1d/5d windows.

    Zero LLM. Batch yfinance download. Degrades gracefully on any
    individual ticker failure — partial data is returned with data_issues.
    """
    issues: list[str] = []
    all_tickers = [_BENCHMARK, _SMALL_CAPS] + list(_SECTOR_ETFS.keys())

    if yf is None:
        logger.warning("sector_rotation: yfinance not installed")
        return SectorRotationResult(
            entering=(),
            exiting=(),
            iwm_rel_1d=None,
            small_caps_trend="neutre",
            data_issues=("sector_yfinance_unavailable",),
        )

    try:
        hist = yf.download(
            all_tickers,
            period="10d",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
    except Exception as exc:
        logger.warning("sector_rotation yfinance fetch failed: %s", exc)
        return SectorRotationResult(
            entering=(),
            exiting=(),
            iwm_rel_1d=None,
            small_caps_trend="neutre",
            data_issues=("sector_fetch_failed",),
        )

    if hist.empty:
        return SectorRotationResult(
            entering=(),
            exiting=(),
            iwm_rel_1d=None,
            small_caps_trend="neutre",
            data_issues=("sector_data_empty",),
        )

    # Multi-ticker download → MultiIndex columns; hist["Close"] → (dates × tickers)
    try:
        if isinstance(hist.columns, pd.MultiIndex):
            close_df: pd.DataFrame = hist["Close"]
        else:
            # Should not happen with multi-ticker download, but handle defensively
            close_df = hist[["Close"]] if "Close" in hist.columns else pd.DataFrame()
    except Exception as exc:
        logger.warning("sector_rotation close extraction failed: %s", exc)
        return SectorRotationResult(
            entering=(),
            exiting=(),
            iwm_rel_1d=None,
            small_caps_trend="neutre",
            data_issues=("sector_close_extraction_failed",),
        )

    if close_df.empty:
        return SectorRotationResult(
            entering=(),
            exiting=(),
            iwm_rel_1d=None,
            small_caps_trend="neutre",
            data_issues=("sector_close_empty",),
        )

    # SPY baseline
    if _BENCHMARK not in close_df.columns:
        issues.append("sector_spy_missing")
        spy_1d: float | None = None
        spy_5d: float | None = None
    else:
        spy_1d = _pct_change_nd(close_df[_BENCHMARK], 1)
        spy_5d = _pct_change_nd(close_df[_BENCHMARK], 5)
        if spy_1d is None:
            issues.append("sector_spy_1d_missing")
        if spy_5d is None:
            issues.append("sector_spy_5d_missing")

    # Per-sector relative performance
    sector_perfs: list[SectorPerf] = []
    for ticker, name in _SECTOR_ETFS.items():
        if ticker not in close_df.columns:
            issues.append(f"sector_missing:{ticker}")
            continue
        pct_1d = _pct_change_nd(close_df[ticker], 1)
        pct_5d = _pct_change_nd(close_df[ticker], 5)
        rel_1d = (
            round(pct_1d - spy_1d, 4)
            if pct_1d is not None and spy_1d is not None
            else None
        )
        rel_5d = (
            round(pct_5d - spy_5d, 4)
            if pct_5d is not None and spy_5d is not None
            else None
        )
        sector_perfs.append(SectorPerf(ticker=ticker, name=name, rel_perf_1d=rel_1d, rel_perf_5d=rel_5d))

    with_1d = [s for s in sector_perfs if s.rel_perf_1d is not None]
    without_1d = [s for s in sector_perfs if s.rel_perf_1d is None]
    for s in without_1d:
        issues.append(f"sector_no_1d:{s.ticker}")

    sorted_perfs = sorted(with_1d, key=lambda s: s.rel_perf_1d, reverse=True)  # type: ignore[arg-type]

    entering = tuple(sorted_perfs[:3])
    exiting = tuple(sorted(sorted_perfs[-3:], key=lambda s: s.rel_perf_1d))  # type: ignore[arg-type]

    # IWM vs SPY small-caps gauge
    iwm_rel_1d: float | None = None
    small_caps_trend = "neutre"
    if _SMALL_CAPS in close_df.columns:
        iwm_1d = _pct_change_nd(close_df[_SMALL_CAPS], 1)
        if iwm_1d is not None and spy_1d is not None:
            iwm_rel_1d = round(iwm_1d - spy_1d, 4)
            if iwm_rel_1d > _SMALL_CAPS_THRESHOLD:
                small_caps_trend = "surperformance"
            elif iwm_rel_1d < -_SMALL_CAPS_THRESHOLD:
                small_caps_trend = "sous-performance"
    else:
        issues.append("sector_iwm_missing")

    return SectorRotationResult(
        entering=entering,
        exiting=exiting,
        iwm_rel_1d=iwm_rel_1d,
        small_caps_trend=small_caps_trend,
        data_issues=tuple(dict.fromkeys(issues)),
    )
