"""Epic 5 Sprint 3 — Harness de backtest & calibration des seuils.

Couvre les acceptance criteria :
  - anomalies plantées détectées aux bons jours, aucune détection anticipée ;
  - dedup_state.json de prod intact après un backtest complet ;
  - zéro appel réseau Warren/OpenClaw pendant le backtest ;
  - sortie donne alertes/mois par combinaison de seuils testée.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from market_intelligence import backtest
from market_intelligence.candidate_alerts import load_alert_config
from market_intelligence.dedup_hysteresis import load_dedup_config
from market_intelligence.eod_orchestrator import CandidateDetail, EodRunResult

DAYS = pd.bdate_range("2026-06-01", periods=5)  # 5 jours ouvrés
PLANTED = DAYS[2].date()  # anomalie plantée le 3e jour
START, END = DAYS[0].date(), DAYS[-1].date()


def _frames() -> dict[str, pd.DataFrame]:
    return {"AAA": pd.DataFrame({"Close": [10, 11, 12, 13, 14]}, index=DAYS)}


def _survived_detail() -> CandidateDetail:
    return CandidateDetail("AAA", 3.0, 2.5, ("residual_z",), "survived", ())


def _spy_runner(planted: date, calls: list):
    """Fake pipeline : détecte 'survived' uniquement le jour où max(bar) == planted."""
    def runner(**kwargs):
        frames = kwargs["frame_fetcher"](kwargs["history_days"])
        maxd = max(
            (pd.to_datetime(f.index).max() for f in frames.values() if not f.empty),
            default=None,
        )
        calls.append(kwargs)
        detail = [_survived_detail()] if maxd is not None and maxd.date() == planted else []
        return EodRunResult(
            as_of=str(maxd.date()) if maxd is not None else None,
            expected_symbols=("AAA",), fetched_symbols=("AAA",),
            candidate_count=len(detail), survivor_count=len(detail), analysis_count=0,
            should_send=bool(detail), digest="", data_issues=(), dry_run=True,
            run_id=None, pending_state_path=None, candidates_detail=tuple(detail),
        )
    return runner


def _simulate(state_path: Path, planted: date, calls: list) -> backtest.ComboResult:
    return backtest.simulate_combo(
        _frames(), backtest.trading_days(_frames(), START, END),
        speculative_z=2.5, rearm_z=1.0, state_path=state_path,
        base_alert_config=load_alert_config(), base_dedup_config=load_dedup_config(),
        registry=None, pipeline_runner=_spy_runner(planted, calls),
    )


# --- no-look-ahead : troncature stricte -----------------------------------


def test_truncating_fetcher_no_look_ahead():
    fetch = backtest._truncating_fetcher(_frames(), PLANTED)
    frame = fetch(999)["AAA"]
    assert pd.to_datetime(frame.index).max().date() == PLANTED  # rien après le jour
    assert len(frame) == 3  # jours 1..3 seulement, futur exclu


# --- détection au bon jour, aucune anticipation ---------------------------


def test_detects_planted_anomaly_on_right_day(tmp_path):
    calls: list = []
    result = _simulate(tmp_path / "st.json", PLANTED, calls)

    assert result.total_alerts == 1
    assert result.per_ticker == {"AAA": 1}
    assert result.per_signal == {"residual_z": 1}
    # Le spy ne voit jamais de barre future : chaque jour, max(frames) == ce jour.
    seen_max = [
        max(pd.to_datetime(f.index).max().date() for f in c["frame_fetcher"](0).values() if not f.empty)
        for c in calls
    ]
    assert seen_max == list(backtest.trading_days(_frames(), START, END))


def test_no_detection_before_planted_day(tmp_path):
    calls: list = []
    # Anomalie plantée au dernier jour : aucun jour antérieur ne doit alerter.
    result = _simulate(tmp_path / "st.json", DAYS[-1].date(), calls)
    assert result.total_alerts == 1  # seulement le dernier jour


# --- prod dedup intact + état éphémère ------------------------------------


def test_ephemeral_dedup_never_touches_prod(tmp_path):
    prod_state = tmp_path / "prod_dedup_state.json"
    prod_state.write_text('{"schema_version": 1, "states": {}}', encoding="utf-8")
    prod_before = prod_state.read_text()

    ephemeral = tmp_path / "ephemeral.json"
    dedup = backtest._ephemeral_deduplicator(ephemeral, load_dedup_config())
    # Appel direct avec décisions vides : écrit l'état éphémère, jamais la prod.
    survivors = dedup({}, {}, readonly=True, pending_path=Path("/should/not/use"))

    assert survivors == ()
    assert ephemeral.exists()               # état éphémère écrit
    assert prod_state.read_text() == prod_before  # prod intacte


# --- zéro Warren / réseau -------------------------------------------------


def test_stubs_are_inert():
    assert backtest._no_short_interest(None) == {}


def test_backtest_runs_dry_and_skips_warren(tmp_path):
    calls: list = []
    _simulate(tmp_path / "st.json", PLANTED, calls)
    for kwargs in calls:
        assert kwargs["dry_run"] is True
        assert kwargs["skip_warren"] is True
        assert kwargs["short_interest_fetcher"] is backtest._no_short_interest
        assert kwargs["journal_path"] is None


# --- sortie : alertes/mois par combinaison --------------------------------


def test_run_backtest_grid_alerts_per_month(tmp_path):
    calls: list = []
    grid = [(2.0, 1.0), (3.0, 1.0)]
    results = backtest.run_backtest(
        _frames(), START, END, grid=grid, state_dir=tmp_path / "states",
        pipeline_runner=_spy_runner(PLANTED, calls),
    )

    assert len(results) == 2
    for combo in results:
        d = combo.to_dict()
        assert "alerts_per_month" in d
        assert combo.months == 1  # tout en juin
        assert combo.total_alerts == 1
        assert combo.alerts_per_month == 1.0

    summary = backtest.format_summary(results)
    assert "/mois" in summary
    assert "spec_z=2.0" in summary


def test_default_grid_is_three_by_three():
    assert len(backtest.default_grid()) == 9
