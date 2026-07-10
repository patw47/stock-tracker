from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from market_intelligence.anomaly_signals import calculate_all as calculate_signals
from market_intelligence.anomaly_signals import calculate_signals as calculate_signal
from market_intelligence.beta_gate import (
    FactorConfig,
    calculate_all,
    calculate_beta_gate,
    load_factor_config,
)


def _frame(returns: list[float], *, start: str = "2024-01-01") -> pd.DataFrame:
    closes = [100.0]
    for daily_return in returns:
        closes.append(closes[-1] * (1 + daily_return))
    return pd.DataFrame(
        {
            "open": closes,
            "high": [close + 1 for close in closes],
            "low": [close - 1 for close in closes],
            "close": closes,
            "volume": [100.0] * len(closes),
        },
        index=pd.date_range(start, periods=len(closes), freq="B"),
    )


def _config(
    *,
    sector_factors: dict[str, tuple[str, ...]] | None = None,
    single_factor_symbols: tuple[str, ...] = ("TEST",),
) -> FactorConfig:
    return FactorConfig(
        market_factor="IWM",
        correlation_threshold=0.35,
        sector_factors=sector_factors or {},
        single_factor_symbols=single_factor_symbols,
        endogeneity_note="No correction because constituent weights are small.",
    )


def _noisy_market() -> list[float]:
    return [-0.02, -0.01, 0.005, 0.015, 0.025, -0.015] * 10


def _orthogonal_noise(*factors: list[float]) -> list[float]:
    base = np.array([-0.003, 0.001, 0.002, -0.001, 0.003, -0.002] * 10)
    design = np.column_stack([np.ones(len(base)), *factors])
    projection = design @ np.linalg.lstsq(design, base, rcond=None)[0]
    residual = base - projection
    return (residual * (0.003 / np.max(np.abs(residual)))).tolist()


def test_market_model_removes_beta_move_and_excludes_current_day() -> None:
    market_prior = _noisy_market()
    noise = _orthogonal_noise(market_prior)
    stock_prior = [
        0.001 + 1.5 * value + noise_value
        for value, noise_value in zip(market_prior, noise)
    ]
    frames = {
        "TEST": _frame(stock_prior + [-0.059]),
        "IWM": _frame(market_prior + [-0.04]),
    }
    signal = calculate_signal("TEST", frames["TEST"])

    result = calculate_beta_gate(signal, frames, _config())

    assert result.model_used == "market"
    assert result.observation_count == 60
    assert result.fallback_applied is False
    assert result.alpha == pytest.approx(0.001, abs=1e-10)
    assert result.market_beta == pytest.approx(1.5, abs=1e-10)
    assert result.expected_return == pytest.approx(-0.059, abs=1e-10)
    assert result.residual_return == pytest.approx(0.0, abs=1e-10)


def test_idiosyncratic_move_produces_robust_residual_z() -> None:
    market_prior = _noisy_market()
    noise = _orthogonal_noise(market_prior)
    stock_prior = [
        1.5 * value + noise_value for value, noise_value in zip(market_prior, noise)
    ]
    frames = {
        "TEST": _frame(stock_prior + [0.02]),
        "IWM": _frame(market_prior + [-0.04]),
    }

    result = calculate_beta_gate(
        calculate_signal("TEST", frames["TEST"]), frames, _config()
    )

    assert result.expected_return == pytest.approx(-0.06, abs=1e-10)
    assert result.residual_return == pytest.approx(0.08, abs=1e-10)
    assert result.residual_scale is not None
    assert result.z_resid == pytest.approx(
        result.residual_return / result.residual_scale
    )


def test_sector_factor_activates_above_correlation_gate() -> None:
    market_prior = _noisy_market()
    sector_prior = [0.012, -0.018, 0.02, -0.006, 0.014, -0.01] * 10
    noise = _orthogonal_noise(market_prior, sector_prior)
    stock_prior = [
        0.5 * market + 2.0 * sector + error
        for market, sector, error in zip(market_prior, sector_prior, noise)
    ]
    frames = {
        "TEST": _frame(stock_prior + [0.055]),
        "IWM": _frame(market_prior + [0.01]),
        "SECT": _frame(sector_prior + [0.025]),
    }
    config = _config(
        sector_factors={"TEST": ("SECT",)},
        single_factor_symbols=(),
    )

    result = calculate_beta_gate(
        calculate_signal("TEST", frames["TEST"]), frames, config
    )

    assert result.model_used == "market+sector"
    assert result.sector_factor == "SECT"
    assert result.sector_correlation is not None
    assert result.sector_correlation > 0.35
    assert result.market_beta == pytest.approx(0.5, abs=1e-10)
    assert result.sector_beta == pytest.approx(2.0, abs=1e-10)
    assert result.residual_return == pytest.approx(0.0, abs=1e-10)


