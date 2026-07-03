from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import replace

import pandas as pd
import pytest

import market_intelligence.eod_orchestrator as orchestrator
from market_intelligence.eod_orchestrator import append_run_log
from market_intelligence.candidate_alerts import CandidateAlert
from market_intelligence.dedup_hysteresis import DeduplicatedAlert, SuppressionDetail
from market_intelligence.macro_snapshot import MacroSnapshot
from market_intelligence.registry_schema import Registry, TickerEntry
from market_intelligence.short_interest import ShortInterestResult
from market_intelligence.warren_alert_research import WarrenAlertAnalysis


def _registry(symbols: tuple[str, ...] = ("AAA", "BBB")) -> Registry:
    return Registry(
        portfolio_tickers=tuple(
            TickerEntry(symbol=symbol, api_symbol=symbol, expected_name=f"{symbol} Corp")
            for symbol in symbols
        ),
        macro_tickers=(
            TickerEntry(symbol="IWM", api_symbol="IWM", expected_name="Russell 2000"),
            TickerEntry(symbol="^VIX", api_symbol="^VIX", expected_name="VIX"),
            TickerEntry(symbol="^TNX", api_symbol="^TNX", expected_name="10Y"),
            TickerEntry(symbol="DXY", api_symbol="DX-Y.NYB", expected_name="Dollar"),
            TickerEntry(symbol="OIL", api_symbol="CL=F", expected_name="Oil"),
        ),
        alias_map={},
    )


def _frame(values: tuple[float, ...] = (100.0, 102.0)) -> pd.DataFrame:
    dates = pd.date_range("2026-06-01", periods=len(values), freq="B")
    return pd.DataFrame(
        {
            "Open": values,
            "High": values,
            "Low": values,
            "Close": values,
            "Volume": 1_000_000,
        },
        index=dates,
    )


def _frames(symbols: tuple[str, ...] = ("AAA", "BBB")) -> dict[str, pd.DataFrame]:
    frames = {symbol: _frame() for symbol in symbols}
    frames.update(
        {
            "IWM": _frame(),
            "^VIX": _frame(),
            "^TNX": _frame((44.0, 45.0)),
            "DXY": _frame(),
            "OIL": _frame(),
        }
    )
    return frames


def _candidate(symbol: str, *, is_candidate: bool = True) -> CandidateAlert:
    return CandidateAlert(
        ticker=symbol,
        as_of="2026-06-02",
        classification="speculative",
        eligible=True,
        is_candidate=is_candidate,
        direction="up" if is_candidate else None,
        signal_types=("residual_z",) if is_candidate else (),
        z_resid=3.0 if is_candidate else 0.2,
        residual_threshold=2.5,
        short_history_fallback_applied=False,
        data_issues=(),
    )


def _survivor(symbol: str) -> DeduplicatedAlert:
    return DeduplicatedAlert(
        candidate=_candidate(symbol),
        squeeze_prone=None,
        fire_reason="initial",
        signal_types=("residual_z",),
    )


def _short(symbol: str) -> ShortInterestResult:
    return ShortInterestResult(
        ticker=symbol,
        api_symbol=symbol,
        short_percent_float=None,
        shares_short=None,
        days_to_cover=None,
        squeeze_prone=None,
        coverage_status="incomplete",
        data_issues=("missing_short_percent_float",),
    )


def _macro() -> MacroSnapshot:
    return MacroSnapshot(
        as_of="2026-06-02",
        ten_year_yield=4.5,
        iwm_close=102.0,
        iwm_pct_change=2.0,
        oil_close=102.0,
        oil_pct_change=2.0,
        vix_close=102.0,
        dxy_close=102.0,
        data_issues=(),
    )


def _minimal_pipeline(monkeypatch, **kwargs):
    """Run the pipeline with lightweight S1-S3 stubs, honoring caller doubles."""
    registry = _registry(("AAA",))
    monkeypatch.setattr(orchestrator, "calculate_all", lambda frames: {})
    monkeypatch.setattr(orchestrator, "calculate_beta_gates", lambda signals, frames: {})
    monkeypatch.setattr(
        orchestrator,
        "evaluate_candidates",
        lambda *args, **kwargs: {"AAA": _candidate("AAA")},
    )
    defaults = dict(
        registry=registry,
        frame_fetcher=lambda days: _frames(("AAA",)),
        short_interest_fetcher=lambda source_registry: {"AAA": _short("AAA")},
        macro_builder=lambda source_frames, source_registry: _macro(),
        analyzer=lambda enriched_alerts: (),
    )
    defaults.update(kwargs)
    return orchestrator.run_eod_anomaly_pipeline(**defaults)


