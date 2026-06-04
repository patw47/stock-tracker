from __future__ import annotations

import json
import math

import pandas as pd
import pytest

from market_intelligence.anomaly_signals import calculate_all, calculate_signals


def _frame(
    closes: list[float],
    *,
    opens: list[float] | None = None,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
    volumes: list[float] | None = None,
) -> pd.DataFrame:
    size = len(closes)
    return pd.DataFrame(
        {
            "open": opens or closes,
            "high": highs or [close + 1 for close in closes],
            "low": lows or [close - 1 for close in closes],
            "close": closes,
            "volume": volumes or [100.0] * size,
        },
        index=pd.date_range("2024-01-01", periods=size, freq="B"),
    )


def test_calculates_latest_return_gap_and_rvol_excluding_current_bar() -> None:
    frame = _frame(
        [100.0, 110.0, 121.0],
        opens=[100.0, 110.0, 115.5],
        volumes=[100.0, 100.0, 300.0],
    )

    result = calculate_signals("TEST", frame)

    assert result.daily_return == pytest.approx(0.1)
    assert result.opening_gap == pytest.approx(0.05)
    assert result.rvol == pytest.approx(3.0)
    assert result.return_median == pytest.approx(0.1)
    assert result.return_window == 1
    assert result.volume_window == 2


def test_robust_z_uses_prior_returns_and_returns_none_for_zero_mad() -> None:
    frame = _frame([100.0, 101.0, 102.01, 103.0301, 110.0])

    result = calculate_signals("TEST", frame)

    assert result.return_mad == pytest.approx(0.0)
    assert result.return_robust_z is None


def test_robust_z_matches_modified_z_score_formula() -> None:
    frame = _frame([100.0, 101.0, 103.02, 106.1106, 116.72166])

    result = calculate_signals("TEST", frame)

    assert result.return_median == pytest.approx(0.02)
    assert result.return_mad == pytest.approx(0.01)
    assert result.return_robust_z == pytest.approx(0.6745 * (0.1 - 0.02) / 0.01)


def test_true_range_and_atr_use_prior_fourteen_bars() -> None:
    closes = [100.0] * 15
    frame = _frame(
        closes,
        opens=closes,
        highs=[101.0] * 14 + [110.0],
        lows=[99.0] * 15,
    )

    result = calculate_signals("TEST", frame)

    assert result.true_range == pytest.approx(11.0)
    assert result.atr14 == pytest.approx(2.0)
    assert result.atr_expansion_ratio == pytest.approx(5.5)


def test_detects_breakouts_against_prior_history() -> None:
    frame = _frame(
        [10.0, 10.0, 11.0],
        highs=[11.0, 12.0, 13.0],
        lows=[9.0, 8.0, 7.0],
    )

    result = calculate_signals("TEST", frame)

    assert result.high_52w == pytest.approx(13.0)
    assert result.low_52w == pytest.approx(7.0)
    assert result.breakout_high_52w is True
    assert result.breakout_low_52w is True
    assert result.extrema_window == 3


def test_caps_rolling_windows_and_marks_only_52_week_fallback() -> None:
    frame = _frame([float(value) for value in range(100, 201)])

    result = calculate_signals("TEST", frame)

    assert result.short_history is False
    assert result.return_window == 60
    assert result.volume_window == 20
    assert result.extrema_window == 101
    assert result.fallback_applied is True


def test_full_history_disables_fallback() -> None:
    frame = _frame([float(value) for value in range(100, 353)])

    result = calculate_signals("TEST", frame)

    assert result.return_window == 60
    assert result.volume_window == 20
    assert result.extrema_window == 252
    assert result.fallback_applied is False


def test_missing_data_only_makes_affected_metrics_unavailable() -> None:
    frame = _frame([100.0, 101.0])
    frame.loc[frame.index[-1], ["open", "volume"]] = float("nan")

    result = calculate_signals("TEST", frame)

    assert result.daily_return == pytest.approx(0.01)
    assert result.opening_gap is None
    assert result.rvol is None
    assert "missing_values" in result.data_issues


def test_empty_and_missing_columns_are_explicit_and_json_compatible() -> None:
    empty = calculate_signals("EMPTY", pd.DataFrame())
    partial = calculate_signals(
        "PARTIAL",
        pd.DataFrame({"close": [100.0]}, index=pd.to_datetime(["2024-01-01"])),
    )

    assert empty.as_of is None
    assert empty.data_issues == ("empty_frame",)
    assert "missing_column:volume" in partial.data_issues
    assert "NaN" not in json.dumps(partial.to_dict())
    assert "Infinity" not in json.dumps(partial.to_dict())


def test_duplicate_dates_are_deduplicated_and_sorted() -> None:
    frame = _frame([100.0, 101.0, 102.0])
    frame.index = pd.to_datetime(["2024-01-02", "2024-01-01", "2024-01-02"])

    result = calculate_signals("TEST", frame)

    assert result.bar_count == 2
    assert result.as_of == "2024-01-02"
    assert "duplicate_date" in result.data_issues
    assert result.daily_return == pytest.approx(102.0 / 101.0 - 1)


def test_calculate_all_does_not_abort_on_empty_frame() -> None:
    results = calculate_all({"GOOD": _frame([100.0, 101.0]), "EMPTY": pd.DataFrame()})

    assert results["GOOD"].daily_return == pytest.approx(0.01)
    assert results["EMPTY"].daily_return is None


def test_outputs_never_contain_non_finite_floats() -> None:
    frame = _frame([0.0, 100.0], volumes=[0.0, 0.0])

    values = calculate_signals("TEST", frame).to_dict().values()

    assert not any(
        isinstance(value, float) and not math.isfinite(value) for value in values
    )
