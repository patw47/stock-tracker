from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import pandas as pd

from market_intelligence.anomaly_signals import AnomalySignals

RETURN_WINDOW: Final[int] = 60
MINIMUM_OBSERVATIONS: Final[int] = 20
MAD_SCALE: Final[float] = 0.6745
_CONFIG_PATH: Final[Path] = Path(__file__).parent / "data" / "sector_factors.json"


@dataclass(frozen=True)
class FactorConfig:
    """Define market and optional sector factors used by the beta gate."""

    market_factor: str
    correlation_threshold: float
    sector_factors: dict[str, tuple[str, ...]]
    single_factor_symbols: tuple[str, ...]
    endogeneity_note: str


@dataclass(frozen=True)
class BetaGateResult:
    """Represent the latest beta-adjusted residual for one Sprint 1 signal."""

    symbol: str
    as_of: str | None
    model_used: str | None
    market_factor: str
    sector_factor: str | None
    observation_count: int
    fallback_applied: bool
    sector_correlation: float | None
    alpha: float | None
    market_beta: float | None
    sector_beta: float | None
    expected_return: float | None
    residual_return: float | None
    residual_mad: float | None
    residual_scale: float | None
    z_resid: float | None
    data_issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation without non-finite floats."""
        return asdict(self)


def load_factor_config(
    path: Path = _CONFIG_PATH,
    single_factors_path: Path | None = None,
) -> FactorConfig:
    """Load the explicit market and sector factor mapping.

    Two files since Epic 10 S4: the market factor, the correlation threshold and
    the sector map are configuration (an ETF choice, decided in a PR); the
    single-factor list is state — whatever the cohort brought in with no sector
    guessed for it. An absent state file is an empty list, the fresh-machine case:
    every symbol then falls back to the market factor alone.
    """
    from market_intelligence.registry_check import SINGLE_FACTORS_PATH, load_state

    raw = json.loads(path.read_text(encoding="utf-8"))
    single = load_state(single_factors_path or SINGLE_FACTORS_PATH)
    return FactorConfig(
        market_factor=str(raw["market_factor"]),
        correlation_threshold=float(raw["correlation_threshold"]),
        sector_factors={
            str(symbol): tuple(str(factor) for factor in factors)
            for symbol, factors in raw["sector_factors"].items()
        },
        single_factor_symbols=tuple(
            str(symbol) for symbol in single.get("single_factor_symbols", [])
        ),
        endogeneity_note=str(raw["endogeneity_note"]),
    )


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _close_returns(frame: pd.DataFrame) -> pd.Series:
    if frame.empty:
        return pd.Series(dtype="float64")
    clean = frame.copy()
    clean.columns = [str(column).lower() for column in clean.columns]
    if "close" not in clean.columns:
        return pd.Series(dtype="float64")
    if not isinstance(clean.index, pd.DatetimeIndex):
        clean.index = pd.to_datetime(clean.index, errors="coerce")
    clean = clean[~clean.index.isna()]
    clean = clean[~clean.index.duplicated(keep="last")].sort_index()
    closes = pd.to_numeric(clean["close"], errors="coerce")
    returns = closes.pct_change(fill_method=None).replace(
        [math.inf, -math.inf], float("nan")
    )
    return returns.dropna()


def _empty_result(
    signal: AnomalySignals,
    config: FactorConfig,
    issues: list[str],
    *,
    sector_factor: str | None = None,
    sector_correlation: float | None = None,
    observation_count: int = 0,
) -> BetaGateResult:
    return BetaGateResult(
        symbol=signal.symbol,
        as_of=signal.as_of,
        model_used=None,
        market_factor=config.market_factor,
        sector_factor=sector_factor,
        observation_count=observation_count,
        fallback_applied=True,
        sector_correlation=sector_correlation,
        alpha=None,
        market_beta=None,
        sector_beta=None,
        expected_return=None,
        residual_return=None,
        residual_mad=None,
        residual_scale=None,
        z_resid=None,
        data_issues=tuple(dict.fromkeys(issues)),
    )


def _select_sector_factor(
    signal: AnomalySignals,
    frames: dict[str, pd.DataFrame],
    config: FactorConfig,
    stock_returns: pd.Series,
    as_of: pd.Timestamp,
    issues: list[str],
) -> tuple[str | None, float | None]:
    candidates = config.sector_factors.get(signal.symbol, ())
    if signal.symbol in config.single_factor_symbols:
        return None, None
    if not candidates:
        issues.append("factor_mapping_missing")
        return None, None

    best_factor: str | None = None
    best_correlation: float | None = None
    for factor in candidates:
        factor_frame = frames.get(factor)
        if factor_frame is None or factor_frame.empty:
            issues.append(f"missing_factor_frame:{factor}")
            continue
        factor_returns = _close_returns(factor_frame)
        paired = pd.concat(
            {"stock": stock_returns, "sector": factor_returns}, axis=1, join="inner"
        ).dropna()
        prior = paired[paired.index < as_of].tail(RETURN_WINDOW)
        if len(prior) < MINIMUM_OBSERVATIONS:
            issues.append(f"insufficient_factor_history:{factor}")
            continue
        correlation = _finite(prior["stock"].corr(prior["sector"]))
        if correlation is None:
            issues.append(f"invalid_factor_correlation:{factor}")
            continue
        if best_correlation is None or correlation > best_correlation:
            best_factor = factor
            best_correlation = correlation

    if best_correlation is None or best_correlation <= config.correlation_threshold:
        return None, best_correlation
    return best_factor, best_correlation


def calculate_beta_gate(
    signal: AnomalySignals,
    frames: dict[str, pd.DataFrame],
    config: FactorConfig | None = None,
) -> BetaGateResult:
    """Calculate the latest out-of-sample market-model residual for one S1 signal."""
    factor_config = config or load_factor_config()
    issues = list(signal.data_issues)
    if signal.as_of is None or signal.daily_return is None:
        issues.append("signal_return_unavailable")
        return _empty_result(signal, factor_config, issues)

    stock_frame = frames.get(signal.symbol)
    market_frame = frames.get(factor_config.market_factor)
    if stock_frame is None or stock_frame.empty:
        issues.append("missing_stock_frame")
        return _empty_result(signal, factor_config, issues)
    if market_frame is None or market_frame.empty:
        issues.append("missing_market_frame")
        return _empty_result(signal, factor_config, issues)

    as_of = pd.Timestamp(signal.as_of)
    stock_returns = _close_returns(stock_frame)
    market_returns = _close_returns(market_frame)
    if as_of not in stock_returns.index:
        issues.append("stock_return_date_missing")
        return _empty_result(signal, factor_config, issues)
    if as_of not in market_returns.index:
        issues.append("market_return_date_missing")
        return _empty_result(signal, factor_config, issues)
    frame_return = _finite(stock_returns.loc[as_of])
    if frame_return is None or not math.isclose(
        signal.daily_return, frame_return, rel_tol=1e-9, abs_tol=1e-12
    ):
        issues.append("signal_frame_return_mismatch")
        return _empty_result(signal, factor_config, issues)

    sector_factor, sector_correlation = _select_sector_factor(
        signal, frames, factor_config, stock_returns, as_of, issues
    )
    series: dict[str, pd.Series] = {
        "stock": stock_returns,
        "market": market_returns,
    }
    if sector_factor is not None:
        series["sector"] = _close_returns(frames[sector_factor])

    paired = pd.concat(series, axis=1, join="inner").dropna()
    prior = paired[paired.index < as_of].tail(RETURN_WINDOW)
    if len(prior) < MINIMUM_OBSERVATIONS:
        issues.append("insufficient_history")
        return _empty_result(
            signal,
            factor_config,
            issues,
            sector_factor=sector_factor,
            sector_correlation=sector_correlation,
            observation_count=len(prior),
        )
    if as_of not in paired.index:
        issues.append("factor_return_date_missing")
        return _empty_result(
            signal,
            factor_config,
            issues,
            sector_factor=sector_factor,
            sector_correlation=sector_correlation,
            observation_count=len(prior),
        )

    factor_columns = ["market"] + (["sector"] if sector_factor is not None else [])
    design = np.column_stack(
        [np.ones(len(prior), dtype=float), prior[factor_columns].to_numpy(dtype=float)]
    )
    if np.linalg.matrix_rank(design) < design.shape[1]:
        issues.append("singular_regression")
        return _empty_result(
            signal,
            factor_config,
            issues,
            sector_factor=sector_factor,
            sector_correlation=sector_correlation,
            observation_count=len(prior),
        )

    coefficients, _, _, _ = np.linalg.lstsq(
        design, prior["stock"].to_numpy(dtype=float), rcond=None
    )
    historical_residuals = prior["stock"].to_numpy(dtype=float) - design @ coefficients
    residual_median = float(np.median(historical_residuals))
    residual_mad = _finite(np.median(np.abs(historical_residuals - residual_median)))
    residual_scale = (
        _finite(residual_mad / MAD_SCALE)
        if residual_mad is not None and residual_mad > 0
        else None
    )

    current_factors = paired.loc[as_of, factor_columns].to_numpy(dtype=float)
    expected_return = _finite(coefficients[0] + current_factors @ coefficients[1:])
    current_stock_return = _finite(paired.loc[as_of, "stock"])
    residual_return = (
        _finite(current_stock_return - expected_return)
        if current_stock_return is not None and expected_return is not None
        else None
    )
    z_resid = (
        _finite(residual_return / residual_scale)
        if residual_return is not None and residual_scale is not None
        else None
    )
    if residual_scale is None:
        issues.append("zero_residual_scale")

    return BetaGateResult(
        symbol=signal.symbol,
        as_of=signal.as_of,
        model_used="market+sector" if sector_factor is not None else "market",
        market_factor=factor_config.market_factor,
        sector_factor=sector_factor,
        observation_count=len(prior),
        fallback_applied=len(prior) < RETURN_WINDOW,
        sector_correlation=sector_correlation,
        alpha=_finite(coefficients[0]),
        market_beta=_finite(coefficients[1]),
        sector_beta=_finite(coefficients[2]) if sector_factor is not None else None,
        expected_return=expected_return,
        residual_return=residual_return,
        residual_mad=residual_mad,
        residual_scale=residual_scale,
        z_resid=z_resid,
        data_issues=tuple(dict.fromkeys(issues)),
    )


def calculate_all(
    signals: dict[str, AnomalySignals],
    frames: dict[str, pd.DataFrame],
    config: FactorConfig | None = None,
) -> dict[str, BetaGateResult]:
    """Calculate beta-adjusted residuals for every supplied Sprint 1 signal."""
    factor_config = config or load_factor_config()
    target_symbols = set(factor_config.sector_factors) | set(
        factor_config.single_factor_symbols
    )
    return {
        symbol: calculate_beta_gate(signal, frames, factor_config)
        for symbol, signal in signals.items()
        if symbol in target_symbols
    }