def _run_pipeline_with(monkeypatch, decisions, deduplicator, **kwargs):
    """Run the pipeline with caller-supplied decisions + deduplicator stub."""
    symbols = tuple(decisions)
    registry = _registry(symbols)
    monkeypatch.setattr(orchestrator, "calculate_all", lambda frames: {})
    monkeypatch.setattr(orchestrator, "calculate_beta_gates", lambda signals, frames: {})
    monkeypatch.setattr(
        orchestrator, "evaluate_candidates", lambda *a, **k: decisions
    )
    return orchestrator.run_eod_anomaly_pipeline(
        registry=registry,
        frame_fetcher=lambda days: _frames(symbols),
        short_interest_fetcher=lambda source_registry: {s: _short(s) for s in symbols},
        macro_builder=lambda source_frames, source_registry: _macro(),
        analyzer=lambda enriched_alerts: (),
        deduplicator=deduplicator,
        **kwargs,
    )


def test_candidates_detail_gated_dedup_already_observed_nuai(monkeypatch) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)
    nuai = replace(_candidate("NUAI"), z_resid=-4.18, direction="down")

    def dedup(decisions, short_interest, *, readonly=False, suppressions=None, **kwargs):
        if suppressions is not None:
            suppressions.append(SuppressionDetail("NUAI", -4.18, "already_observed"))
        return ()

    result = _run_pipeline_with(monkeypatch, {"NUAI": nuai}, dedup, dry_run=True)

    detail = {d.ticker: d for d in result.candidates_detail}["NUAI"]
    assert detail.outcome == "gated_dedup:already_observed"
    assert detail.z_resid == pytest.approx(-4.18)


def test_candidates_detail_survived_and_not_candidate(monkeypatch) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)
    decisions = {"AAA": _candidate("AAA"), "BBB": _candidate("BBB", is_candidate=False)}

    result = _run_pipeline_with(
        monkeypatch,
        decisions,
        lambda d, s, **k: (_survivor("AAA"),),
        dry_run=True,
    )

    outcomes = {d.ticker: d.outcome for d in result.candidates_detail}
    assert outcomes["AAA"] == "survived"
    assert outcomes["BBB"] == "not_candidate"


def test_candidates_detail_surfaces_data_issues_when_not_sending(monkeypatch) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)
    decision = replace(_candidate("AAA"), data_issues=("stale_price",))

    result = _run_pipeline_with(
        monkeypatch, {"AAA": decision}, lambda d, s, **k: (), dry_run=True
    )

    assert result.should_send is False
    detail = {d.ticker: d for d in result.candidates_detail}["AAA"]
    assert detail.data_issues == ("stale_price",)


def test_to_dict_serializes_candidates_detail_and_keeps_legacy_keys(monkeypatch) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)

    result = _run_pipeline_with(
        monkeypatch,
        {"AAA": _candidate("AAA")},
        lambda d, s, **k: (_survivor("AAA"),),
        dry_run=True,
    )
    payload = json.loads(json.dumps(result.to_dict()))

    assert payload["candidate_count"] == 1
    assert payload["survivor_count"] == 1
    assert isinstance(payload["candidates_detail"], list)
    assert set(payload["candidates_detail"][0]) == {
        "ticker",
        "z_resid",
        "residual_threshold",
        "signal_types",
        "outcome",
        "data_issues",
    }


def test_append_run_log_writes_single_atomic_line_and_creates_parent(tmp_path) -> None:
    path = tmp_path / "nested" / "runs.jsonl"
    record = {"a": 1, "b": [1, 2], "dry_run": True}

    append_run_log(record, path)

    assert path.parent.exists()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == record

    append_run_log({"c": 2}, path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1]) == {"c": 2}


def test_official_and_dry_run_each_append_one_valid_line(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)
    journal = tmp_path / "runs.jsonl"

    _run_pipeline_with(
        monkeypatch, {"AAA": _candidate("AAA")}, lambda d, s, **k: (), journal_path=journal
    )
    _run_pipeline_with(
        monkeypatch,
        {"AAA": _candidate("AAA")},
        lambda d, s, **k: (),
        journal_path=journal,
        dry_run=True,
    )

    lines = journal.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    official, dry = json.loads(lines[0]), json.loads(lines[1])
    assert official["dry_run"] is False
    assert dry["dry_run"] is True


