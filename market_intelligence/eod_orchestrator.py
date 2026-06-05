from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Final

import pandas as pd

from market_intelligence.anomaly_signals import AnomalySignals, calculate_all
from market_intelligence.beta_gate import calculate_all as calculate_beta_gates
from market_intelligence.candidate_alerts import CandidateAlert
from market_intelligence.candidate_alerts import evaluate_all as evaluate_candidates
from market_intelligence.dedup_hysteresis import DeduplicatedAlert
from market_intelligence.dedup_hysteresis import deduplicate_alerts
from market_intelligence.fetch_eod import fetch_all
from market_intelligence.macro_snapshot import (
    MacroSnapshot,
    MacroSnapshotCache,
    attach_macro_snapshot,
)
from market_intelligence.registry_schema import Registry, load_quarantine, load_registry
from market_intelligence.short_interest import ShortInterestResult, fetch_all_short_interest
from market_intelligence.warren_alert_research import (
    WarrenAlertAnalysis,
    analyze_alerts,
)

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_DAYS: Final[int] = 280

FrameFetcher = Callable[[int], dict[str, pd.DataFrame]]
ShortInterestFetcher = Callable[[Registry], dict[str, ShortInterestResult]]
Deduplicator = Callable[
    [dict[str, CandidateAlert], dict[str, ShortInterestResult]],
    tuple[DeduplicatedAlert, ...],
]
AlertAnalyzer = Callable[[Sequence[object]], tuple[WarrenAlertAnalysis, ...]]
MacroBuilder = Callable[[Mapping[str, pd.DataFrame], Registry | None], MacroSnapshot]


