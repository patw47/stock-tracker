from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from market_intelligence.candidate_alerts import CandidateAlert
from market_intelligence.dedup_hysteresis import (
    DedupConfig,
    DedupInputError,
    DedupStateError,
    deduplicate_alerts,
    load_dedup_config,
    load_dedup_state,
)
from market_intelligence.short_interest import ShortInterestResult


def _candidate(**overrides: object) -> CandidateAlert:
    base = CandidateAlert(
        ticker="TEST",
        as_of="2026-06-01",
        classification="calm",
        eligible=True,
        is_candidate=True,
        direction="up",
        signal_types=("residual_z",),
        z_resid=2.5,
        residual_threshold=2.0,
        short_history_fallback_applied=False,
        data_issues=(),
    )
    return replace(base, **overrides)


def _short_interest(**overrides: object) -> ShortInterestResult:
    base = ShortInterestResult(
        ticker="TEST",
        api_symbol="TEST",
        short_percent_float=0.25,
        shares_short=1_000_000,
        days_to_cover=6.0,
        squeeze_prone=True,
        coverage_status="covered",
        data_issues=(),
    )
    return replace(base, **overrides)


def _config(
    *,
    rearm_z: float = 1.0,
    escalation_z_delta: float = 1.0,
    max_latch_observations: int = 10,
) -> DedupConfig:
    return DedupConfig(rearm_z, escalation_z_delta, max_latch_observations)


def _run(
    path: Path,
    candidate: CandidateAlert,
    short_interest: ShortInterestResult | None = None,
    config: DedupConfig | None = None,
) -> tuple[object, ...]:
    short_results = {} if short_interest is None else {candidate.ticker: short_interest}
    return deduplicate_alerts(
        {candidate.ticker: candidate},
        short_results,
        state_path=path,
        config=config or _config(),
    )