def test_journal_record_contains_candidates_detail_and_core_fields(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)
    journal = tmp_path / "runs.jsonl"
    decisions = {"AAA": _candidate("AAA"), "BBB": _candidate("BBB", is_candidate=False)}

    _run_pipeline_with(
        monkeypatch,
        decisions,
        lambda d, s, **k: (_survivor("AAA"),),
        journal_path=journal,
        dry_run=True,
    )

    record = json.loads(journal.read_text(encoding="utf-8").splitlines()[0])
    assert set(record) >= {
        "timestamp",
        "as_of",
        "dry_run",
        "candidate_count",
        "survivor_count",
        "candidates_detail",
        "data_issues",
        "should_send",
    }
    assert isinstance(record["candidates_detail"], list) and record["candidates_detail"]


def test_no_journal_path_writes_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)
    orphan = tmp_path / "runs.jsonl"

    _run_pipeline_with(monkeypatch, {"AAA": _candidate("AAA")}, lambda d, s, **k: ())

    assert not orphan.exists()


def test_dry_run_flag_forwards_readonly_and_sets_field(monkeypatch) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)
    recorded: dict[str, bool] = {}

    def rec(decisions, short_interest, *, readonly=False, **_kwargs):
        recorded["readonly"] = readonly
        return (_survivor("AAA"),)

    result = _minimal_pipeline(monkeypatch, deduplicator=rec, dry_run=True)

    assert recorded["readonly"] is True
    assert result.dry_run is True
    assert json.loads(json.dumps(result.to_dict()))["dry_run"] is True


def test_env_var_readonly_matches_dry_run_flag(monkeypatch) -> None:
    monkeypatch.setenv("ANOMALY_DEDUP_READONLY", "1")
    recorded: dict[str, bool] = {}

    def rec(decisions, short_interest, *, readonly=False, **_kwargs):
        recorded["readonly"] = readonly
        return (_survivor("AAA"),)

    # No dry_run flag: the env var alone must flip readonly and the field.
    result = _minimal_pipeline(monkeypatch, deduplicator=rec)

    assert recorded["readonly"] is True
    assert result.dry_run is True


def test_no_flag_no_env_is_not_dry_run(monkeypatch) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)
    recorded: dict[str, bool] = {}

    def rec(decisions, short_interest, *, readonly=False, **_kwargs):
        recorded["readonly"] = readonly
        return ()

    result = _minimal_pipeline(monkeypatch, deduplicator=rec)

    assert recorded["readonly"] is False
    assert result.dry_run is False
    assert json.loads(json.dumps(result.to_dict()))["dry_run"] is False


def test_skip_warren_computes_survivors_without_analyzer(monkeypatch) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)
    analyzer_calls: list[int] = []

    def analyzer(enriched_alerts):
        analyzer_calls.append(1)
        return (WarrenAlertAnalysis("AAA", "prompt", "analysis", None),)  # type: ignore[arg-type]

    result = _minimal_pipeline(
        monkeypatch,
        deduplicator=lambda decisions, short_interest, *, readonly=False, **_kwargs: (
            _survivor("AAA"),
        ),
        analyzer=analyzer,
        skip_warren=True,
        dry_run=True,
    )

    assert result.survivor_count == 1
    assert result.analysis_count == 0
    assert result.should_send is False
    assert result.digest == ""
    assert result.dry_run is True
    assert analyzer_calls == []


def test_skip_warren_without_dry_run_is_rejected(monkeypatch) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)
    fetched: list[int] = []

    def guard_fetcher(days):
        fetched.append(days)
        return _frames(("AAA",))

    with pytest.raises(ValueError, match="skip_warren requires dry-run"):
        _minimal_pipeline(
            monkeypatch,
            frame_fetcher=guard_fetcher,
            skip_warren=True,
        )

    # Fail-fast: the guard must trip before any data fetch runs.
    assert fetched == []


def test_skip_warren_allowed_when_env_readonly(monkeypatch) -> None:
    monkeypatch.setenv("ANOMALY_DEDUP_READONLY", "1")

    result = _minimal_pipeline(
        monkeypatch,
        deduplicator=lambda decisions, short_interest, *, readonly=False, **_kwargs: (
            _survivor("AAA"),
        ),
        skip_warren=True,
    )

    assert result.dry_run is True
    assert result.analysis_count == 0


