from __future__ import annotations

from datetime import date
from pathlib import Path

from market_intelligence import weekly_heartbeat

# Fixed clock: a Wednesday. Its week window is Mon 2026-06-29 .. Fri 2026-07-03.
TODAY = date(2026, 7, 1)


def _rec(
    *,
    timestamp: str,
    dry_run: bool = False,
    candidate_count: int = 0,
    survivor_count: int = 0,
    should_send: bool = False,
    outcomes: tuple[str, ...] = (),
    data_issues: tuple[str, ...] = (),
) -> dict:
    return {
        "timestamp": timestamp,
        "as_of": timestamp[:10],
        "dry_run": dry_run,
        "candidate_count": candidate_count,
        "survivor_count": survivor_count,
        "should_send": should_send,
        "candidates_detail": [{"outcome": o} for o in outcomes],
        "data_issues": list(data_issues),
    }


def test_build_heartbeat_exact_counts_excludes_dryrun_and_other_week() -> None:
    records = [
        _rec(  # official A — Monday, in window
            timestamp="2026-06-29T21:30:00+00:00",
            candidate_count=3,
            survivor_count=1,
            should_send=True,
            outcomes=("survived", "gated_dedup:already_observed", "not_candidate"),
            data_issues=("missing_eod_frame:XYZ",),
        ),
        _rec(  # official B — Wednesday, in window
            timestamp="2026-07-01T21:30:00+00:00",
            candidate_count=2,
            survivor_count=0,
            should_send=False,
            outcomes=("gated_dedup:already_observed", "gated_dedup:below_rearm"),
            data_issues=("missing_eod_frame:XYZ",),
        ),
        _rec(  # dry-run in window — must be excluded
            timestamp="2026-06-30T21:30:00+00:00",
            dry_run=True,
            candidate_count=99,
            survivor_count=50,
            should_send=True,
            outcomes=("gated_dedup:poison",),
            data_issues=("poison",),
        ),
        _rec(  # prior-week official — must be excluded by window
            timestamp="2026-06-26T21:30:00+00:00",
            candidate_count=77,
            survivor_count=7,
            should_send=True,
            outcomes=("gated_dedup:poison",),
            data_issues=("poison",),
        ),
    ]

    msg = weekly_heartbeat.build_heartbeat(records, TODAY)

    assert "Runs officiels : 2" in msg
    assert "Candidats détectés : 5" in msg
    assert "Alertes envoyées : 1" in msg
    assert "• already_observed : 2" in msg
    assert "• below_rearm : 1" in msg
    assert "• missing_eod_frame:XYZ : 2" in msg
    # Excluded records must not leak into the numbers.
    assert "99" not in msg
    assert "poison" not in msg
    assert "50" not in msg


def test_build_heartbeat_zero_candidates_is_alive_not_silent() -> None:
    record = _rec(timestamp="2026-06-29T21:30:00+00:00", candidate_count=0)
    msg = weekly_heartbeat.build_heartbeat([record], TODAY)

    assert msg
    assert "Pipeline vivant" in msg and "Rien à signaler" in msg


def test_build_heartbeat_no_run_is_honest_not_silent() -> None:
    msg = weekly_heartbeat.build_heartbeat([], TODAY)
    assert "Aucun run officiel cette semaine" in msg


def test_module_has_no_llm_or_network() -> None:
    # No LLM client and no network client => proves "aucun appel LLM".
    # ("llm" as a word is skipped: the module docstring legitimately says "zéro LLM".)
    source = Path(weekly_heartbeat.__file__).read_text(encoding="utf-8").lower()
    for forbidden in ("anthropic", "openai", "requests", "httpx", "urllib", "socket"):
        assert forbidden not in source
