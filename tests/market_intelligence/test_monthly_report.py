"""Epic 5 Sprint 2 — Rapport mensuel de track record.

Couvre les acceptance criteria :
  - rapport depuis un outcomes.jsonl de test → agrégats exacts ;
  - moins de N événements → mention explicite d'échantillon insuffisant ;
  - message conforme au format Telegram sûr (pas de #, échappé) ;
  - (bonus) top regret, filtre de période, jointure signal_types.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from market_intelligence import monthly_report as mr
from market_intelligence.monthly_report import build_report, run

JUNE = (date(2026, 6, 1), date(2026, 6, 30))
IN_JUNE = "2026-06-15"


def _survived(ticker: str, r1: float, r5: float, r20: float, *, as_of: str = IN_JUNE) -> dict:
    return {
        "event_id": f"{ticker}:{as_of}", "ticker": ticker, "as_of": as_of,
        "status": "measured", "outcome": "survived",
        "ret_1d": r1, "ret_5d": r5, "ret_20d": r20,
    }


def _gated(ticker: str, r20: float, *, as_of: str = IN_JUNE) -> dict:
    return {
        "event_id": f"{ticker}:{as_of}", "ticker": ticker, "as_of": as_of,
        "status": "measured", "outcome": "gated_dedup:cooldown", "ret_20d": r20,
    }


def _ten_survivors() -> list[dict]:
    # 7 gagnants (ret_20d=+0.05), 3 perdants (ret_20d=-0.05) ; J+1=+0.02, J+5=+0.03.
    events = [_survived(f"WIN{i}", 0.02, 0.03, 0.05) for i in range(7)]
    events += [_survived(f"LOSE{i}", 0.02, 0.03, -0.05) for i in range(3)]
    return events


# --- agrégats exacts ------------------------------------------------------


def test_aggregates_exact():
    classifications = {f"WIN{i}": "speculative" for i in range(7)}
    classifications.update({f"LOSE{i}": "calm" for i in range(3)})

    msg = build_report(_ten_survivors(), period_start=JUNE[0], period_end=JUNE[1],
                       classifications=classifications)

    assert "Alertes envoyées : 10 · candidats gated : 0" in msg
    assert "J+1 : +2.0%" in msg
    assert "J+5 : +3.0%" in msg
    assert "J+20 : +5.0%" in msg
    assert "Continuation J+20 : 70% · réversion : 30%" in msg
    assert "Par classification : speculative=7, calm=3" in msg


def test_signal_types_join():
    signal_types = {f"WIN{i}:{IN_JUNE}": ("residual_z",) for i in range(7)}
    signal_types.update({f"LOSE{i}:{IN_JUNE}": ("volume_z",) for i in range(3)})

    msg = build_report(_ten_survivors(), period_start=JUNE[0], period_end=JUNE[1],
                       signal_types=signal_types)

    assert "Par signal : residual_z=7, volume_z=3" in msg


# --- échantillon insuffisant ---------------------------------------------


def test_small_sample_insufficient():
    events = [_survived("AAA", 0.9, 0.9, 0.9)]  # 1 seul → pas de stats
    msg = build_report(events, period_start=JUNE[0], period_end=JUNE[1])

    assert f"&lt; {mr.MIN_SAMPLE}" in msg
    assert "Échantillon insuffisant" in msg
    assert "Continuation" not in msg  # pas de pourcentage trompeur
    assert "J+20 :" not in msg


# --- format Telegram sûr --------------------------------------------------


def test_telegram_safe_no_hash_and_escaped():
    events = _ten_survivors()
    events.append({
        "event_id": "ZZZ:2026-06-10", "ticker": "ZZZ", "as_of": "2026-06-10",
        "status": "unavailable", "outcome": "survived", "reason": "fetch&<err>",
    })
    msg = build_report(events, period_start=JUNE[0], period_end=JUNE[1])

    assert "#" not in msg  # aucun heading markdown brut
    assert "fetch&amp;&lt;err&gt;" in msg  # reason échappée
    assert "fetch&<err>" not in msg


# --- top regret -----------------------------------------------------------


def test_top_regret_biggest_move():
    events = _ten_survivors() + [
        _gated("SMALL", 0.03),
        _gated("BIG", -0.15),  # plus gros mouvement post-filtrage (|.|)
    ]
    msg = build_report(events, period_start=JUNE[0], period_end=JUNE[1])

    assert "Top regret (gated) : BIG -15.0% à J+20" in msg


# --- filtre de période ----------------------------------------------------


def test_period_filter_excludes_other_months():
    events = _ten_survivors() + [_survived("OLD", 0.5, 0.5, 0.5, as_of="2026-05-15")]
    msg = build_report(events, period_start=JUNE[0], period_end=JUNE[1])

    assert "Alertes envoyées : 10 " in msg  # l'événement de mai est exclu


# --- intégration run() : période = mois précédent + jointures -------------


def test_run_reads_files_and_previous_month(tmp_path):
    outcomes = tmp_path / "outcomes.jsonl"
    runs = tmp_path / "runs.jsonl"
    thresholds = tmp_path / "alert_thresholds.json"
    outcomes.write_text("\n".join(json.dumps(e) for e in _ten_survivors()) + "\n", encoding="utf-8")
    runs.write_text(json.dumps({
        "as_of": IN_JUNE,
        "candidates_detail": [{"ticker": "WIN0", "signal_types": ["residual_z"]}],
    }) + "\n", encoding="utf-8")
    thresholds.write_text(json.dumps({"classifications": {"WIN0": "speculative"}}), encoding="utf-8")

    msg = run(outcomes_path=outcomes, runs_path=runs, thresholds_path=thresholds,
              today=date(2026, 7, 3))  # mois précédent = juin

    assert "2026-06" in msg
    assert "Alertes envoyées : 10" in msg
    assert "residual_z=1" in msg