def test_parse_args_exposes_dry_run_and_skip_warren(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run", "--skip-warren"])
    namespace = orchestrator._parse_args()
    assert namespace.dry_run is True
    assert namespace.skip_warren is True

    monkeypatch.setattr(sys, "argv", ["prog"])
    defaults = orchestrator._parse_args()
    assert defaults.dry_run is False
    assert defaults.skip_warren is False


def test_main_forwards_dry_run_and_skip_warren(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["prog", "--dry-run", "--skip-warren"])
    captured: dict[str, object] = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return orchestrator.EodRunResult(
            as_of=None,
            expected_symbols=(),
            fetched_symbols=(),
            candidate_count=0,
            survivor_count=0,
            analysis_count=0,
            should_send=False,
            digest="",
            data_issues=(),
            dry_run=True,
            run_id=None,
            pending_state_path=None,
        )

    monkeypatch.setattr(orchestrator, "run_eod_anomaly_pipeline", fake_run)
    orchestrator.main()

    assert captured["dry_run"] is True
    assert captured["skip_warren"] is True
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_official_run_stages_pending_with_run_id(monkeypatch) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)
    recorded: dict[str, object] = {}

    def rec(decisions, short_interest, *, readonly=False, **kwargs):
        recorded["readonly"] = readonly
        recorded.update(kwargs)
        return (_survivor("AAA"),)

    result = _minimal_pipeline(monkeypatch, deduplicator=rec)

    assert recorded["readonly"] is False
    # Official run: run_id generated, pending path staged, run_as_of == run as_of.
    assert isinstance(result.run_id, str) and len(result.run_id) == 32
    assert recorded["run_id"] == result.run_id
    assert recorded["pending_path"] is not None
    assert str(recorded["pending_path"]) == result.pending_state_path
    assert recorded["run_as_of"] == result.as_of
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["run_id"] == result.run_id
    assert payload["pending_state_path"] == result.pending_state_path


def test_dry_run_has_no_run_id_or_pending(monkeypatch) -> None:
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)
    recorded: dict[str, object] = {}

    def rec(decisions, short_interest, *, readonly=False, **kwargs):
        recorded["readonly"] = readonly
        recorded.update(kwargs)
        return (_survivor("AAA"),)

    result = _minimal_pipeline(monkeypatch, deduplicator=rec, dry_run=True)

    assert recorded["readonly"] is True
    assert recorded["run_id"] is None
    assert recorded["pending_path"] is None
    assert result.run_id is None
    assert result.pending_state_path is None
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["run_id"] is None
    assert payload["pending_state_path"] is None


def test_pipeline_orders_s0_to_s7_and_calls_warren_only_for_survivors(
    monkeypatch,
) -> None:
    calls: list[str] = []
    registry = _registry()
    decisions = {"AAA": _candidate("AAA"), "BBB": _candidate("BBB", is_candidate=False)}

    def calculate_signals(frames: dict[str, pd.DataFrame]) -> dict:
        calls.append("s1")
        assert set(frames) == {"AAA", "BBB"}
        return {
            symbol: type(
                "Signal",
                (),
                {"as_of": "2026-06-02", "bar_count": 2, "symbol": symbol},
            )()
            for symbol in frames
        }

    def calculate_gates(signals: dict, frames: dict[str, pd.DataFrame]) -> dict:
        calls.append("s2")
        assert set(signals) == {"AAA", "BBB"}
        assert "IWM" in frames
        return {"AAA": object(), "BBB": object()}

    def evaluate(
        signals: dict,
        gates: dict,
        *,
        expected_as_of: str,
        expected_symbols: tuple[str, ...],
    ) -> dict[str, CandidateAlert]:
        calls.append("s3")
        assert expected_as_of == "2026-06-02"
        assert expected_symbols == ("AAA", "BBB")
        assert set(gates) == {"AAA", "BBB"}
        return decisions

    def short_interest(source_registry: Registry) -> dict[str, ShortInterestResult]:
        calls.append("s4")
        assert source_registry is registry
        return {"AAA": _short("AAA"), "BBB": _short("BBB")}

    def dedup(
        source_decisions: dict[str, CandidateAlert],
        source_short: dict[str, ShortInterestResult],
        *,
        readonly: bool = False,
        **_kwargs: object,
    ) -> tuple[DeduplicatedAlert, ...]:
        calls.append("s5")
        assert source_decisions is decisions
        assert set(source_short) == {"AAA", "BBB"}
        return (_survivor("AAA"),)

    def macro_builder(
        frames: Mapping[str, pd.DataFrame],
        source_registry: Registry | None,
    ) -> MacroSnapshot:
        calls.append("s6")
        assert source_registry is registry
        assert "IWM" in frames
        return _macro()

    def analyzer(enriched_alerts: tuple) -> tuple[WarrenAlertAnalysis, ...]:
        calls.append("s7")
        assert len(enriched_alerts) == 1
        assert enriched_alerts[0].alert.candidate.ticker == "AAA"
        return (
            WarrenAlertAnalysis(
                ticker="AAA",
                prompt="prompt",
                analysis="AAA moved on idiosyncratic flow.",
                context=None,  # type: ignore[arg-type]
            ),
        )

    monkeypatch.setattr(orchestrator, "calculate_all", calculate_signals)
    monkeypatch.setattr(orchestrator, "calculate_beta_gates", calculate_gates)
    monkeypatch.setattr(orchestrator, "evaluate_candidates", evaluate)

    result = orchestrator.run_eod_anomaly_pipeline(
        registry=registry,
        frame_fetcher=lambda days: _frames(),
        short_interest_fetcher=short_interest,
        deduplicator=dedup,
        macro_builder=macro_builder,
        analyzer=analyzer,
    )

    assert calls == ["s1", "s2", "s3", "s4", "s5", "s6", "s7"]
    assert result.survivor_count == 1
    assert result.analysis_count == 1
    assert result.should_send is True
    assert "AAA moved on idiosyncratic flow." in result.digest


