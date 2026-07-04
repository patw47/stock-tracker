"""Epic 5 Sprint 4 — Boucle d'auto-critique Warren.

Couvre les acceptance criteria :
  - 2e alerte sur un ticker → prompt S7 contient l'analyse précédente + outcome réel ;
  - ticker sans historique → section absente, comportement inchangé ;
  - analyses persistées après chaque run avec survivors ;
  - prompt borné (troncature du résumé vérifiée par test).
"""
from __future__ import annotations

import json
from functools import partial
from pathlib import Path

from market_intelligence.candidate_alerts import CandidateAlert
from market_intelligence.dedup_hysteresis import DeduplicatedAlert
from market_intelligence.edgar_form4 import EdgarForm4Result
from market_intelligence.macro_snapshot import MacroEnrichedAlert, MacroSnapshot
from market_intelligence.registry_schema import Registry, TickerEntry
from market_intelligence import warren_alert_research as war
from market_intelligence.warren_alert_research import (
    MarketStructureStatus,
    analyze_alerts,
    build_alert_research_context,
    build_alert_research_prompt,
    load_past_analyses,
)

TICKER = "AAA"


def _registry() -> Registry:
    return Registry(
        portfolio_tickers=(TickerEntry(symbol=TICKER, api_symbol=TICKER, expected_name="A Corp"),),
        macro_tickers=(), alias_map={},
    )


def _enriched(as_of: str) -> MacroEnrichedAlert:
    candidate = CandidateAlert(
        ticker=TICKER, as_of=as_of, classification="speculative", eligible=True,
        is_candidate=True, direction="up", signal_types=("residual_z",),
        z_resid=3.2, residual_threshold=2.5, short_history_fallback_applied=False,
        data_issues=(),
    )
    alert = DeduplicatedAlert(candidate=candidate, squeeze_prone=False,
                             fire_reason="initial", signal_types=("residual_z",))
    macro = MacroSnapshot(as_of=as_of, ten_year_yield=4.5, iwm_close=204.0,
                          iwm_pct_change=2.0, oil_close=78.0, oil_pct_change=4.0,
                          vix_close=19.0, dxy_close=103.5, data_issues=())
    return MacroEnrichedAlert(alert=alert, macro_snapshot=macro)


def _no_market(_entry) -> MarketStructureStatus:
    return MarketStructureStatus(halt_status="unknown", ssr_status="unknown", data_issues=())


def _context(as_of: str, *, analyses_dir: Path, outcomes_path: Path):
    loader = partial(load_past_analyses, analyses_dir=analyses_dir, outcomes_path=outcomes_path)
    return build_alert_research_context(
        _enriched(as_of), registry=_registry(),
        edgar_fetcher=lambda e: EdgarForm4Result(ticker=TICKER, cik=None, filings=(), data_issues=()),
        product_research_fetcher=lambda e: (), sector_research_fetcher=lambda e: (),
        market_status_fetcher=_no_market,
        past_analyses_loader=lambda t, b: loader(t, b),
    )


