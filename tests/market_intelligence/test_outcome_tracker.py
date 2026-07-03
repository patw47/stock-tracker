"""Epic 5 Sprint 1 — Suivi des outcomes prix.

Couvre les acceptance criteria :
  - alerte + 20 jours de closes → rendements J+1/J+5/J+20 corrects et signés ;
  - candidats gated mesurés au même titre que les survivors ;
  - double exécution → aucune ligne dupliquée ;
  - ticker sans données → outcome unavailable sans crash.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

from market_intelligence import outcome_tracker
from market_intelligence.outcome_tracker import (
    MEASURED,
    UNAVAILABLE,
    AlertEvent,
    iter_events,
    run,
)

AS_OF = date(2026, 5, 1)
TODAY = AS_OF + timedelta(days=40)


def _closes(entry: float, steps: list[float], *, as_of: date = AS_OF) -> pd.Series:
    """Series : close=entry le jour as_of, puis un close par jour ouvré suivant."""
    idx = [pd.Timestamp(as_of)]
    vals = [entry]
    d = pd.Timestamp(as_of)
    for s in steps:
        d = d + pd.tseries.offsets.BDay(1)
        idx.append(d)
        vals.append(s)
    return pd.Series(vals, index=pd.DatetimeIndex(idx))


def _run_record(as_of: date, details: list[dict], *, dry_run: bool = False) -> str:
    return json.dumps({
        "as_of": as_of.isoformat(),
        "dry_run": dry_run,
        "candidates_detail": details,
    })


def _detail(ticker: str, outcome: str, z_resid: float) -> dict:
    return {"ticker": ticker, "outcome": outcome, "z_resid": z_resid,
            "residual_threshold": 2.5, "signal_types": ["residual_z"], "data_issues": []}


def _write_runs(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "runs.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# --- rendements corrects et signés ---------------------------------------


def test_returns_correct_and_signed_up(tmp_path):
    runs = _write_runs(tmp_path, [_run_record(AS_OF, [_detail("AAA", "survived", 3.0)])])
    outcomes = tmp_path / "outcomes.jsonl"
    # entry 100, +1/jour ouvré pendant 20 jours → 101..120
    closes = {"AAA": _closes(100.0, [100.0 + i for i in range(1, 21)])}

    summary = run(runs_path=runs, outcomes_path=outcomes,
                  close_fetcher=lambda: closes, today=TODAY)

    assert summary.measured == 1
    rec = json.loads(outcomes.read_text().strip())
    assert rec["status"] == MEASURED
    assert rec["direction"] == "up"
    assert rec["ret_1d"] == pytest.approx(0.01)
    assert rec["ret_5d"] == pytest.approx(0.05)
    assert rec["ret_20d"] == pytest.approx(0.20)
    assert rec["max_drawup"] == pytest.approx(0.20)
    assert rec["max_drawdown"] == pytest.approx(0.01)


def test_returns_signed_by_down_direction(tmp_path):
    # z_resid négatif → direction down ; prix qui montent → outcome signé négatif.
    runs = _write_runs(tmp_path, [_run_record(AS_OF, [_detail("BBB", "survived", -3.0)])])
    outcomes = tmp_path / "outcomes.jsonl"
    closes = {"BBB": _closes(100.0, [100.0 + i for i in range(1, 21)])}

    run(runs_path=runs, outcomes_path=outcomes, close_fetcher=lambda: closes, today=TODAY)

    rec = json.loads(outcomes.read_text().strip())
    assert rec["direction"] == "down"
    assert rec["ret_20d"] == pytest.approx(-0.20)
    assert rec["ret_1d"] == pytest.approx(-0.01)


# --- gated mesurés comme les survivors -----------------------------------


def test_gated_candidate_measured_like_survivor(tmp_path):
    runs = _write_runs(tmp_path, [_run_record(AS_OF, [
        _detail("AAA", "survived", 3.0),
        _detail("BBB", "gated_dedup:cooldown", 3.0),
        _detail("CCC", "not_candidate", 0.5),  # ignoré
    ])])
    outcomes = tmp_path / "outcomes.jsonl"
    closes = {
        "AAA": _closes(100.0, [100.0 + i for i in range(1, 21)]),
        "BBB": _closes(100.0, [100.0 + i for i in range(1, 21)]),
        "CCC": _closes(100.0, [100.0 + i for i in range(1, 21)]),
    }

    summary = run(runs_path=runs, outcomes_path=outcomes,
                  close_fetcher=lambda: closes, today=TODAY)

    assert summary.measured == 2
    tickers = {json.loads(l)["ticker"] for l in outcomes.read_text().splitlines()}
    assert tickers == {"AAA", "BBB"}  # not_candidate exclu


# --- idempotence ----------------------------------------------------------


def test_double_run_no_duplicate(tmp_path):
    runs = _write_runs(tmp_path, [_run_record(AS_OF, [_detail("AAA", "survived", 3.0)])])
    outcomes = tmp_path / "outcomes.jsonl"
    closes = {"AAA": _closes(100.0, [100.0 + i for i in range(1, 21)])}

    run(runs_path=runs, outcomes_path=outcomes, close_fetcher=lambda: closes, today=TODAY)
    second = run(runs_path=runs, outcomes_path=outcomes, close_fetcher=lambda: closes, today=TODAY)

    assert second.measured == 0
    assert len(outcomes.read_text().strip().splitlines()) == 1


# --- ticker sans données → unavailable, pas de crash ---------------------


def test_no_data_marks_unavailable(tmp_path):
    runs = _write_runs(tmp_path, [_run_record(AS_OF, [_detail("ZZZ", "survived", 3.0)])])
    outcomes = tmp_path / "outcomes.jsonl"

    summary = run(runs_path=runs, outcomes_path=outcomes,
                  close_fetcher=lambda: {}, today=TODAY)

    assert summary.unavailable == 1
    rec = json.loads(outcomes.read_text().strip())
    assert rec["status"] == UNAVAILABLE
    assert rec["reason"] == "no_data"


def test_insufficient_history_not_ready_is_skipped(tmp_path):
    runs = _write_runs(tmp_path, [_run_record(AS_OF, [_detail("AAA", "survived", 3.0)])])
    outcomes = tmp_path / "outcomes.jsonl"
    closes = {"AAA": _closes(100.0, [101.0, 102.0])}  # seulement 2 jours après
    recent_today = AS_OF + timedelta(days=3)  # trop tôt

    summary = run(runs_path=runs, outcomes_path=outcomes,
                  close_fetcher=lambda: closes, today=recent_today)

    assert summary == outcome_tracker.Summary(measured=0, unavailable=0, skipped=1)
    assert not outcomes.exists()


# --- B1 : marge férié + unavailable jamais figé --------------------------


def test_holiday_window_not_frozen_then_measured(tmp_path):
    # Un férié dans la fenêtre décale le 20e close au-delà de J+28. À J+30 il
    # manque encore une barre : l'event doit être SKIPPED (pas figé unavailable),
    # puis mesuré une fois la 20e barre présente.
    runs = _write_runs(tmp_path, [_run_record(AS_OF, [_detail("AAA", "survived", 3.0)])])
    outcomes = tmp_path / "outcomes.jsonl"

    only_19 = {"AAA": _closes(100.0, [100.0 + i for i in range(1, 20)])}  # 19 barres
    s1 = run(runs_path=runs, outcomes_path=outcomes,
             close_fetcher=lambda: only_19, today=AS_OF + timedelta(days=30))
    assert s1 == outcome_tracker.Summary(measured=0, unavailable=0, skipped=1)
    assert not outcomes.exists()  # jamais figé unavailable

    full_20 = {"AAA": _closes(100.0, [100.0 + i for i in range(1, 21)])}  # 20 barres
    s2 = run(runs_path=runs, outcomes_path=outcomes,
             close_fetcher=lambda: full_20, today=AS_OF + timedelta(days=45))
    assert s2.measured == 1
    assert json.loads(outcomes.read_text().strip())["status"] == MEASURED


def test_transient_unavailable_upgraded_to_measured(tmp_path):
    # Fetch dégradé transitoire → unavailable, puis données présentes → measured.
    runs = _write_runs(tmp_path, [_run_record(AS_OF, [_detail("AAA", "survived", 3.0)])])
    outcomes = tmp_path / "outcomes.jsonl"
    today = AS_OF + timedelta(days=45)

    s1 = run(runs_path=runs, outcomes_path=outcomes, close_fetcher=lambda: {}, today=today)
    assert s1.unavailable == 1

    full = {"AAA": _closes(100.0, [100.0 + i for i in range(1, 21)])}
    s2 = run(runs_path=runs, outcomes_path=outcomes, close_fetcher=lambda: full, today=today)
    assert s2.measured == 1

    statuses = [json.loads(line)["status"] for line in outcomes.read_text().splitlines()]
    assert statuses == [UNAVAILABLE, MEASURED]  # upgrade, pas de gel

    # 3e passe : measured est terminal → sauté avant tout comptage.
    s3 = run(runs_path=runs, outcomes_path=outcomes, close_fetcher=lambda: full, today=today)
    assert s3 == outcome_tracker.Summary(measured=0, unavailable=0, skipped=0)


def test_repeated_unavailable_not_duplicated(tmp_path):
    runs = _write_runs(tmp_path, [_run_record(AS_OF, [_detail("AAA", "survived", 3.0)])])
    outcomes = tmp_path / "outcomes.jsonl"
    today = AS_OF + timedelta(days=45)

    run(runs_path=runs, outcomes_path=outcomes, close_fetcher=lambda: {}, today=today)
    s2 = run(runs_path=runs, outcomes_path=outcomes, close_fetcher=lambda: {}, today=today)

    assert s2 == outcome_tracker.Summary(measured=0, unavailable=0, skipped=1)
    assert len(outcomes.read_text().strip().splitlines()) == 1  # pas de doublon unavailable


# --- parsing du journal ---------------------------------------------------


def test_iter_events_skips_dry_run_and_dedups(tmp_path):
    runs = _write_runs(tmp_path, [
        _run_record(AS_OF, [_detail("AAA", "survived", 3.0)], dry_run=True),   # ignoré
        _run_record(AS_OF, [_detail("AAA", "survived", 3.0)]),
        _run_record(AS_OF, [_detail("AAA", "gated_dedup:x", 3.0)]),            # même id
    ])
    events = iter_events(runs)
    assert len(events) == 1
    assert events[0] == AlertEvent(ticker="AAA", as_of=AS_OF, direction="up", outcome="survived")