@dataclass(frozen=True)
class EodRunResult:
    """Represent one deployed Sprint 8 anomaly orchestration run."""

    as_of: str | None
    expected_symbols: tuple[str, ...]
    fetched_symbols: tuple[str, ...]
    candidate_count: int
    survivor_count: int
    analysis_count: int
    should_send: bool
    digest: str
    data_issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return an n8n-friendly JSON payload."""
        payload = asdict(self)
        payload["expected_symbols"] = list(self.expected_symbols)
        payload["fetched_symbols"] = list(self.fetched_symbols)
        payload["data_issues"] = list(self.data_issues)
        return payload


def _portfolio_symbols(registry: Registry) -> tuple[str, ...]:
    return tuple(entry.symbol for entry in registry.portfolio_tickers)


def _missing_frame_issues(
    frames: Mapping[str, pd.DataFrame],
    registry: Registry,
) -> tuple[str, ...]:
    quarantined = {entry.symbol for entry in load_quarantine()}
    issues: list[str] = []
    for symbol in _portfolio_symbols(registry):
        frame = frames.get(symbol)
        if symbol in quarantined:
            issues.append(f"ticker_quarantined:{symbol}")
        if frame is None:
            issues.append(f"missing_eod_frame:{symbol}")
        elif frame.empty:
            issues.append(f"empty_eod_frame:{symbol}")
    return tuple(dict.fromkeys(issues))


def _expected_as_of(signals: Mapping[str, AnomalySignals]) -> str | None:
    dates = tuple(
        signal.as_of
        for signal in signals.values()
        if signal.as_of is not None and signal.bar_count > 0
    )
    return max(dates) if dates else None


def _default_deduplicator(
    decisions: dict[str, CandidateAlert],
    short_interest: dict[str, ShortInterestResult],
) -> tuple[DeduplicatedAlert, ...]:
    return deduplicate_alerts(decisions, short_interest)


def _default_analyzer(
    enriched_alerts: Sequence[object],
) -> tuple[WarrenAlertAnalysis, ...]:
    return analyze_alerts(enriched_alerts)


def _build_macro_once(
    cache: MacroSnapshotCache,
    frames: Mapping[str, pd.DataFrame],
    registry: Registry,
    macro_builder: MacroBuilder | None,
) -> None:
    if macro_builder is None:
        cache.get(frames, registry=registry)
        return
    cache.get(frames, registry=registry, builder=macro_builder)


def _attach_cached_macro(
    survivors: Sequence[DeduplicatedAlert],
    frames: Mapping[str, pd.DataFrame],
    cache: MacroSnapshotCache,
    registry: Registry,
    macro_builder: MacroBuilder | None,
) -> tuple[object, ...]:
    if macro_builder is None:
        return attach_macro_snapshot(survivors, frames, cache=cache, registry=registry)
    return attach_macro_snapshot(
        survivors,
        frames,
        cache=cache,
        registry=registry,
        builder=macro_builder,
    )


def format_digest(
    analyses: Sequence[WarrenAlertAnalysis],
    *,
    as_of: str | None,
) -> str:
    """Aggregate all S7 analyses into one Telegram-ready digest."""
    if not analyses:
        return ""
    date_label = as_of or "unknown date"
    lines = [
        f"# EOD anomaly digest - {date_label}",
        "",
        f"Survivors: {len(analyses)}",
        "",
    ]
    for index, analysis in enumerate(analyses, start=1):
        text = analysis.analysis.strip() or "No Warren analysis returned."
        lines.extend((f"## {index}. {analysis.ticker}", text, ""))
    return "\n".join(lines).strip()


def run_eod_anomaly_pipeline(
    *,
    history_days: int = DEFAULT_HISTORY_DAYS,
    registry: Registry | None = None,
    frame_fetcher: FrameFetcher = fetch_all,
    short_interest_fetcher: ShortInterestFetcher = fetch_all_short_interest,
    deduplicator: Deduplicator = _default_deduplicator,
    analyzer: AlertAnalyzer = _default_analyzer,
    macro_builder: MacroBuilder | None = None,
) -> EodRunResult:
    """Run S0-S7 once in the Sprint 8 deployment order."""
    ticker_registry = registry or load_registry()
    expected_symbols = _portfolio_symbols(ticker_registry)
    frames = frame_fetcher(history_days)
    portfolio_frames = {
        symbol: frames[symbol]
        for symbol in expected_symbols
        if symbol in frames
    }

    signals = calculate_all(portfolio_frames)
    gates = calculate_beta_gates(signals, frames)
    as_of = _expected_as_of(signals)
    decisions = evaluate_candidates(
        signals,
        gates,
        expected_as_of=as_of or "",
        expected_symbols=expected_symbols,
    )
    short_interest = short_interest_fetcher(ticker_registry)
    survivors = deduplicator(decisions, short_interest)

    macro_cache = MacroSnapshotCache()
    _build_macro_once(macro_cache, frames, ticker_registry, macro_builder)
    enriched = _attach_cached_macro(
        survivors,
        frames,
        macro_cache,
        ticker_registry,
        macro_builder,
    )
    analyses = analyzer(enriched)
    digest = format_digest(analyses, as_of=as_of)

    issues = list(_missing_frame_issues(frames, ticker_registry))
    for decision in decisions.values():
        issues.extend(decision.data_issues)
    logger.info(
        "EOD anomaly run complete: candidates=%d survivors=%d analyses=%d issues=%d",
        sum(1 for decision in decisions.values() if decision.is_candidate),
        len(survivors),
        len(analyses),
        len(issues),
    )
    return EodRunResult(
        as_of=as_of,
        expected_symbols=expected_symbols,
        fetched_symbols=tuple(sorted(frames)),
        candidate_count=sum(1 for decision in decisions.values() if decision.is_candidate),
        survivor_count=len(survivors),
        analysis_count=len(analyses),
        should_send=bool(digest),
        digest=digest,
        data_issues=tuple(dict.fromkeys(issues)),
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Sprint 8 EOD anomaly pipeline.")
    parser.add_argument("--history-days", type=int, default=DEFAULT_HISTORY_DAYS)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    """Run the CLI and print a JSON payload for n8n."""
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = run_eod_anomaly_pipeline(history_days=args.history_days)
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