def _write_log(analyses_dir: Path, records: list[dict]) -> None:
    analyses_dir.mkdir(parents=True, exist_ok=True)
    (analyses_dir / f"{TICKER}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _write_outcomes(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


# --- 2e alerte : analyse précédente + outcome réel ------------------------


def test_second_alert_prompt_has_past_analysis_and_outcome(tmp_path):
    adir, opath = tmp_path / "analyses", tmp_path / "outcomes.jsonl"
    _write_log(adir, [{"as_of": "2026-06-01", "fire_reason": "initial", "z_resid": 2.9,
                       "signal_types": ["residual_z"], "analysis": "hypothèse: squeeze technique"}])
    _write_outcomes(opath, [{"event_id": f"{TICKER}:2026-06-01", "status": "measured",
                             "ret_1d": 0.01, "ret_5d": 0.03, "ret_20d": 0.05}])

    prompt = build_alert_research_prompt(_context("2026-06-15", analyses_dir=adir, outcomes_path=opath))

    assert "ANALYSES PASSÉES + OUTCOMES" in prompt
    assert "2026-06-01" in prompt
    assert "squeeze technique" in prompt
    assert "J+20 +5.0%" in prompt


# --- pas d'historique : section absente, inchangé -------------------------


def test_no_history_section_absent(tmp_path):
    adir, opath = tmp_path / "analyses", tmp_path / "outcomes.jsonl"
    context = _context("2026-06-15", analyses_dir=adir, outcomes_path=opath)
    prompt = build_alert_research_prompt(context)

    assert context.past_analyses == ()
    assert "ANALYSES PASSÉES" not in prompt
    assert "auto-critique" not in prompt


# --- persistance après un run avec survivor -------------------------------


def test_analyses_persisted_after_run(tmp_path):
    adir, opath = tmp_path / "analyses", tmp_path / "outcomes.jsonl"
    analyses = analyze_alerts(
        [_enriched("2026-06-02")], registry=_registry(),
        edgar_fetcher=lambda e: EdgarForm4Result(ticker=TICKER, cik=None, filings=(), data_issues=()),
        product_research_fetcher=lambda e: (), sector_research_fetcher=lambda e: (),
        market_status_fetcher=_no_market,
        warren_client=lambda p: "analyse de test",
        past_analyses_loader=lambda t, b: (),
        analysis_writer=partial(war.persist_analysis, analyses_dir=adir),
    )

    assert len(analyses) == 1
    lines = (adir / f"{TICKER}.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["as_of"] == "2026-06-02"
    assert record["fire_reason"] == "initial"
    assert record["z_resid"] == 3.2
    assert record["analysis"] == "analyse de test"


# --- prompt borné : troncature du résumé ----------------------------------


def test_summary_truncated_to_300(tmp_path):
    adir, opath = tmp_path / "analyses", tmp_path / "outcomes.jsonl"
    _write_log(adir, [{"as_of": "2026-06-01", "fire_reason": "initial", "z_resid": 2.9,
                       "signal_types": ["residual_z"], "analysis": "X" * 500}])

    past = load_past_analyses(TICKER, "2026-06-15", analyses_dir=adir, outcomes_path=opath)

    assert len(past) == 1
    assert len(past[0].summary) == war._SUMMARY_MAX_CHARS  # 300


# --- outcome unavailable : pas de confabulation ---------------------------


def test_unavailable_outcome_no_confabulation(tmp_path):
    adir, opath = tmp_path / "analyses", tmp_path / "outcomes.jsonl"
    _write_log(adir, [{"as_of": "2026-06-01", "fire_reason": "initial", "z_resid": 2.9,
                       "signal_types": ["residual_z"], "analysis": "hyp"}])
    _write_outcomes(opath, [{"event_id": f"{TICKER}:2026-06-01", "status": "unavailable",
                             "reason": "no_data"}])

    prompt = build_alert_research_prompt(_context("2026-06-15", analyses_dir=adir, outcomes_path=opath))
    assert "unavailable" in prompt
    assert "ne rien inférer" in prompt


# --- fail-soft + bornes ---------------------------------------------------


def test_load_fail_soft_when_missing(tmp_path):
    assert load_past_analyses(TICKER, "2026-06-15",
                              analyses_dir=tmp_path / "nope", outcomes_path=tmp_path / "no.jsonl") == ()


def test_non_utf8_analyses_file_fail_soft(tmp_path):
    # Log tronqué mi-séquence UTF-8 (kill/OOM) → () sans lever, section absente.
    adir, opath = tmp_path / "analyses", tmp_path / "outcomes.jsonl"
    adir.mkdir(parents=True)
    (adir / f"{TICKER}.jsonl").write_bytes(b'{"as_of": "2026-06-01", "analysis": "\xff\xfe bad"}\n')

    assert load_past_analyses(TICKER, "2026-06-15", analyses_dir=adir, outcomes_path=opath) == ()

    prompt = build_alert_research_prompt(_context("2026-06-15", analyses_dir=adir, outcomes_path=opath))
    assert "ANALYSES PASSÉES" not in prompt


def test_non_utf8_outcomes_file_fail_soft(tmp_path):
    # outcomes.jsonl corrompu ne doit pas faire crasher la lecture des analyses.
    adir, opath = tmp_path / "analyses", tmp_path / "outcomes.jsonl"
    _write_log(adir, [{"as_of": "2026-06-01", "fire_reason": "initial", "z_resid": 2.9,
                       "signal_types": ["residual_z"], "analysis": "hyp"}])
    opath.write_bytes(b'\xff\xfe not utf8')

    past = load_past_analyses(TICKER, "2026-06-15", analyses_dir=adir, outcomes_path=opath)
    assert len(past) == 1
    assert past[0].outcome is None  # outcomes illisibles → aucun outcome, pas de crash


def test_at_most_two_most_recent(tmp_path):
    adir, opath = tmp_path / "analyses", tmp_path / "outcomes.jsonl"
    _write_log(adir, [
        {"as_of": "2026-06-01", "fire_reason": "initial", "z_resid": 2.0, "analysis": "a1"},
        {"as_of": "2026-06-05", "fire_reason": "initial", "z_resid": 2.1, "analysis": "a2"},
        {"as_of": "2026-06-10", "fire_reason": "initial", "z_resid": 2.2, "analysis": "a3"},
    ])
    past = load_past_analyses(TICKER, "2026-06-20", analyses_dir=adir, outcomes_path=opath)
    assert [p.as_of for p in past] == ["2026-06-05", "2026-06-10"]  # 2 plus récentes


def test_excludes_current_and_future_as_of(tmp_path):
    adir, opath = tmp_path / "analyses", tmp_path / "outcomes.jsonl"
    _write_log(adir, [
        {"as_of": "2026-06-01", "fire_reason": "initial", "z_resid": 2.0, "analysis": "old"},
        {"as_of": "2026-06-15", "fire_reason": "initial", "z_resid": 2.1, "analysis": "current"},
    ])
    past = load_past_analyses(TICKER, "2026-06-15", analyses_dir=adir, outcomes_path=opath)
    assert [p.as_of for p in past] == ["2026-06-01"]  # exclut l'as_of courant
