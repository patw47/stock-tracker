from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from market_intelligence.anomaly_signals import AnomalySignals
from market_intelligence.beta_gate import BetaGateResult
from market_intelligence.candidate_alerts import (
    AlertConfigError,
    AlertThresholdConfig,
    CandidateAlert,
    evaluate_all,
    evaluate_candidate as _evaluate_candidate,
    load_alert_config,
    select_candidates,
)
from market_intelligence.registry_schema import load_registry


def _signal(**overrides: object) -> AnomalySignals:
    base = AnomalySignals(
        symbol="TEST",
        as_of="2026-06-03",
        bar_count=100,
        short_history=False,
        fallback_applied=True,
        return_window=60,
        volume_window=20,
        extrema_window=100,
        daily_return=0.03,
        return_median=0.0,
        return_mad=0.01,
        return_robust_z=2.0,
        rvol=1.0,
        log_volume_z=1.0,
        opening_gap=0.0,
        true_range=2.0,
        atr14=2.0,
        atr_expansion_ratio=1.0,
        high_52w=110.0,
        low_52w=90.0,
        breakout_high_52w=False,
        breakout_low_52w=False,
        data_issues=(),
    )
    return replace(base, **overrides)


def _gate(**overrides: object) -> BetaGateResult:
    base = BetaGateResult(
        symbol="TEST",
        as_of="2026-06-03",
        model_used="market",
        market_factor="IWM",
        sector_factor=None,
        observation_count=60,
        fallback_applied=False,
        sector_correlation=None,
        alpha=0.0,
        market_beta=1.0,
        sector_beta=None,
        expected_return=0.0,
        residual_return=0.03,
        residual_mad=0.01,
        residual_scale=0.01,
        z_resid=0.0,
        data_issues=(),
    )
    return replace(base, **overrides)


def _config(
    *,
    classifications: dict[str, str] | None = None,
    short_history_multiplier: float = 1.25,
) -> AlertThresholdConfig:
    return AlertThresholdConfig(
        calm_residual_z=2.0,
        speculative_residual_z=2.5,
        volume_z=2.5,
        rvol=2.5,
        atr_expansion=1.5,
        short_history_multiplier=short_history_multiplier,
        short_history_return=0.08,
        breakout_min_window=60,
        classifications=classifications or {"TEST": "calm"},
    )


def evaluate_candidate(
    signal: AnomalySignals,
    gate: BetaGateResult,
    config: AlertThresholdConfig,
    *,
    expected_as_of: str = "2026-06-03",
) -> CandidateAlert:
    """Evaluate a fixture against the shared expected EOD date."""
    return _evaluate_candidate(
        signal, gate, config, expected_as_of=expected_as_of
    )


def test_residual_z_alone_triggers_with_configured_classification_thresholds() -> None:
    calm = evaluate_candidate(_signal(), _gate(z_resid=2.01), _config())
    speculative = evaluate_candidate(
        _signal(), _gate(z_resid=2.01), _config(classifications={"TEST": "speculative"})
    )

    assert calm.is_candidate is True
    assert calm.signal_types == ("residual_z",)
    assert calm.direction == "up"
    assert speculative.is_candidate is False
    assert speculative.residual_threshold == pytest.approx(2.5)


def test_thresholds_are_strictly_greater_than_boundaries() -> None:
    residual = evaluate_candidate(_signal(), _gate(z_resid=2.0), _config())
    combination = evaluate_candidate(
        _signal(rvol=2.5, atr_expansion_ratio=1.5), _gate(), _config()
    )

    assert residual.is_candidate is False
    assert combination.is_candidate is False
    assert combination.signal_types == ()


@pytest.mark.parametrize(
    ("volume_overrides", "expected_type"),
    [
        ({"rvol": 2.51}, "rvol"),
        ({"log_volume_z": 2.51}, "volume_z"),
    ],
)
def test_volume_anomaly_plus_second_signal_triggers(
    volume_overrides: dict[str, float], expected_type: str
) -> None:
    decision = evaluate_candidate(
        _signal(**volume_overrides, atr_expansion_ratio=1.51), _gate(), _config()
    )

    assert decision.is_candidate is True
    assert decision.signal_types == (expected_type, "atr_expansion")