def test_first_candidate_fires_and_persists_latched_state(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"

    alerts = _run(path, _candidate())
    state = load_dedup_state(path)["TEST"]

    assert len(alerts) == 1
    assert alerts[0].fire_reason == "initial"
    assert state.latched is True
    assert state.direction == "up"
    assert state.trigger_z_resid == pytest.approx(2.5)
    assert state.seen_signal_types == ("residual_z",)
    assert state.latch_observations == 1


def test_persistent_event_and_same_day_replay_fire_only_once(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"

    assert len(_run(path, _candidate())) == 1
    assert _run(path, _candidate()) == ()
    assert _run(path, _candidate(as_of="2026-06-02", z_resid=2.6)) == ()

    state = load_dedup_state(path)["TEST"]
    assert state.last_observed_as_of == "2026-06-02"
    assert state.latch_observations == 2


def test_valid_calm_day_rearms_then_next_candidate_fires(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    _run(path, _candidate())

    calm = _candidate(
        as_of="2026-06-02",
        is_candidate=False,
        direction=None,
        signal_types=(),
        z_resid=0.99,
    )
    assert _run(path, calm) == ()
    assert load_dedup_state(path)["TEST"].latched is False

    alerts = _run(path, _candidate(as_of="2026-06-03", z_resid=2.6))
    assert len(alerts) == 1
    assert alerts[0].fire_reason == "initial"


@pytest.mark.parametrize("z_resid", [1.0, -1.0, None])
def test_boundary_or_missing_z_does_not_rearm(
    tmp_path: Path, z_resid: float | None
) -> None:
    path = tmp_path / "dedup.json"
    _run(path, _candidate())

    calm = _candidate(
        as_of="2026-06-02",
        is_candidate=False,
        direction=None,
        signal_types=(),
        z_resid=z_resid,
    )
    _run(path, calm)

    assert load_dedup_state(path)["TEST"].latched is True


@pytest.mark.parametrize(
    ("candidate", "reason"),
    [
        (
            _candidate(as_of="2026-06-02", direction="down", z_resid=-2.6),
            "direction_reversal",
        ),
        (
            _candidate(
                as_of="2026-06-02",
                signal_types=("residual_z", "atr_expansion"),
                z_resid=2.6,
            ),
            "new_signal_type",
        ),
        (_candidate(as_of="2026-06-02", z_resid=3.5), "escalation"),
    ],
)
def test_overrides_fire_and_update_latch(
    tmp_path: Path, candidate: CandidateAlert, reason: str
) -> None:
    path = tmp_path / "dedup.json"
    _run(path, _candidate())

    alerts = _run(path, candidate)
    state = load_dedup_state(path)["TEST"]

    assert len(alerts) == 1
    assert alerts[0].fire_reason == reason
    assert state.last_alert_as_of == "2026-06-02"
    assert state.latch_observations == (
        1 if reason == "direction_reversal" else 2
    )


def test_seen_new_type_and_escalation_do_not_repeat(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    _run(path, _candidate())
    with_type = _candidate(
        as_of="2026-06-02",
        signal_types=("residual_z", "atr_expansion"),
        z_resid=2.6,
    )
    assert len(_run(path, with_type)) == 1

    assert _run(
        path,
        replace(with_type, as_of="2026-06-03", signal_types=("residual_z",)),
    ) == ()
    assert _run(path, replace(with_type, as_of="2026-06-04")) == ()
    assert _run(path, replace(with_type, as_of="2026-06-05", z_resid=3.59)) == ()


def test_new_type_and_escalation_preserve_max_latch_clock(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    _run(path, _candidate())

    _run(
        path,
        _candidate(
            as_of="2026-06-02",
            signal_types=("residual_z", "atr_expansion"),
            z_resid=2.6,
        ),
    )
    _run(path, _candidate(as_of="2026-06-03", z_resid=3.6))
    state = load_dedup_state(path)["TEST"]

    assert state.latched_since == "2026-06-01"
    assert state.latch_observations == 3


def test_squeeze_true_is_a_candidate_override_but_unknown_never_clears_it(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dedup.json"
    _run(path, _candidate())

    alerts = _run(
        path,
        _candidate(as_of="2026-06-02", z_resid=2.6),
        _short_interest(),
    )
    assert len(alerts) == 1
    assert alerts[0].fire_reason == "new_signal_type"
    assert alerts[0].squeeze_prone is True
    assert alerts[0].signal_types == ("residual_z", "squeeze_prone")

    assert _run(path, _candidate(as_of="2026-06-03", z_resid=2.7)) == ()
    state = load_dedup_state(path)["TEST"]
    assert state.seen_signal_types == ("residual_z", "squeeze_prone")


def test_squeeze_never_creates_an_alert_without_s3_candidate(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    decision = _candidate(
        is_candidate=False,
        direction=None,
        signal_types=(),
        z_resid=1.5,
    )

    assert _run(path, decision, _short_interest()) == ()
    assert load_dedup_state(path)["TEST"].seen_signal_types == ()


def test_max_latch_valve_refires_on_eleventh_valid_observation(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    config = _config(max_latch_observations=3)
    assert len(_run(path, _candidate(), config=config)) == 1
    assert _run(path, _candidate(as_of="2026-06-02"), config=config) == ()
    assert _run(path, _candidate(as_of="2026-06-03"), config=config) == ()

    alerts = _run(path, _candidate(as_of="2026-06-04"), config=config)

    assert len(alerts) == 1
    assert alerts[0].fire_reason == "max_latch_expired"


def test_fallback_candidate_without_z_latches_until_override_or_valve(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dedup.json"
    fallback = _candidate(
        signal_types=("short_history_return", "rvol", "atr_expansion"),
        z_resid=None,
        short_history_fallback_applied=True,
    )

    assert len(_run(path, fallback)) == 1
    assert _run(path, replace(fallback, as_of="2026-06-02")) == ()
    assert load_dedup_state(path)["TEST"].latched is True


def test_ineligible_and_out_of_order_decisions_do_not_mutate_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dedup.json"
    _run(path, _candidate(as_of="2026-06-03"))
    before = path.read_text(encoding="utf-8")

    assert _run(path, _candidate(as_of="2026-06-04", eligible=False)) == ()
    assert _run(path, _candidate(as_of="2026-06-02", z_resid=4.0)) == ()

    assert path.read_text(encoding="utf-8") == before


def test_multiple_tickers_are_independent(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    first = _candidate()
    second = _candidate(ticker="OTHER")

    alerts = deduplicate_alerts(
        {"TEST": first, "OTHER": second}, state_path=path, config=_config()
    )

    assert {alert.candidate.ticker for alert in alerts} == {"TEST", "OTHER"}
    assert set(load_dedup_state(path)) == {"TEST", "OTHER"}


def test_corrupt_or_invalid_state_is_rejected_explicitly(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{", encoding="utf-8")
    with pytest.raises(DedupStateError):
        load_dedup_state(corrupt)

    invalid = tmp_path / "invalid.json"
    invalid.write_text(
        json.dumps({"schema_version": 1, "tickers": {"TEST": {}}}),
        encoding="utf-8",
    )
    with pytest.raises(DedupStateError):
        load_dedup_state(invalid)


def test_malformed_eligible_decision_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DedupInputError):
        deduplicate_alerts(
            {"WRONG": _candidate()},
            state_path=tmp_path / "dedup.json",
            config=_config(),
        )


def test_mismatched_short_interest_ticker_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(DedupInputError):
        _run(
            tmp_path / "dedup.json",
            _candidate(),
            _short_interest(ticker="OTHER"),
        )


def test_default_config_matches_sprint_5_policy() -> None:
    config = load_dedup_config()

    assert config.rearm_z == pytest.approx(1.0)
    assert config.escalation_z_delta == pytest.approx(1.0)
    assert config.max_latch_observations == 10
