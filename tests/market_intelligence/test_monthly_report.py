"""Epic 5 Sprint 2 — Rapport mensuel de track record.

Couvre les acceptance criteria :
  - rapport depuis un outcomes.jsonl de test → agrégats exacts ;
  - moins de N événements → mention explicite d'échantillon insuffisant ;
  - message conforme au format Telegram sûr (pas de #, échappé) ;
et les correctifs de revue :
  - B1 sélection par measured_at (exactement un rapport par événement) ;
  - B2 cron chaque vendredi + garde 1er vendredi + If wiring ;
  - B3 dédup last-wins par event_id (unavailable→measured non double-compté).
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from market_intelligence import monthly_report as mr
from market_intelligence.monthly_report import build_report, is_first_friday, run

JUNE = (date(2026, 6, 1), date(2026, 6, 30))
IN_JUNE = "2026-06-15"

WORKFLOW_PATH = Path(__file__).parent.parent.parent / "workflow.json"


def _survived(ticker: str, r1: float, r5: float, r20: float, *,
              as_of: str = IN_JUNE, measured_at: str = IN_JUNE) -> dict:
    return {
        "event_id": f"{ticker}:{as_of}", "ticker": ticker, "as_of": as_of,
        "measured_at": measured_at, "status": "measured", "outcome": "survived",
        "ret_1d": r1, "ret_5d": r5, "ret_20d": r20,
    }


def _gated(ticker: str, r20: float, *, as_of: str = IN_JUNE, measured_at: str = IN_JUNE) -> dict:
    return {
        "event_id": f"{ticker}:{as_of}", "ticker": ticker, "as_of": as_of,
        "measured_at": measured_at, "status": "measured",
        "outcome": "gated_dedup:cooldown", "ret_20d": r20,
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
        "measured_at": IN_JUNE, "status": "unavailable", "outcome": "survived",
        "reason": "fetch&<err>",
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


# --- B1 : sélection par measured_at, exactement un rapport ----------------


def test_selects_by_measured_at_exactly_one_report():
    # Alerte du 15/06 mais mesurée le 13/07 (lag S1). Elle ne doit apparaître
    # que dans le rapport couvrant juillet, jamais dans celui de juin.
    event = _survived("LAGGED", 0.02, 0.03, 0.05, as_of="2026-06-15", measured_at="2026-07-13")
    months = [
        (date(2026, 6, 1), date(2026, 6, 30)),
        (date(2026, 7, 1), date(2026, 7, 31)),
        (date(2026, 8, 1), date(2026, 8, 31)),
    ]
    appearances = sum(
        "Alertes envoyées : 1 " in build_report([event], period_start=s, period_end=e)
        for s, e in months
    )
    assert appearances == 1
    june = build_report([event], period_start=months[0][0], period_end=months[0][1])
    july = build_report([event], period_start=months[1][0], period_end=months[1][1])
    assert "Alertes envoyées : 0 " in june
    assert "Alertes envoyées : 1 " in july


# --- B3 : dédup last-wins par event_id ------------------------------------


def test_dedup_last_wins_unavailable_then_measured():
    eid, tk = "AAA:2026-06-15", "AAA"
    unavailable = {"event_id": eid, "ticker": tk, "as_of": IN_JUNE, "measured_at": IN_JUNE,
                   "status": "unavailable", "outcome": "survived", "reason": "no_data"}
    measured = _survived(tk, 0.02, 0.03, 0.05)  # même event_id
    msg = build_report([unavailable, measured], period_start=JUNE[0], period_end=JUNE[1])

    assert "Alertes envoyées : 1 " in msg          # compté comme mesuré
    assert "Data issues chroniques" not in msg     # plus dans les data issues


# --- B2 : garde 1er vendredi + wiring workflow ----------------------------


def test_is_first_friday():
    assert is_first_friday(date(2026, 7, 3)) is True    # 1er vendredi
    assert is_first_friday(date(2026, 8, 7)) is True     # 1er vendredi
    assert is_first_friday(date(2026, 7, 10)) is False   # 2e vendredi
    assert is_first_friday(date(2026, 7, 4)) is False     # samedi


def test_workflow_cron_and_if_guard_wiring():
    workflow = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
    nodes = {n["name"]: n for n in workflow["nodes"]}
    conns = workflow["connections"]

    schedule = nodes["Monthly Report Schedule Vendredi 22h UTC"]
    expr = schedule["parameters"]["rule"]["interval"][0]["expression"]
    assert expr == "0 22 * * 5"  # chaque vendredi, pas de OR dom/dow

    assert "Monthly Report Has Content?" in nodes
    assert nodes["Monthly Report Has Content?"]["type"] == "n8n-nodes-base.if"
    # Prepare → If (garde) → Aggregate : le fallback n'est plus envoyé si vide.
    prep = conns["Prepare Monthly Report for Telegram"]["main"][0]
    assert [e["node"] for e in prep] == ["Monthly Report Has Content?"]
    guard = conns["Monthly Report Has Content?"]["main"][0]
    assert [e["node"] for e in guard] == ["Aggregate for Telegram"]


# --- filtre de période ----------------------------------------------------


def test_period_filter_excludes_other_months():
    events = _ten_survivors() + [
        _survived("OLD", 0.5, 0.5, 0.5, as_of="2026-05-15", measured_at="2026-05-20")
    ]
    msg = build_report(events, period_start=JUNE[0], period_end=JUNE[1])

    assert "Alertes envoyées : 10 " in msg  # l'événement mesuré en mai est exclu


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