def test_volume_or_secondary_signal_alone_does_not_trigger() -> None:
    volume_only = evaluate_candidate(_signal(rvol=3.0), _gate(), _config())
    breakout_only = evaluate_candidate(
        _signal(breakout_high_52w=True), _gate(), _config()
    )

    assert volume_only.is_candidate is False
    assert breakout_only.is_candidate is False


def test_breakout_is_a_secondary_signal_and_all_signal_types_are_preserved() -> None:
    decision = evaluate_candidate(
        _signal(
            rvol=3.0,
            log_volume_z=3.0,
            atr_expansion_ratio=2.0,
            breakout_low_52w=True,
        ),
        _gate(residual_return=-0.03),
        _config(),
    )

    assert decision.is_candidate is True
    assert decision.direction == "down"
    assert decision.signal_types == (
        "volume_z",
        "rvol",
        "atr_expansion",
        "breakout_low_52w",
    )


def test_volume_breakout_direction_follows_breakout_not_small_residual() -> None:
    decision = evaluate_candidate(
        _signal(rvol=3.0, breakout_low_52w=True, daily_return=-0.03),
        _gate(z_resid=0.2, residual_return=0.01),
        _config(),
    )

    assert decision.is_candidate is True
    assert decision.direction == "down"


def test_short_history_widens_residual_and_volume_thresholds() -> None:
    residual = evaluate_candidate(
        _signal(volume_window=10),
        _gate(observation_count=40, fallback_applied=True, z_resid=2.1),
        _config(),
    )
    volume = evaluate_candidate(
        _signal(volume_window=10, rvol=3.0, atr_expansion_ratio=2.0),
        _gate(observation_count=40, fallback_applied=True),
        _config(),
    )

    assert residual.residual_threshold == pytest.approx(2.5)
    assert residual.short_history_fallback_applied is True
    assert residual.is_candidate is False
    assert volume.is_candidate is False


def test_valid_short_history_gate_can_trigger_above_widened_threshold() -> None:
    decision = evaluate_candidate(
        _signal(),
        _gate(observation_count=40, fallback_applied=True, z_resid=-2.51),
        _config(),
    )

    assert decision.is_candidate is True
    assert decision.direction == "down"
    assert decision.short_history_fallback_applied is True


def test_unavailable_direction_and_mismatched_or_stale_records_are_unclassified() -> None:
    no_direction = evaluate_candidate(
        _signal(daily_return=0.0, rvol=3.0, atr_expansion_ratio=2.0),
        _gate(residual_return=0.0),
        _config(),
    )
    mismatch = evaluate_candidate(
        _signal(), _gate(symbol="OTHER"), _config()
    )
    stale = evaluate_candidate(
        _signal(), _gate(), _config(), expected_as_of="2026-06-04"
    )

    assert no_direction.eligible is False
    assert "direction_unavailable" in no_direction.data_issues
    assert mismatch.eligible is False
    assert "signal_gate_symbol_mismatch" in mismatch.data_issues
    assert stale.eligible is False
    assert "stale_signal" in stale.data_issues


def test_ambiguous_data_is_never_silently_accepted() -> None:
    decision = evaluate_candidate(
        _signal(data_issues=("ambiguous_symbol",)),
        _gate(z_resid=5.0),
        _config(),
    )

    assert decision.eligible is False
    assert decision.is_candidate is False
    assert decision.data_issues == ("ambiguous_symbol",)


def test_insufficient_beta_history_uses_conservative_compound_fallback() -> None:
    decision = evaluate_candidate(
        _signal(daily_return=0.09, rvol=3.0, atr_expansion_ratio=2.0),
        _gate(
            model_used=None,
            observation_count=10,
            z_resid=None,
            data_issues=("insufficient_history",),
        ),
        _config(),
    )

    assert decision.eligible is True
    assert decision.is_candidate is True
    assert decision.signal_types == (
        "short_history_return",
        "rvol",
        "atr_expansion",
    )


