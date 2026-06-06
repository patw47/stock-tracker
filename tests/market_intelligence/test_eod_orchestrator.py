from __future__ import annotations

from collections.abc import Mapping

import pandas as pd

import market_intelligence.eod_orchestrator as orchestrator
from market_intelligence.candidate_alerts import CandidateAlert
from market_intelligence.dedup_hysteresis import DeduplicatedAlert
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
        deduplicator=lambda decisions, short_interest: (),
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
        deduplicator=lambda decisions, short_interest: (
            _survivor("AAA"),
            _survivor("BBB"),
        ),
        macro_builder=macro_builder,
        analyzer=analyzer,
    )

    assert macro_calls == 1
    assert seen_snapshot_ids == [id(snapshot), id(snapshot)]
    assert result.digest.count("## ") == 2


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
        deduplicator=lambda decisions, short_interest: (),
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

    assert digest.startswith("# EOD anomaly digest - 2026-06-02")
    assert "Survivors: 2" in digest
    assert "## 1. AAA" in digest
    assert "## 2. BBB" in digest
