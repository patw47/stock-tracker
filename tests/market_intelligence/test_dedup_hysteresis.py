from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from market_intelligence.candidate_alerts import CandidateAlert
from market_intelligence.dedup_hysteresis import (
    _DEFAULT_STATE_PATH,
    DedupConfig,
    DedupInputError,
    DedupStateError,
    SuppressionDetail,
    deduplicate_alerts,
    default_pending_path,
    load_dedup_config,
    load_dedup_state,
    load_pending_state,
    save_pending_state,
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
    assert alerts[0].prev_trigger_z_resid is None
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


def test_state_file_suppresses_alert_across_fresh_invocations(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    first = _candidate()

    assert len(deduplicate_alerts({"TEST": first}, state_path=path)) == 1
    second = _candidate(as_of="2026-06-02", z_resid=2.6)

    assert deduplicate_alerts({"TEST": second}, state_path=path) == ()


def test_same_day_calm_observation_does_not_rearm(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    _run(path, _candidate())

    calm_same_day = _candidate(
        is_candidate=False,
        direction=None,
        signal_types=(),
        z_resid=0.5,
    )

    assert _run(path, calm_same_day) == ()
    assert load_dedup_state(path)["TEST"].latched is True


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
    # On an escalation the previous trigger level (the initial 2.5) is carried so
    # the digest can say "il avait déclenché à …"; None for every other reason.
    assert alerts[0].prev_trigger_z_resid == (2.5 if reason == "escalation" else None)
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


def test_max_latch_valve_refreshes_state_without_refiring_same_event(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dedup.json"
    config = _config(max_latch_observations=3)
    assert len(_run(path, _candidate(), config=config)) == 1
    assert _run(path, _candidate(as_of="2026-06-02"), config=config) == ()
    assert _run(path, _candidate(as_of="2026-06-03"), config=config) == ()

    alerts = _run(path, _candidate(as_of="2026-06-04"), config=config)
    state = load_dedup_state(path)["TEST"]

    assert alerts == ()
    assert state.latched is True
    assert state.latched_since == "2026-06-04"
    assert state.last_alert_as_of == "2026-06-01"
    assert state.trigger_z_resid == pytest.approx(2.5)
    assert state.latch_observations == 1


def test_ineligible_dated_observations_advance_latch_valve(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    config = _config(max_latch_observations=2)
    _run(path, _candidate(), config=config)

    stale = _candidate(
        as_of="2026-06-02",
        eligible=False,
        is_candidate=False,
        direction=None,
        signal_types=(),
        z_resid=None,
    )
    assert _run(path, stale, config=config) == ()
    assert load_dedup_state(path)["TEST"].latch_observations == 2

    missing = replace(stale, as_of="2026-06-03")
    assert _run(path, missing, config=config) == ()
    state = load_dedup_state(path)["TEST"]
    assert state.latched is False
    assert state.last_observed_as_of == "2026-06-03"


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


def test_out_of_order_decisions_do_not_mutate_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "dedup.json"
    _run(path, _candidate(as_of="2026-06-03"))
    before = path.read_text(encoding="utf-8")

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


def test_readonly_survivors_match_normal_run(tmp_path: Path) -> None:
    normal_path = tmp_path / "normal.json"
    readonly_path = tmp_path / "readonly.json"

    # Seed both files with an identical latched state.
    assert len(_run(normal_path, _candidate())) == 1
    assert len(_run(readonly_path, _candidate())) == 1

    escalation = _candidate(as_of="2026-06-02", z_resid=3.5)
    normal_alerts = _run(normal_path, escalation)
    readonly_alerts = deduplicate_alerts(
        {escalation.ticker: escalation},
        state_path=readonly_path,
        config=_config(),
        readonly=True,
    )

    assert len(readonly_alerts) == len(normal_alerts) == 1
    assert readonly_alerts[0].fire_reason == normal_alerts[0].fire_reason
    assert readonly_alerts[0].signal_types == normal_alerts[0].signal_types


def test_readonly_leaves_existing_state_byte_for_byte(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    _run(path, _candidate())
    before_bytes = path.read_bytes()
    before_hash = hashlib.sha256(before_bytes).hexdigest()

    # An overriding, later-dated candidate would normally mutate the latch.
    escalation = _candidate(as_of="2026-06-02", z_resid=3.5)
    alerts = deduplicate_alerts(
        {escalation.ticker: escalation},
        state_path=path,
        config=_config(),
        readonly=True,
    )

    assert len(alerts) == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash
    # Persisted state still reflects the pre-readonly run, not the escalation.
    assert load_dedup_state(path)["TEST"].last_observed_as_of == "2026-06-01"


def test_readonly_does_not_create_missing_state_file(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    assert not path.exists()

    alerts = deduplicate_alerts(
        {"TEST": _candidate()},
        state_path=path,
        config=_config(),
        readonly=True,
    )

    assert len(alerts) == 1
    assert alerts[0].fire_reason == "initial"
    assert not path.exists()


def test_readonly_still_acquires_flock(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    lock_path = path.with_name(f".{path.name}.lock")

    deduplicate_alerts(
        {"TEST": _candidate()},
        state_path=path,
        config=_config(),
        readonly=True,
    )

    assert lock_path.exists()
    assert not path.exists()


def test_readonly_env_var_matches_readonly_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "dedup.json"
    monkeypatch.setenv("ANOMALY_DEDUP_READONLY", "1")

    alerts = _run(path, _candidate())

    assert len(alerts) == 1
    assert not path.exists()


def test_readonly_false_still_persists_state(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"

    alerts = deduplicate_alerts(
        {"TEST": _candidate()},
        state_path=path,
        config=_config(),
        readonly=False,
    )

    assert len(alerts) == 1
    assert path.exists()
    assert load_dedup_state(path)["TEST"].latched is True


def test_save_and_load_pending_roundtrips_run_id_and_as_of(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    _run(path, _candidate())  # produce a valid latched state
    states = load_dedup_state(path)
    pending = tmp_path / "dedup.pending.json"

    save_pending_state(states, pending, run_id="r1", as_of="2026-06-01")
    loaded = load_pending_state(pending)

    assert loaded.run_id == "r1"
    assert loaded.as_of == "2026-06-01"
    assert loaded.tickers == states
    assert not (tmp_path / ".dedup.pending.json.tmp").exists()


@pytest.mark.parametrize(
    "content",
    [
        "{",
        json.dumps({"schema_version": 2, "run_id": "r1", "tickers": {}}),
        json.dumps({"schema_version": 1, "tickers": {}}),  # missing run_id
        json.dumps({"schema_version": 1, "run_id": "r1"}),  # missing tickers
    ],
)
def test_load_pending_rejects_invalid(tmp_path: Path, content: str) -> None:
    pending = tmp_path / "dedup.pending.json"
    pending.write_text(content, encoding="utf-8")
    with pytest.raises(DedupStateError):
        load_pending_state(pending)


def test_default_pending_path_derives_from_state_path() -> None:
    assert default_pending_path() == _DEFAULT_STATE_PATH.with_name(
        f"{_DEFAULT_STATE_PATH.stem}.pending.json"
    )
    assert default_pending_path().name == "dedup_state.pending.json"


def test_deduplicate_with_pending_writes_pending_not_real(tmp_path: Path) -> None:
    real = tmp_path / "dedup.json"
    pending = tmp_path / "dedup.pending.json"

    alerts = deduplicate_alerts(
        {"TEST": _candidate()},
        state_path=real,
        config=_config(),
        pending_path=pending,
        run_id="r1",
        run_as_of="2026-06-01",
    )

    assert len(alerts) == 1 and alerts[0].fire_reason == "initial"
    assert not real.exists()  # real state untouched (not created)
    loaded = load_pending_state(pending)
    assert loaded.run_id == "r1"
    assert loaded.tickers["TEST"].latched is True


def test_pending_write_refires_same_candidate_next_run(tmp_path: Path) -> None:
    real = tmp_path / "dedup.json"
    pending = tmp_path / "dedup.pending.json"

    # Run 1: stage pending, simulate Telegram failure => NO commit.
    first = deduplicate_alerts(
        {"TEST": _candidate()},
        state_path=real,
        config=_config(),
        pending_path=pending,
        run_id="r1",
        run_as_of="2026-06-01",
    )
    assert len(first) == 1

    # Run 2: real state still empty => same candidate refires.
    second = deduplicate_alerts(
        {"TEST": _candidate()},
        state_path=real,
        config=_config(),
        pending_path=pending,
        run_id="r2",
        run_as_of="2026-06-01",
    )

    assert len(second) == 1 and second[0].fire_reason == "initial"
    assert not real.exists()
    assert load_pending_state(pending).run_id == "r2"


def test_pending_written_even_with_zero_survivors(tmp_path: Path) -> None:
    real = tmp_path / "dedup.json"
    pending = tmp_path / "dedup.pending.json"
    _run(real, _candidate())  # latch TEST at 2026-06-01
    before = real.read_bytes()

    calm = _candidate(
        as_of="2026-06-02",
        is_candidate=False,
        direction=None,
        signal_types=(),
        z_resid=0.5,
    )
    alerts = deduplicate_alerts(
        {"TEST": calm},
        state_path=real,
        config=_config(),
        pending_path=pending,
        run_id="r1",
        run_as_of="2026-06-02",
    )

    assert alerts == ()  # no new survivor
    assert real.read_bytes() == before  # real untouched
    # Advance (last_observed) staged in pending only.
    assert load_pending_state(pending).tickers["TEST"].last_observed_as_of == "2026-06-02"


def test_orphan_pending_overwritten_with_warning(tmp_path: Path, caplog) -> None:
    real = tmp_path / "dedup.json"
    pending = tmp_path / "dedup.pending.json"
    save_pending_state(
        load_dedup_state(_seed_latched(tmp_path)),
        pending,
        run_id="old",
        as_of="2000-01-01",
    )

    with caplog.at_level("WARNING", logger="market_intelligence.dedup_hysteresis"):
        alerts = deduplicate_alerts(
            {"TEST": _candidate()},
            state_path=real,
            config=_config(),
            pending_path=pending,
            run_id="r1",
            run_as_of="2026-06-01",
        )

    assert any("orphan pending" in rec.message for rec in caplog.records)
    # Orphan latch never read into the real-state computation => fires initial.
    assert len(alerts) == 1 and alerts[0].fire_reason == "initial"
    assert load_pending_state(pending).run_id == "r1"


def _seed_latched(tmp_path: Path) -> Path:
    seed = tmp_path / "seed.json"
    deduplicate_alerts({"TEST": _candidate()}, state_path=seed, config=_config())
    return seed


def test_pending_ignored_when_readonly(tmp_path: Path) -> None:
    real = tmp_path / "dedup.json"
    pending = tmp_path / "dedup.pending.json"

    alerts = deduplicate_alerts(
        {"TEST": _candidate()},
        state_path=real,
        config=_config(),
        pending_path=pending,
        run_id="r1",
        readonly=True,
    )

    assert len(alerts) == 1  # survivor computed
    assert not pending.exists()  # readonly wins: nothing staged
    assert not real.exists()


def test_two_phase_leaves_real_state_byte_for_byte(tmp_path: Path) -> None:
    real = tmp_path / "dedup.json"
    pending = tmp_path / "dedup.pending.json"
    _run(real, _candidate())
    before_hash = hashlib.sha256(real.read_bytes()).hexdigest()

    escalation = _candidate(as_of="2026-06-02", z_resid=3.5)
    deduplicate_alerts(
        {"TEST": escalation},
        state_path=real,
        config=_config(),
        pending_path=pending,
        run_id="r1",
        run_as_of="2026-06-02",
    )

    assert hashlib.sha256(real.read_bytes()).hexdigest() == before_hash
    assert load_dedup_state(real)["TEST"].last_observed_as_of == "2026-06-01"
    # The escalated state is staged in pending.
    assert load_pending_state(pending).tickers["TEST"].last_observed_as_of == "2026-06-02"


def test_pending_requires_run_id(tmp_path: Path) -> None:
    with pytest.raises(DedupInputError):
        deduplicate_alerts(
            {"TEST": _candidate()},
            state_path=tmp_path / "dedup.json",
            config=_config(),
            pending_path=tmp_path / "dedup.pending.json",
        )


def test_suppressions_record_already_observed_replay_nuai(tmp_path: Path) -> None:
    # Incident 2026-07-01: NUAI already latched (by a manual run), the real -4.18σ
    # candidate is gated as already_observed and must be observable.
    path = tmp_path / "dedup.json"
    _run(path, _candidate())  # latch TEST at 2026-06-01

    sup: list[SuppressionDetail] = []
    alerts = deduplicate_alerts(
        {"TEST": _candidate(z_resid=-4.18, direction="down")},  # same as_of => gated
        state_path=path,
        config=_config(),
        suppressions=sup,
    )

    assert alerts == ()
    assert sup == [SuppressionDetail("TEST", -4.18, "already_observed")]


@pytest.mark.parametrize(
    ("config", "second", "reason", "z"),
    [
        (_config(), _candidate(as_of="2026-06-02", z_resid=0.5), "below_rearm", 0.5),
        (
            _config(max_latch_observations=1),
            _candidate(as_of="2026-06-02", z_resid=2.6),
            "max_latch_refresh",
            2.6,
        ),
        (
            _config(),
            _candidate(as_of="2026-06-02", z_resid=2.6),
            "latched_no_override",
            2.6,
        ),
    ],
)
def test_suppression_reason_per_scenario(
    tmp_path: Path,
    config: DedupConfig,
    second: CandidateAlert,
    reason: str,
    z: float,
) -> None:
    path = tmp_path / "dedup.json"
    _run(path, _candidate(), config=config)

    sup: list[SuppressionDetail] = []
    alerts = deduplicate_alerts(
        {"TEST": second}, state_path=path, config=config, suppressions=sup
    )

    assert alerts == ()
    assert sup == [SuppressionDetail("TEST", z, reason)]


def test_suppressions_omit_survivor_noncandidate_and_ineligible(tmp_path: Path) -> None:
    # (a) A firing candidate is a survivor, not a suppression.
    path_a = tmp_path / "a.json"
    sup_a: list[SuppressionDetail] = []
    assert len(deduplicate_alerts(
        {"TEST": _candidate()}, state_path=path_a, config=_config(), suppressions=sup_a
    )) == 1
    assert sup_a == []

    # (b) Eligible non-candidate calm observation after a latch: not gated_dedup.
    path_b = tmp_path / "b.json"
    _run(path_b, _candidate(), config=_config())
    calm = _candidate(
        as_of="2026-06-02", is_candidate=False, direction=None, signal_types=(), z_resid=0.5
    )
    sup_b: list[SuppressionDetail] = []
    deduplicate_alerts({"TEST": calm}, state_path=path_b, config=_config(), suppressions=sup_b)
    assert sup_b == []

    # (c) Ineligible decision: never a gated candidate.
    path_c = tmp_path / "c.json"
    ineligible = _candidate(
        eligible=False, is_candidate=False, direction=None, signal_types=(), z_resid=None
    )
    sup_c: list[SuppressionDetail] = []
    deduplicate_alerts(
        {"TEST": ineligible}, state_path=path_c, config=_config(), suppressions=sup_c
    )
    assert sup_c == []


def test_suppressions_arg_is_backward_compatible(tmp_path: Path) -> None:
    without = tmp_path / "without.json"
    with_sink = tmp_path / "with.json"
    _run(without, _candidate())
    _run(with_sink, _candidate())

    escalation = _candidate(as_of="2026-06-02", z_resid=3.5)
    plain = deduplicate_alerts({"TEST": escalation}, state_path=without, config=_config())
    sunk = deduplicate_alerts(
        {"TEST": escalation}, state_path=with_sink, config=_config(), suppressions=[]
    )

    assert len(plain) == len(sunk) == 1
    assert plain[0].fire_reason == sunk[0].fire_reason


def test_default_config_matches_sprint_5_policy() -> None:
    config = load_dedup_config()

    assert config.rearm_z == pytest.approx(1.0)
    assert config.escalation_z_delta == pytest.approx(1.0)
    assert config.max_latch_observations == 10