def test_short_history_fallback_direction_follows_fixed_return() -> None:
    decision = evaluate_candidate(
        _signal(
            daily_return=-0.09,
            rvol=3.0,
            breakout_high_52w=True,
        ),
        _gate(
            model_used=None,
            observation_count=10,
            z_resid=None,
            data_issues=("insufficient_history",),
        ),
        _config(),
    )

    assert decision.is_candidate is True
    assert decision.direction == "down"


def test_invalid_beta_data_cannot_use_short_history_fallback() -> None:
    decision = evaluate_candidate(
        _signal(daily_return=0.09, rvol=3.0, atr_expansion_ratio=2.0),
        _gate(
            model_used=None,
            observation_count=10,
            z_resid=None,
            data_issues=("insufficient_history", "missing_market_frame"),
        ),
        _config(),
    )

    assert decision.eligible is False
    assert "beta_gate_unavailable" in decision.data_issues


def test_short_breakout_history_is_flagged_and_not_used() -> None:
    decision = evaluate_candidate(
        _signal(extrema_window=2, rvol=3.0, breakout_high_52w=True),
        _gate(),
        _config(),
    )

    assert decision.is_candidate is False
    assert "breakout_history_short" in decision.data_issues


def test_missing_volume_history_is_flagged_without_blocking_residual_alert() -> None:
    decision = evaluate_candidate(
        _signal(volume_window=0), _gate(z_resid=2.1), _config()
    )

    assert decision.is_candidate is True
    assert "volume_history_unavailable" in decision.data_issues


def test_batch_reports_missing_inputs_and_selects_only_candidates() -> None:
    config = _config(classifications={"TEST": "calm", "MISSING": "speculative"})
    signals = {"TEST": _signal()}
    gates = {"TEST": _gate(z_resid=2.1)}

    decisions = evaluate_all(
        signals,
        gates,
        config,
        expected_as_of="2026-06-03",
        expected_symbols=("TEST", "MISSING"),
    )
    candidates = select_candidates(
        signals,
        gates,
        config,
        expected_as_of="2026-06-03",
        expected_symbols=("TEST", "MISSING"),
    )

    assert tuple(decisions) == ("TEST", "MISSING")
    assert decisions["MISSING"].eligible is False
    assert decisions["MISSING"].data_issues == ("missing_s1_signal", "missing_s2_gate")
    assert candidates == (decisions["TEST"],)
    assert "NaN" not in json.dumps(decisions["MISSING"].to_dict())


def test_default_config_matches_s0_registry_and_spec_thresholds() -> None:
    config = load_alert_config()
    portfolio = {entry.symbol for entry in load_registry().portfolio_tickers}

    assert set(config.classifications) == portfolio
    assert config.classifications["XYL"] == "calm"
    assert config.classifications["MMED"] == "calm"
    assert config.classifications["RGTI"] == "speculative"
    assert config.calm_residual_z == pytest.approx(2.0)
    assert config.speculative_residual_z == pytest.approx(2.5)
    assert config.volume_z == pytest.approx(2.5)
    assert config.rvol == pytest.approx(2.5)
    assert config.atr_expansion == pytest.approx(1.5)
    assert config.short_history_return == pytest.approx(0.08)
    assert config.breakout_min_window == 60


def test_invalid_config_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "thresholds.json"
    path.write_text(
        json.dumps(
            {
                "thresholds": {
                    "calm_residual_z": 2.5,
                    "speculative_residual_z": 2.0,
                    "volume_z": 2.5,
                    "rvol": 2.5,
                    "atr_expansion": 1.5,
                    "short_history_multiplier": 1.0,
                    "short_history_return": 0.08,
                    "breakout_min_window": 60,
                },
                "classifications": {"TEST": "unknown"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AlertConfigError):
        load_alert_config(path)


def test_fractional_breakout_window_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "thresholds.json"
    path.write_text(
        json.dumps(
            {
                "thresholds": {
                    "calm_residual_z": 2.0,
                    "speculative_residual_z": 2.5,
                    "volume_z": 2.5,
                    "rvol": 2.5,
                    "atr_expansion": 1.5,
                    "short_history_multiplier": 1.25,
                    "short_history_return": 0.08,
                    "breakout_min_window": 60.9,
                },
                "classifications": {"TEST": "calm"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AlertConfigError, match="must be an integer"):
        load_alert_config(path)