def test_no_survivor_run_builds_macro_once_but_sends_no_digest(monkeypatch) -> None:
    macro_calls = 0
    analyzer_calls = 0
    registry = _registry(("AAA",))

    monkeypatch.setattr(
        orchestrator,
        "evaluate_candidates",
        lambda *args, **kwargs: {"AAA": _candidate("AAA", is_candidate=False)},
    )

    def macro_builder(
        frames: Mapping[str, pd.DataFrame],
        source_registry: Registry | None,
    ) -> MacroSnapshot:
        nonlocal macro_calls
        macro_calls += 1
        return _macro()

    def analyzer(enriched_alerts: tuple) -> tuple[WarrenAlertAnalysis, ...]:
        nonlocal analyzer_calls
        analyzer_calls += 1
        assert enriched_alerts == ()
        return ()

    result = orchestrator.run_eod_anomaly_pipeline(
        registry=registry,
        frame_fetcher=lambda days: _frames(("AAA",)),
        short_interest_fetcher=lambda source_registry: {"AAA": _short("AAA")},
        deduplicator=lambda decisions, short_interest, *, readonly=False, **_kwargs: (),
        macro_builder=macro_builder,
        analyzer=analyzer,
    )

    assert macro_calls == 1
    assert analyzer_calls == 1
    assert result.survivor_count == 0
    assert result.should_send is False
    assert result.digest == ""


def test_macro_snapshot_is_reused_for_multiple_survivors(monkeypatch) -> None:
    registry = _registry(("AAA", "BBB"))
    snapshot = _macro()
    seen_snapshot_ids: list[int] = []
    macro_calls = 0

    monkeypatch.setattr(
        orchestrator,
        "evaluate_candidates",
        lambda *args, **kwargs: {"AAA": _candidate("AAA"), "BBB": _candidate("BBB")},
    )

    def macro_builder(
        frames: Mapping[str, pd.DataFrame],
        source_registry: Registry | None,
    ) -> MacroSnapshot:
        nonlocal macro_calls
        macro_calls += 1
        return snapshot

    def analyzer(enriched_alerts: tuple) -> tuple[WarrenAlertAnalysis, ...]:
        seen_snapshot_ids.extend(id(alert.macro_snapshot) for alert in enriched_alerts)
        return (
            WarrenAlertAnalysis("AAA", "prompt", "AAA analysis", None),  # type: ignore[arg-type]
            WarrenAlertAnalysis("BBB", "prompt", "BBB analysis", None),  # type: ignore[arg-type]
        )

    result = orchestrator.run_eod_anomaly_pipeline(
        registry=registry,
        frame_fetcher=lambda days: _frames(("AAA", "BBB")),
        short_interest_fetcher=lambda source_registry: {
            "AAA": _short("AAA"),
            "BBB": _short("BBB"),
        },
        deduplicator=lambda decisions, short_interest, *, readonly=False, **_kwargs: (
            _survivor("AAA"),
            _survivor("BBB"),
        ),
        macro_builder=macro_builder,
        analyzer=analyzer,
    )

    assert macro_calls == 1
    assert seen_snapshot_ids == [id(snapshot), id(snapshot)]
    # HTML format: 1 header <b> block + 2 per-ticker <b> blocks.
    assert result.digest.count("</b>") == 3


