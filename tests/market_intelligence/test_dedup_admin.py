from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from market_intelligence.dedup_admin import _resolve_state_path, main
from market_intelligence.dedup_hysteresis import (
    _pending_path_for,
    _REPO_ROOT,
    TickerDedupState,
    load_dedup_state,
    save_dedup_state,
    save_pending_state,
)


def _latched(as_of: str = "2026-06-01", direction: str = "up") -> TickerDedupState:
    return TickerDedupState(
        last_observed_as_of=as_of,
        last_alert_as_of=as_of,
        latched_since=as_of,
        latched=True,
        direction=direction,  # type: ignore[arg-type]
        trigger_z_resid=2.5,
        seen_signal_types=("residual_z",),
        latch_observations=2,
    )


def _armed(as_of: str = "2026-06-01") -> TickerDedupState:
    return TickerDedupState(
        last_observed_as_of=as_of,
        last_alert_as_of=None,
        latched_since=None,
        latched=False,
        direction=None,
        trigger_z_resid=None,
        seen_signal_types=(),
        latch_observations=0,
    )


def _seed(path: Path, states: dict[str, TickerDedupState]) -> None:
    save_dedup_state(states, path)


def test_show_lists_state_per_ticker(tmp_path: Path, capsys) -> None:
    path = tmp_path / "dedup.json"
    _seed(path, {"AAA": _latched(), "BBB": _armed()})

    rc = main(["show", "--state-path", str(path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "AAA" in out and "BBB" in out
    assert "latched" in out  # AAA rendered as latched
    assert "armed" in out  # BBB rendered as armed
    assert "residual_z" in out  # AAA signal types rendered


def test_show_empty_state_is_ok(tmp_path: Path, capsys) -> None:
    path = tmp_path / "dedup.json"  # does not exist
    rc = main(["show", "--state-path", str(path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "0 tickers" in out


def test_reset_ticker_rearms_only_target(tmp_path: Path, capsys) -> None:
    path = tmp_path / "dedup.json"
    _seed(path, {"AAA": _latched(), "BBB": _latched(direction="down")})
    bbb_before = load_dedup_state(path)["BBB"]

    rc = main(["reset", "--ticker", "AAA", "--state-path", str(path)])

    assert rc == 0
    reloaded = load_dedup_state(path)
    assert "AAA" not in reloaded  # re-armed (latch dropped)
    assert reloaded["BBB"] == bbb_before  # untouched, frozen-dataclass equality


def test_reset_ticker_leaves_other_tickers_byte_identical(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    reference = tmp_path / "reference.json"
    _seed(path, {"AAA": _latched(), "BBB": _latched(direction="down")})
    # Independently serialize just BBB to compare its persisted sub-object bytes.
    _seed(reference, {"BBB": _latched(direction="down")})

    main(["reset", "--ticker", "AAA", "--state-path", str(path)])

    assert path.read_bytes() == reference.read_bytes()


def test_reset_all_empties_state(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    _seed(path, {"AAA": _latched(), "BBB": _latched(direction="down")})

    rc = main(["reset", "--all", "--state-path", str(path)])

    assert rc == 0
    assert load_dedup_state(path) == {}
    assert path.exists()  # valid empty state, not a deleted file


def test_reset_unknown_ticker_is_noop_without_corrupting(
    tmp_path: Path, capsys
) -> None:
    path = tmp_path / "dedup.json"
    _seed(path, {"AAA": _latched()})
    before = path.read_bytes()

    rc = main(["reset", "--ticker", "ZZZ", "--state-path", str(path)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "nothing to reset" in out
    assert path.read_bytes() == before  # existing state untouched


@pytest.mark.parametrize("argv", [["reset", "--ticker", "AAA"], ["reset", "--all"], ["show"]])
def test_corrupt_state_is_refused_without_writing(
    tmp_path: Path, argv: list[str]
) -> None:
    path = tmp_path / "dedup.json"
    path.write_text("{", encoding="utf-8")  # invalid JSON
    before = path.read_bytes()

    rc = main([*argv, "--state-path", str(path)])

    assert rc == 1
    assert path.read_bytes() == before  # no write, no truncation
    assert not (tmp_path / ".dedup.json.tmp").exists()


def test_invalid_schema_is_refused_without_writing(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    path.write_text(
        '{"schema_version": 1, "tickers": {"AAA": {}}}', encoding="utf-8"
    )
    before = path.read_bytes()

    rc = main(["reset", "--ticker", "AAA", "--state-path", str(path)])

    assert rc == 1
    assert path.read_bytes() == before


def test_reset_acquires_flock(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    _seed(path, {"AAA": _latched()})
    lock_path = path.with_name(f".{path.name}.lock")

    main(["reset", "--all", "--state-path", str(path)])

    assert lock_path.exists()  # _state_lock was entered


def test_state_path_resolved_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    path = tmp_path / "dedup.json"
    _seed(path, {"AAA": _latched()})
    monkeypatch.setenv("ANOMALY_DEDUP_STATE_PATH", str(path))

    rc = main(["show"])  # no --state-path
    out = capsys.readouterr().out

    assert rc == 0
    assert "AAA" in out


def test_commit_promotes_pending_and_removes_it(tmp_path: Path, capsys) -> None:
    real = tmp_path / "dedup.json"  # absent (never sent before)
    pending = tmp_path / "dedup.pending.json"
    save_pending_state({"AAA": _latched()}, pending, run_id="r1", as_of="2026-06-01")

    rc = main(
        ["commit", "--run-id", "r1", "--state-path", str(real), "--pending-path", str(pending)]
    )

    assert rc == 0
    assert load_dedup_state(real)["AAA"].latched is True  # latch now in real state
    assert not pending.exists()  # pending consumed


def test_commit_advances_last_observed(tmp_path: Path) -> None:
    real = tmp_path / "dedup.json"
    pending = tmp_path / "dedup.pending.json"
    _seed(real, {"AAA": _latched(as_of="2026-06-01")})
    advanced = replace(_latched(as_of="2026-06-01"), last_observed_as_of="2026-06-02")
    save_pending_state({"AAA": advanced}, pending, run_id="r1", as_of="2026-06-02")

    rc = main(
        ["commit", "--run-id", "r1", "--state-path", str(real), "--pending-path", str(pending)]
    )

    assert rc == 0
    assert load_dedup_state(real)["AAA"].last_observed_as_of == "2026-06-02"
    assert not pending.exists()


def test_commit_absent_pending_is_noop(tmp_path: Path, capsys) -> None:
    real = tmp_path / "dedup.json"
    pending = tmp_path / "dedup.pending.json"  # absent

    rc = main(
        ["commit", "--run-id", "r1", "--state-path", str(real), "--pending-path", str(pending)]
    )
    out = capsys.readouterr().out

    assert rc == 0
    assert "nothing to commit" in out
    assert not real.exists()  # nothing written


def test_commit_run_id_mismatch_refuses_without_writing(tmp_path: Path) -> None:
    real = tmp_path / "dedup.json"
    pending = tmp_path / "dedup.pending.json"
    _seed(real, {"AAA": _armed()})
    before = real.read_bytes()
    save_pending_state({"AAA": _latched()}, pending, run_id="r1", as_of="2026-06-01")

    rc = main(
        ["commit", "--run-id", "r2", "--state-path", str(real), "--pending-path", str(pending)]
    )

    assert rc == 1
    assert real.read_bytes() == before  # not promoted
    assert pending.exists()  # not consumed on refusal


def test_commit_corrupt_pending_refuses_without_writing(tmp_path: Path) -> None:
    real = tmp_path / "dedup.json"
    pending = tmp_path / "dedup.pending.json"
    _seed(real, {"AAA": _armed()})
    before = real.read_bytes()
    pending.write_text("{", encoding="utf-8")

    rc = main(
        ["commit", "--run-id", "r1", "--state-path", str(real), "--pending-path", str(pending)]
    )

    assert rc == 1
    assert real.read_bytes() == before
    assert not (tmp_path / ".dedup.json.tmp").exists()


def test_commit_acquires_flock(tmp_path: Path) -> None:
    real = tmp_path / "dedup.json"
    pending = tmp_path / "dedup.pending.json"
    save_pending_state({"AAA": _latched()}, pending, run_id="r1", as_of="2026-06-01")
    lock_path = real.with_name(f".{real.name}.lock")

    main(
        ["commit", "--run-id", "r1", "--state-path", str(real), "--pending-path", str(pending)]
    )

    assert lock_path.exists()


def test_commit_defaults_pending_path_from_state_path(tmp_path: Path) -> None:
    real = tmp_path / "dedup.json"
    pending = _pending_path_for(real)  # sibling default location
    save_pending_state({"AAA": _latched()}, pending, run_id="r1", as_of="2026-06-01")

    rc = main(["commit", "--run-id", "r1", "--state-path", str(real)])  # no --pending-path

    assert rc == 0
    assert load_dedup_state(real)["AAA"].latched is True
    assert not pending.exists()


def test_relative_paths_anchor_at_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # Both --state-path and the env var resolve a relative value the same way:
    # relative to the repo root, never the current working directory.
    expected = _REPO_ROOT / "runtime/market_intelligence/dedup_state.json"
    assert _resolve_state_path("runtime/market_intelligence/dedup_state.json") == expected

    monkeypatch.setenv("ANOMALY_DEDUP_STATE_PATH", "runtime/market_intelligence/dedup_state.json")
    assert _resolve_state_path(None) == expected

    absolute = Path("/tmp/x/dedup.json")
    assert _resolve_state_path(str(absolute)) == absolute


def test_reset_requires_ticker_or_all(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    _seed(path, {"AAA": _latched()})
    before = path.read_bytes()

    with pytest.raises(SystemExit) as exc:
        main(["reset", "--state-path", str(path)])

    assert exc.value.code != 0
    assert path.read_bytes() == before  # argparse errored before any write


def test_reset_ticker_and_all_are_mutually_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "dedup.json"
    with pytest.raises(SystemExit) as exc:
        main(["reset", "--ticker", "AAA", "--all", "--state-path", str(path)])
    assert exc.value.code != 0