def test_low_sector_correlation_falls_back_to_market_model() -> None:
    market_prior = _noisy_market()
    sector_prior = [0.02, 0.02, -0.02, -0.02, 0.01, -0.01] * 10
    noise = [-0.003, 0.001, 0.002, -0.001, 0.003, -0.002] * 10
    stock_prior = [1.2 * market + error for market, error in zip(market_prior, noise)]
    frames = {
        "TEST": _frame(stock_prior + [0.012]),
        "IWM": _frame(market_prior + [0.01]),
        "SECT": _frame(sector_prior + [-0.02]),
    }
    config = _config(sector_factors={"TEST": ("SECT",)}, single_factor_symbols=())

    result = calculate_beta_gate(
        calculate_signal("TEST", frames["TEST"]), frames, config
    )

    assert result.model_used == "market"
    assert result.sector_factor is None
    assert result.sector_correlation is not None
    assert result.sector_correlation <= 0.35
    assert result.sector_beta is None


def test_short_history_and_missing_factor_are_explicit() -> None:
    market_prior = _noisy_market()[:25]
    noise = [-0.003, 0.001, 0.002, -0.001, 0.003] * 5
    stock_prior = [1.5 * market + error for market, error in zip(market_prior, noise)]
    frames = {
        "TEST": _frame(stock_prior + [0.01]),
        "IWM": _frame(market_prior + [0.01]),
    }
    config = _config(sector_factors={"TEST": ("MISSING",)}, single_factor_symbols=())

    result = calculate_beta_gate(
        calculate_signal("TEST", frames["TEST"]), frames, config
    )

    assert result.model_used == "market"
    assert result.observation_count == 25
    assert result.fallback_applied is True
    assert "missing_factor_frame:MISSING" in result.data_issues


def test_insufficient_or_missing_market_data_returns_unavailable() -> None:
    short_frames = {
        "TEST": _frame([0.01] * 19 + [0.02]),
        "IWM": _frame([0.005] * 19 + [0.01]),
    }
    short = calculate_beta_gate(
        calculate_signal("TEST", short_frames["TEST"]), short_frames, _config()
    )
    missing_frames = {"TEST": _frame(_noisy_market() + [0.02])}
    missing = calculate_beta_gate(
        calculate_signal("TEST", missing_frames["TEST"]), missing_frames, _config()
    )

    assert short.model_used is None
    assert short.observation_count == 19
    assert "insufficient_history" in short.data_issues
    assert missing.model_used is None
    assert "missing_market_frame" in missing.data_issues


def test_singular_regression_and_stale_market_are_explicit() -> None:
    constant_market = [0.01] * 60
    frames = {
        "TEST": _frame([0.02] * 60 + [0.03]),
        "IWM": _frame(constant_market + [0.01]),
    }
    singular = calculate_beta_gate(
        calculate_signal("TEST", frames["TEST"]), frames, _config()
    )

    stale_frames = {
        "TEST": _frame(_noisy_market() + [0.02]),
        "IWM": _frame(_noisy_market()),
    }
    stale = calculate_beta_gate(
        calculate_signal("TEST", stale_frames["TEST"]), stale_frames, _config()
    )

    assert singular.model_used is None
    assert "singular_regression" in singular.data_issues
    assert stale.model_used is None
    assert "market_return_date_missing" in stale.data_issues


def test_signal_and_frame_return_mismatch_is_explicit() -> None:
    frames = {
        "TEST": _frame(_noisy_market() + [0.02]),
        "IWM": _frame(_noisy_market() + [0.01]),
    }
    signal = calculate_signal("TEST", frames["TEST"])
    changed_frames = {**frames, "TEST": frames["TEST"].copy()}
    changed_frames["TEST"].loc[changed_frames["TEST"].index[-1], "close"] *= 1.1

    result = calculate_beta_gate(signal, changed_frames, _config())

    assert result.model_used is None
    assert "signal_frame_return_mismatch" in result.data_issues


def test_batch_consumes_s1_signals_excludes_iwm_and_serializes_finite_values() -> None:
    market_prior = _noisy_market()
    noise = [-0.003, 0.001, 0.002, -0.001, 0.003, -0.002] * 10
    stock_prior = [1.5 * market + error for market, error in zip(market_prior, noise)]
    frames = {
        "TEST": _frame(stock_prior + [0.02]),
        "IWM": _frame(market_prior + [-0.04]),
        "^VIX": _frame(market_prior + [0.03]),
    }

    results = calculate_all(calculate_signals(frames), frames, _config())
    serialized = json.dumps(results["TEST"].to_dict())

    assert set(results) == {"TEST"}
    assert "NaN" not in serialized
    assert "Infinity" not in serialized
    assert not any(
        isinstance(value, float) and not math.isfinite(value)
        for value in results["TEST"].to_dict().values()
    )


def test_default_factor_config_covers_portfolio_and_notes_endogeneity() -> None:
    from market_intelligence.registry_schema import load_registry

    config = load_factor_config()
    covered = set(config.sector_factors) | set(config.single_factor_symbols)
    portfolio = {entry.symbol for entry in load_registry().portfolio_tickers}

    assert portfolio <= covered, sorted(portfolio - covered)
    assert config.market_factor == "IWM"
    assert config.correlation_threshold == pytest.approx(0.35)
    assert "no endogeneity correction" in config.endogeneity_note.lower()