def test_missing_and_empty_ticker_frames_are_surfaced(monkeypatch) -> None:
    registry = _registry(("GOOD", "EMPTY", "MISSING"))

    monkeypatch.setattr(orchestrator, "calculate_all", lambda frames: {})
    monkeypatch.setattr(orchestrator, "calculate_beta_gates", lambda signals, frames: {})
    monkeypatch.setattr(
        orchestrator,
        "evaluate_candidates",
        lambda *args, **kwargs: {
            "GOOD": _candidate("GOOD", is_candidate=False),
            "EMPTY": _candidate("EMPTY", is_candidate=False),
            "MISSING": _candidate("MISSING", is_candidate=False),
        },
    )

    frames = _frames(("GOOD",))
    frames["EMPTY"] = pd.DataFrame()
    result = orchestrator.run_eod_anomaly_pipeline(
        registry=registry,
        frame_fetcher=lambda days: frames,
        short_interest_fetcher=lambda source_registry: {
            "GOOD": _short("GOOD"),
            "EMPTY": _short("EMPTY"),
            "MISSING": _short("MISSING"),
        },
        deduplicator=lambda decisions, short_interest, *, readonly=False, **_kwargs: (),
        macro_builder=lambda source_frames, source_registry: _macro(),
        analyzer=lambda enriched_alerts: (),
    )

    assert "empty_eod_frame:EMPTY" in result.data_issues
    assert "missing_eod_frame:MISSING" in result.data_issues


def test_format_digest_returns_one_digest_for_all_analyses() -> None:
    digest = orchestrator.format_digest(
        (
            WarrenAlertAnalysis("AAA", "prompt", "AAA analysis", None),  # type: ignore[arg-type]
            WarrenAlertAnalysis("BBB", "prompt", "BBB analysis", None),  # type: ignore[arg-type]
        ),
        as_of="2026-06-02",
    )

    assert "<b>EOD anomaly digest — 2026-06-02</b>" in digest
    assert "Survivors: 2" in digest
    assert "<b>1. AAA</b>" in digest
    assert "<b>2. BBB</b>" in digest
    assert "#" not in digest


def test_format_digest_escapes_html_and_keeps_bold_balanced() -> None:
    analyses = (
        WarrenAlertAnalysis(  # type: ignore[arg-type]
            "A<B", "prompt", "a < b & c > d _under_ *star* `tick`", None
        ),
        WarrenAlertAnalysis("C&D", "prompt", "plain prose", None),  # type: ignore[arg-type]
    )
    digest = orchestrator.format_digest(analyses, as_of="2026-06-02")

    # Special chars from prose/ticker are HTML-escaped.
    assert "&lt;" in digest and "&gt;" in digest and "&amp;" in digest
    assert "A&lt;B" in digest and "C&amp;D" in digest
    # Bold tags balanced; the only raw '<' are the intended <b>/</b> tags.
    assert digest.count("<b>") == digest.count("</b>") > 0
    stripped = digest.replace("<b>", "").replace("</b>", "")
    assert "<" not in stripped and ">" not in stripped


def test_format_digest_has_no_markdown_heading() -> None:
    digest = orchestrator.format_digest(
        (WarrenAlertAnalysis("AAA", "prompt", "AAA analysis", None),),  # type: ignore[arg-type]
        as_of="2026-06-02",
    )
    assert "#" not in digest


def test_split_telegram_html_keeps_bold_balanced_across_chunks() -> None:
    analyses = tuple(
        WarrenAlertAnalysis(f"T{i}", "prompt", "prose " * 20, None)  # type: ignore[arg-type]
        for i in range(60)
    )
    digest = orchestrator.format_digest(analyses, as_of="2026-06-02")
    assert len(digest) > 4000

    chunks = orchestrator.split_telegram_html(digest, limit=4000)

    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 4000
        assert chunk.count("<b>") == chunk.count("</b>")  # no orphan tag


def test_split_telegram_html_degrades_oversized_paragraph_to_plaintext() -> None:
    oversized = "<b>1. AAA</b> " + "x" * 5000  # single paragraph, no \n\n
    chunks = orchestrator.split_telegram_html(oversized, limit=4000)

    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk) <= 4000
        assert "<" not in chunk  # tags stripped -> plain text fallback
