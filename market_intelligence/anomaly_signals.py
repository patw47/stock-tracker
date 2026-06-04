from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Final

import pandas as pd

RETURN_WINDOW: Final[int] = 60
VOLUME_WINDOW: Final[int] = 20
ATR_WINDOW: Final[int] = 14
YEAR_WINDOW: Final[int] = 252
_REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("open", "high", "low", "close", "volume")
_MAD_Z_SCALE: Final[float] = 0.6745


@dataclass(frozen=True)
class AnomalySignals:
    """Represent deterministic anomaly measurements for one ticker's latest EOD bar."""

    symbol: str
    as_of: str | None
    bar_count: int
    short_history: bool
    fallback_applied: bool
    return_window: int
    volume_window: int
    extrema_window: int
    daily_return: float | None
    return_median: float | None
    return_mad: float | None
    return_robust_z: float | None
    rvol: float | None
    log_volume_z: float | None
    opening_gap: float | None
    true_range: float | None
    atr14: float | None
    atr_expansion_ratio: float | None
    high_52w: float | None
    low_52w: float | None
    breakout_high_52w: bool | None
    breakout_low_52w: bool | None
    data_issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation without non-finite floats."""
        return asdict(self)


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _ratio(numerator: object, denominator: object) -> float | None:
    num = _finite(numerator)
    den = _finite(denominator)
    if num is None or den is None or den == 0:
        return None
    return _finite(num / den)


def _median_and_mad(values: pd.Series) -> tuple[float | None, float | None]:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    clean = clean[clean.map(math.isfinite)]
    if clean.empty:
        return None, None
    median = _finite(clean.median())
    if median is None:
        return None, None
    return median, _finite((clean - median).abs().median())


def _robust_z(
    value: float | None, median: float | None, mad: float | None
) -> float | None:
    if value is None or median is None or mad is None or mad == 0:
        return None
    return _finite(_MAD_Z_SCALE * (value - median) / mad)


def _empty_signals(symbol: str, issues: tuple[str, ...]) -> AnomalySignals:
    return AnomalySignals(
        symbol=symbol,
        as_of=None,
        bar_count=0,
        short_history=True,
        fallback_applied=True,
        return_window=0,
        volume_window=0,
        extrema_window=0,
        daily_return=None,
        return_median=None,
        return_mad=None,
        return_robust_z=None,
        rvol=None,
        log_volume_z=None,
        opening_gap=None,
        true_range=None,
        atr14=None,
        atr_expansion_ratio=None,
        high_52w=None,
        low_52w=None,
        breakout_high_52w=None,
        breakout_low_52w=None,
        data_issues=issues,
    )


def _prepare_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, tuple[str, ...]]:
    issues: list[str] = []
    if df.empty:
        return df.copy(), ("empty_frame",)

    frame = df.copy()
    frame.columns = [str(column).lower() for column in frame.columns]
    missing = [column for column in _REQUIRED_COLUMNS if column not in frame.columns]
    issues.extend(f"missing_column:{column}" for column in missing)
    for column in missing:
        frame[column] = float("nan")

    frame = frame[list(_REQUIRED_COLUMNS)]
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    if frame.index.isna().any():
        issues.append("invalid_date")
        frame = frame[~frame.index.isna()]
    if frame.index.has_duplicates:
        issues.append("duplicate_date")
        frame = frame[~frame.index.duplicated(keep="last")]
    frame = frame.sort_index()

    for column in _REQUIRED_COLUMNS:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if frame[list(_REQUIRED_COLUMNS)].isna().any(axis=None):
        issues.append("missing_values")
    return frame, tuple(issues)


def calculate_signals(symbol: str, df: pd.DataFrame) -> AnomalySignals:
    """Calculate Sprint 1 anomaly measurements from normalized EOD bars."""
    frame, issues = _prepare_frame(df)
    if frame.empty:
        return _empty_signals(symbol, issues)

    current = frame.iloc[-1]
    previous = frame.iloc[-2] if len(frame) >= 2 else None
    prior = frame.iloc[:-1]

    closes = pd.to_numeric(frame["close"], errors="coerce")
    returns = closes.pct_change(fill_method=None).replace(
        [math.inf, -math.inf], float("nan")
    )
    daily_return = _finite(returns.iloc[-1])
    prior_returns = returns.iloc[:-1].dropna().tail(RETURN_WINDOW)
    return_median, return_mad = _median_and_mad(prior_returns)
    return_robust_z = _robust_z(daily_return, return_median, return_mad)

    prior_volumes = pd.to_numeric(prior["volume"], errors="coerce")
    prior_volumes = prior_volumes[prior_volumes > 0].tail(VOLUME_WINDOW)
    current_volume = _finite(current["volume"])
    mean_volume = _finite(prior_volumes.mean()) if not prior_volumes.empty else None
    rvol = _ratio(current_volume, mean_volume) if current_volume is not None else None

    log_volumes = (
        prior_volumes.map(math.log) if not prior_volumes.empty else prior_volumes
    )
    log_median, log_mad = _median_and_mad(log_volumes)
    current_log_volume = (
        math.log(current_volume)
        if current_volume is not None and current_volume > 0
        else None
    )
    log_volume_z = _robust_z(current_log_volume, log_median, log_mad)

    previous_close = _finite(previous["close"]) if previous is not None else None
    opening_gap_ratio = _ratio(current["open"], previous_close)
    opening_gap = (
        _finite(opening_gap_ratio - 1) if opening_gap_ratio is not None else None
    )

    previous_closes = frame["close"].shift(1)
    true_ranges = pd.concat(
        (
            frame["high"] - frame["low"],
            (frame["high"] - previous_closes).abs(),
            (frame["low"] - previous_closes).abs(),
        ),
        axis=1,
    ).max(axis=1)
    true_range = _finite(true_ranges.iloc[-1])
    prior_true_ranges = true_ranges.iloc[:-1].dropna().tail(ATR_WINDOW)
    atr14 = (
        _finite(prior_true_ranges.mean())
        if len(prior_true_ranges) == ATR_WINDOW
        else None
    )
    atr_expansion_ratio = _ratio(true_range, atr14)

    extrema = frame.tail(YEAR_WINDOW)
    extrema_prior = prior.tail(YEAR_WINDOW)
    high_52w = _finite(extrema["high"].max()) if not extrema.empty else None
    low_52w = _finite(extrema["low"].min()) if not extrema.empty else None
    prior_high = (
        _finite(extrema_prior["high"].max()) if not extrema_prior.empty else None
    )
    prior_low = _finite(extrema_prior["low"].min()) if not extrema_prior.empty else None
    current_high = _finite(current["high"])
    current_low = _finite(current["low"])
    breakout_high = (
        current_high > prior_high
        if current_high is not None and prior_high is not None
        else None
    )
    breakout_low = (
        current_low < prior_low
        if current_low is not None and prior_low is not None
        else None
    )

    return_window = len(prior_returns)
    volume_window = len(prior_volumes)
    extrema_window = len(extrema)
    fallback_applied = (
        return_window < RETURN_WINDOW
        or volume_window < VOLUME_WINDOW
        or extrema_window < YEAR_WINDOW
    )

    return AnomalySignals(
        symbol=symbol,
        as_of=frame.index[-1].date().isoformat(),
        bar_count=len(frame),
        short_history=len(frame) < RETURN_WINDOW,
        fallback_applied=fallback_applied,
        return_window=return_window,
        volume_window=volume_window,
        extrema_window=extrema_window,
        daily_return=daily_return,
        return_median=return_median,
        return_mad=return_mad,
        return_robust_z=return_robust_z,
        rvol=rvol,
        log_volume_z=log_volume_z,
        opening_gap=opening_gap,
        true_range=true_range,
        atr14=atr14,
        atr_expansion_ratio=atr_expansion_ratio,
        high_52w=high_52w,
        low_52w=low_52w,
        breakout_high_52w=breakout_high,
        breakout_low_52w=breakout_low,
        data_issues=issues,
    )


def calculate_all(frames: dict[str, pd.DataFrame]) -> dict[str, AnomalySignals]:
    """Calculate one independent signal record for every supplied ticker."""
    return {
        symbol: calculate_signals(symbol, frame) for symbol, frame in frames.items()
    }
