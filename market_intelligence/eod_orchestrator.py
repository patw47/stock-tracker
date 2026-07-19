from __future__ import annotations

import argparse
import html
import json
import logging
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final, Protocol

import fcntl
import pandas as pd

from market_intelligence.anomaly_signals import AnomalySignals, calculate_all
from market_intelligence.beta_gate import calculate_all as calculate_beta_gates
from market_intelligence.candidate_alerts import AlertThresholdConfig, CandidateAlert
from market_intelligence.candidate_alerts import evaluate_all as evaluate_candidates
from market_intelligence.dedup_hysteresis import DeduplicatedAlert
from market_intelligence.dedup_hysteresis import SuppressionDetail
from market_intelligence.dedup_hysteresis import dedup_readonly_env
from market_intelligence.dedup_hysteresis import deduplicate_alerts
from market_intelligence.dedup_hysteresis import default_pending_path
from market_intelligence.fetch_eod import fetch_all, fetch_symbols
from market_intelligence.macro_snapshot import (
    MacroSnapshot,
    MacroSnapshotCache,
    attach_macro_snapshot,
)
from market_intelligence.registry_schema import Registry, load_quarantine, load_registry
from market_intelligence.short_interest import ShortInterestResult, fetch_all_short_interest
from market_intelligence.tension_signals import (
    append_tension_journal,
    calculate_all as calculate_tension_signals,
    format_tension_digest,
)
from market_intelligence.warren_alert_research import (
    WarrenAlertAnalysis,
    analyze_alerts,
)

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_DAYS: Final[int] = 280
_RUNS_LOG_PATH: Final[Path] = (
    Path(__file__).parent.parent / "runtime" / "market_intelligence" / "runs.jsonl"
)
_TENSION_LOG_PATH: Final[Path] = (
    Path(__file__).parent.parent / "runtime" / "market_intelligence" / "tension.jsonl"
)

FrameFetcher = Callable[[int], dict[str, pd.DataFrame]]
ShortInterestFetcher = Callable[[Registry], dict[str, ShortInterestResult]]


class Deduplicator(Protocol):
    """Filter S3 candidates through S5 hysteresis, optionally read-only."""

    def __call__(
        self,
        decisions: dict[str, CandidateAlert],
        short_interest: dict[str, ShortInterestResult],
        *,
        readonly: bool = False,
        pending_path: Path | None = None,
        run_id: str | None = None,
        run_as_of: str | None = None,
        suppressions: list[SuppressionDetail] | None = None,
    ) -> tuple[DeduplicatedAlert, ...]: ...


AlertAnalyzer = Callable[[Sequence[object]], tuple[WarrenAlertAnalysis, ...]]
MacroBuilder = Callable[[Mapping[str, pd.DataFrame], Registry | None], MacroSnapshot]


@dataclass(frozen=True)
class CandidateDetail:
    """Explain the fate of one evaluated ticker (Epic 2 observability).

    ``outcome`` is ``survived``, ``gated_dedup:<reason>`` or ``not_candidate`` so the
    JSON alone answers "why did I receive nothing?".
    """

    ticker: str
    z_resid: float | None
    residual_threshold: float | None
    signal_types: tuple[str, ...]
    outcome: str
    data_issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "ticker": self.ticker,
            "z_resid": self.z_resid,
            "residual_threshold": self.residual_threshold,
            "signal_types": list(self.signal_types),
            "outcome": self.outcome,
            "data_issues": list(self.data_issues),
        }


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
    dry_run: bool
    run_id: str | None
    pending_state_path: str | None
    candidates_detail: tuple[CandidateDetail, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return an n8n-friendly JSON payload."""
        payload = asdict(self)
        payload["expected_symbols"] = list(self.expected_symbols)
        payload["fetched_symbols"] = list(self.fetched_symbols)
        payload["data_issues"] = list(self.data_issues)
        payload["candidates_detail"] = [
            detail.to_dict() for detail in self.candidates_detail
        ]
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
    *,
    readonly: bool = False,
    pending_path: Path | None = None,
    run_id: str | None = None,
    run_as_of: str | None = None,
    suppressions: list[SuppressionDetail] | None = None,
) -> tuple[DeduplicatedAlert, ...]:
    return deduplicate_alerts(
        decisions,
        short_interest,
        readonly=readonly,
        pending_path=pending_path,
        run_id=run_id,
        run_as_of=run_as_of,
        suppressions=suppressions,
    )


def _build_candidates_detail(
    decisions: Mapping[str, CandidateAlert],
    survivors: Sequence[DeduplicatedAlert],
    suppressions: Sequence[SuppressionDetail],
) -> tuple[CandidateDetail, ...]:
    """Explain each evaluated ticker's fate for the enriched output (Epic 2)."""
    survivor_tickers = {survivor.candidate.ticker for survivor in survivors}
    reason_by_ticker = {item.ticker: item.reason for item in suppressions}
    details: list[CandidateDetail] = []
    for ticker, decision in decisions.items():
        if ticker in survivor_tickers:
            outcome = "survived"
        elif decision.is_candidate:
            reason = reason_by_ticker.get(ticker, "unknown")
            outcome = f"gated_dedup:{reason}"
        else:
            outcome = "not_candidate"
        details.append(
            CandidateDetail(
                ticker=ticker,
                z_resid=decision.z_resid,
                residual_threshold=decision.residual_threshold,
                signal_types=tuple(decision.signal_types),
                outcome=outcome,
                data_issues=tuple(decision.data_issues),
            )
        )
    return tuple(details)


def append_run_log(record: Mapping[str, object], path: Path = _RUNS_LOG_PATH) -> None:
    """Append exactly one JSONL line for this run (atomic under an exclusive flock)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as log_file:
        fcntl.flock(log_file.fileno(), fcntl.LOCK_EX)
        try:
            log_file.write(line)
            log_file.flush()
        finally:
            fcntl.flock(log_file.fileno(), fcntl.LOCK_UN)


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


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html_tags(text: str) -> str:
    return _HTML_TAG_RE.sub("", text)


def _format_warren_digest(
    analyses: Sequence[WarrenAlertAnalysis],
    *,
    as_of: str | None,
) -> str:
    """Legacy Warren-prose digest (kept for the opt-in ``skip_warren=False`` path).

    Superseded by the deterministic :func:`format_digest`; removed with the rest
    of the Warren bridge in Epic 6 Sprint 4. Headings are Telegram-native (émoji +
    ``<b>``) and every value coming from Warren prose / tickers / dates is
    ``html.escape``d so no special character can break the send. Each ``<b>`` tag
    stays within a single line so paragraph-based chunking never orphans a tag.
    """
    if not analyses:
        return ""
    date_label = html.escape(as_of or "unknown date")
    lines = [
        f"📊 <b>EOD anomaly digest — {date_label}</b>",
        "",
        f"Survivors: {len(analyses)}",
        "",
    ]
    for index, analysis in enumerate(analyses, start=1):
        text = analysis.analysis.strip() or "No Warren analysis returned."
        lines.extend(
            (f"<b>{index}. {html.escape(analysis.ticker)}</b>", html.escape(text), "")
        )
    return "\n".join(lines).strip()


# ── Deterministic EOD digest (Epic 6, Sprint 3) ─────────────────────────────
# Zero LLM: survivor prose is canned, keyed by the dedup ``fire_reason``, with
# the anomaly numbers slotted in. Telegram parse_mode=HTML — every dynamic value
# is ``html.escape``d and each ``<b>`` tag stays on one line (shared doctrine).

_DIGEST_SEP: Final[str] = "─" * 28

_DIRECTION_WORD: Final[dict[str, str]] = {"up": "hausse ↑", "down": "baisse ↓"}

_FIRE_LABEL: Final[dict[str, str]] = {
    "initial": "première alerte",
    "escalation": "escalade",
    "new_signal_type": "nouveau signal",
    "direction_reversal": "renversement",
}

_SIGNAL_LABEL: Final[dict[str, str]] = {
    "rvol": "volume relatif",
    "volume_z": "volume anormalement élevé",
    "atr_expansion": "volatilité en expansion (ATR)",
    "breakout_high_52w": "cassure du plus-haut 52 semaines",
    "breakout_low_52w": "cassure du plus-bas 52 semaines",
    "residual_z": "mouvement anormal (z-résiduel)",
    "short_history_return": "fort rendement (historique court)",
}

# Verb phrasing for the "il casse en plus …" escalation clause (matches the epic
# template verbatim; the noun forms above feed the descriptive prose elsewhere).
_BREAKOUT_VERB: Final[dict[str, str]] = {
    "breakout_high_52w": "casse en plus son plus-haut 52 semaines",
    "breakout_low_52w": "casse en plus son plus-bas 52 semaines",
}

_HYSTERESIS_BLOCK: Final[str] = "\n".join(
    (
        "ℹ️ <b>Le filtre d'hystérésis — pourquoi si peu d'alertes ?</b>",
        "",
        "Un ticker n'alerte qu'UNE fois en franchissant son seuil (z-résiduel "
        "~2,5). Il est ensuite « verrouillé » et reste silencieux tant qu'il ne "
        "fait rien de neuf. Il ne réapparaît que dans 4 cas :",
        " • escalade — son z-résiduel s'aggrave d'au moins +1,0",
        " • renversement — la direction s'inverse (hausse ↔ baisse)",
        " • nouveau signal — un type de signal s'ajoute (volume, cassure 52 sem…)",
        " • ré-armement — il retombe au calme (sous 1,0) puis re-franchit le seuil",
    )
)


def _fr(value: float, *, signed: bool = False, decimals: int = 1) -> str:
    """Format a number the French way: comma decimal, U+2212 minus, optional sign."""
    text = f"{value:+.{decimals}f}" if signed else f"{value:.{decimals}f}"
    return text.replace("-", "−").replace(".", ",")


def _volume_clause(signal_types: tuple[str, ...], signal: AnomalySignals | None) -> str:
    if signal is None:
        return ""
    parts: list[str] = []
    if "rvol" in signal_types and signal.rvol is not None:
        parts.append(f"un volume relatif de {_fr(signal.rvol)}× la normale")
    if "volume_z" in signal_types and signal.log_volume_z is not None:
        parts.append(
            f"un volume anormalement élevé (z {_fr(signal.log_volume_z, signed=True)})"
        )
    if not parts:
        return ""
    return "Mouvement porté par " + " et ".join(parts) + "."


def _gap_atr_clause(signal_types: tuple[str, ...], signal: AnomalySignals | None) -> str:
    if signal is None:
        return ""
    parts: list[str] = []
    if signal.opening_gap is not None:
        parts.append(f"Gap d'ouverture {_fr(100 * signal.opening_gap, signed=True)} %")
    if "atr_expansion" in signal_types and signal.atr_expansion_ratio is not None:
        parts.append(f"volatilité en expansion (ATR ×{_fr(signal.atr_expansion_ratio)})")
    for breakout in ("breakout_high_52w", "breakout_low_52w"):
        if breakout in signal_types:
            parts.append(_SIGNAL_LABEL[breakout])
    if not parts:
        return ""
    sentence = ", ".join(parts)
    return sentence[0].upper() + sentence[1:] + "."


def _opening_sentence(ticker: str, alert: DeduplicatedAlert) -> str:
    if alert.fire_reason == "initial":
        return (
            f"Première alerte : {ticker} n'était pas encore « verrouillé » et "
            "vient de franchir son seuil de déclenchement."
        )
    if alert.fire_reason == "new_signal_type":
        added = ", ".join(
            _SIGNAL_LABEL[s] for s in alert.signal_types if s in _SIGNAL_LABEL
        )
        return (
            f"Nouveau signal : {ticker} était déjà verrouillé mais présente un "
            f"type de signal jusqu'ici absent ({added})."
        )
    # direction_reversal
    return (
        f"Renversement : {ticker} était verrouillé dans l'autre sens et bascule "
        "— l'anomalie repart en direction opposée."
    )


def _standard_prose(
    ticker: str, alert: DeduplicatedAlert, signal: AnomalySignals | None
) -> str:
    """Prose for initial / new_signal_type / direction_reversal (signals slotted in)."""
    candidate = alert.candidate
    sentences = [_opening_sentence(ticker, alert)]
    volume = _volume_clause(alert.signal_types, signal)
    if volume:
        sentences.append(volume)
    if candidate.z_resid is not None:
        threshold = (
            f" (seuil {_fr(candidate.residual_threshold)})"
            if candidate.residual_threshold is not None
            else ""
        )
        sentences.append(
            f"Son z-résiduel atteint {_fr(candidate.z_resid, signed=True)}{threshold} "
            "— le titre bouge nettement plus que son comportement habituel."
        )
    tail = _gap_atr_clause(alert.signal_types, signal)
    if tail:
        sentences.append(tail)
    return " ".join(sentences)


def _escalation_prose(ticker: str, alert: DeduplicatedAlert) -> str:
    candidate = alert.candidate
    z_resid = candidate.z_resid
    prev = alert.prev_trigger_z_resid
    z_text = _fr(z_resid, signed=True) if z_resid is not None else "?"
    prev_text = _fr(prev, signed=True) if prev is not None else "?"
    if z_resid is not None and prev is not None:
        margin = (
            f", soit {_fr(abs(z_resid) - abs(prev), signed=True)} au-delà du niveau "
            "qui l'avait fait alerter"
        )
    else:
        margin = ""
    sentences = [
        f"Escalade : {ticker} était déjà verrouillé (il avait déclenché à "
        f"{prev_text}), mais son z-résiduel s'est aggravé jusqu'à {z_text}{margin}."
    ]
    verbs = [
        _BREAKOUT_VERB[b]
        for b in ("breakout_high_52w", "breakout_low_52w")
        if b in alert.signal_types
    ]
    if verbs:
        sentences.append("Il " + " et ".join(verbs) + ".")
    return " ".join(sentences)


def _survivor_block(
    index: int, alert: DeduplicatedAlert, signal: AnomalySignals | None
) -> str:
    candidate = alert.candidate
    ticker = html.escape(candidate.ticker)
    direction = _DIRECTION_WORD.get(candidate.direction or "", "")
    label = _FIRE_LABEL.get(alert.fire_reason, alert.fire_reason)
    header = f"<b>{index}. {ticker} — {direction}   [{label}]</b>"
    if alert.fire_reason == "escalation":
        paragraph = _escalation_prose(ticker, alert)
    else:
        paragraph = _standard_prose(ticker, alert, signal)
    lines = [header, "", paragraph]
    if alert.squeeze_prone is True:
        lines.append("⚠ Profil squeeze possible (short interest élevé).")
    lines.append(f"→ Pour l'analyse Warren : « point sur {ticker} »")
    return "\n".join(lines)


def format_digest(
    survivors: Sequence[DeduplicatedAlert],
    signals: Mapping[str, AnomalySignals],
    *,
    as_of: str | None,
    total_analyzed: int,
    tension_block: str = "",
) -> str:
    """Render survivors + tension into one deterministic Telegram digest (HTML).

    Zero LLM: the per-survivor prose is canned, keyed by the dedup ``fire_reason``,
    with the anomaly numbers slotted in. Sections (fixed header, one block per
    survivor sorted by ``|z_resid|`` descending, the existing tension block, and
    the fixed hysteresis explainer) are joined by a dashed rule, reproducing the
    frozen Epic 6 template. Every value from tickers/dates is ``html.escape``d and
    each ``<b>`` tag stays on one line. Returns ``""`` when there is nothing to send.
    """
    ordered = sorted(
        survivors, key=lambda a: abs(a.candidate.z_resid or 0.0), reverse=True
    )
    sections: list[str] = []
    if ordered:
        count = len(ordered)
        noun = "survivant" if count == 1 else "survivants"
        sections.append(
            "\n".join(
                (
                    f"📊 <b>EOD anomalies — {html.escape(as_of or 'date inconnue')}</b>",
                    f"{count} {noun} sur {total_analyzed} symboles analysés.",
                    "Un « survivant » = un ticker dont l'anomalie est NEUVE aujourd'hui.",
                )
            )
        )
        for index, alert in enumerate(ordered, start=1):
            sections.append(
                _survivor_block(index, alert, signals.get(alert.candidate.ticker))
            )
    if tension_block:
        sections.append(tension_block)
    if ordered:
        sections.append(_HYSTERESIS_BLOCK)
    return f"\n{_DIGEST_SEP}\n".join(sections)


def split_telegram_html(text: str, limit: int = 4000) -> list[str]:
    """Split HTML text into <=limit chunks without orphaning a tag.

    Splits on paragraph (``\\n\\n``) boundaries; since each ``<b>`` tag is
    contained in one paragraph, no chunk cuts a tag. A single oversized paragraph
    (> limit) is degraded to plain text (tags stripped) then hard-sliced.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for paragraph in re.split(r"\n\n+", text):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= limit:
            current = paragraph
        else:
            plain = _strip_html_tags(paragraph)
            for start in range(0, len(plain), limit):
                chunks.append(plain[start : start + limit])
    if current:
        chunks.append(current)
    return chunks


def run_eod_anomaly_pipeline(
    *,
    history_days: int = DEFAULT_HISTORY_DAYS,
    registry: Registry | None = None,
    frame_fetcher: FrameFetcher = fetch_all,
    short_interest_fetcher: ShortInterestFetcher = fetch_all_short_interest,
    deduplicator: Deduplicator = _default_deduplicator,
    analyzer: AlertAnalyzer = _default_analyzer,
    macro_builder: MacroBuilder | None = None,
    dry_run: bool = False,
    skip_warren: bool = True,
    journal_path: Path | None = None,
    alert_config: AlertThresholdConfig | None = None,
    tension_journal_path: Path | None = None,
    watchlist_symbols: Sequence[str] | None = None,
    watchlist_fetcher: Callable[[list[str], int], dict[str, pd.DataFrame]] = fetch_symbols,
) -> EodRunResult:
    """Run S0-S7 once in the Sprint 8 deployment order.

    ``dry_run`` (or the ``ANOMALY_DEDUP_READONLY`` env var) forwards read-only
    mode to S5 so the dedup state file is never mutated. ``skip_warren`` (the
    default since Epic 6 Sprint 3) skips the S6/S7 macro+Warren stage and renders
    the digest deterministically from the survivors' signals + dedup state — zero
    LLM. It no longer requires read-only mode: an official run now commits dedup
    state *and* emits a real (deterministic) digest, so survivors are never
    latched without an alert being sent. Set ``skip_warren=False`` to opt into the
    legacy Warren prose (removed entirely in Sprint 4).
    """
    effective_dry_run = dry_run or dedup_readonly_env()
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
    # Pass alert_config only when overridden (e.g. backtest calibration) so the
    # default production path keeps its original call shape.
    if alert_config is None:
        decisions = evaluate_candidates(
            signals, gates, expected_as_of=as_of or "", expected_symbols=expected_symbols,
        )
    else:
        decisions = evaluate_candidates(
            signals, gates, alert_config,
            expected_as_of=as_of or "", expected_symbols=expected_symbols,
        )
    short_interest = short_interest_fetcher(ticker_registry)
    # Two-phase commit: the official run stages its computed state to a pending
    # file (promoted by dedup_admin after Telegram send). Dry-run stages nothing.
    if effective_dry_run:
        run_id: str | None = None
        pending_path: Path | None = None
    else:
        run_id = uuid.uuid4().hex
        pending_path = default_pending_path()
    suppressions: list[SuppressionDetail] = []
    survivors = deduplicator(
        decisions,
        short_interest,
        readonly=effective_dry_run,
        pending_path=pending_path,
        run_id=run_id,
        run_as_of=as_of,
        suppressions=suppressions,
    )
    candidates_detail = _build_candidates_detail(decisions, survivors, suppressions)

    # Layer C — tension: journal every ticker-day (feeds outcome measurement),
    # render an alert block on new episodes. Deterministic, no LLM, and off the
    # critical path: a failure never blocks the anomaly pipeline. Watchlist
    # tickers (tension tier) are scanned too: OHLCV only, no registry entry,
    # no classification, no beta gate. Computed before the digest so the
    # deterministic renderer can slot the block in its template position.
    tension_block = ""
    if tension_journal_path is not None:
        try:
            tension_frames = dict(portfolio_frames)
            extra = [s for s in (watchlist_symbols or ()) if s not in tension_frames]
            if extra:
                tension_frames.update(watchlist_fetcher(extra, history_days))
            tension = calculate_tension_signals(tension_frames)
            append_tension_journal(
                tension, tension_journal_path, dry_run=effective_dry_run
            )
            tension_block = format_tension_digest(tension, as_of=as_of)
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.error("tension layer failed (non-blocking): %s", exc)

    if skip_warren:
        analyses: tuple[WarrenAlertAnalysis, ...] = ()
        digest = format_digest(
            survivors,
            signals,
            as_of=as_of,
            total_analyzed=len(decisions),
            tension_block=tension_block,
        )
    else:
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
        digest = _format_warren_digest(analyses, as_of=as_of)
        if tension_block:
            digest = f"{digest}\n\n{tension_block}" if digest else tension_block

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
    result = EodRunResult(
        as_of=as_of,
        expected_symbols=expected_symbols,
        fetched_symbols=tuple(sorted(frames)),
        candidate_count=sum(1 for decision in decisions.values() if decision.is_candidate),
        survivor_count=len(survivors),
        analysis_count=len(analyses),
        # A dry-run / read-only run (deploy validation) must never send Telegram,
        # even though the deterministic digest is now non-empty on survivor days.
        # Pre-S3 this held implicitly (skip_warren emitted an empty digest); make
        # it explicit so a deploy on an anomaly day cannot fire a spurious alert.
        should_send=bool(digest) and not effective_dry_run,
        digest=digest,
        data_issues=tuple(dict.fromkeys(issues)),
        dry_run=effective_dry_run,
        run_id=run_id,
        pending_state_path=str(pending_path) if pending_path is not None else None,
        candidates_detail=candidates_detail,
    )
    if journal_path is not None:
        append_run_log(_run_log_record(result), journal_path)
    return result


def _run_log_record(result: EodRunResult) -> dict[str, object]:
    """Project one run into a JSONL journal record (Epic 2)."""
    payload = result.to_dict()
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "as_of": result.as_of,
        "dry_run": result.dry_run,
        "candidate_count": result.candidate_count,
        "survivor_count": result.survivor_count,
        "candidates_detail": payload["candidates_detail"],
        "data_issues": list(result.data_issues),
        "should_send": result.should_send,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Sprint 8 EOD anomaly pipeline.")
    parser.add_argument("--history-days", type=int, default=DEFAULT_HISTORY_DAYS)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run the full pipeline without persisting dedup state.",
    )
    parser.add_argument(
        "--with-warren",
        action="store_true",
        help="Opt into the legacy S6/S7 Warren prose (removed in Sprint 4). The "
        "deterministic, LLM-free digest is the default.",
    )
    # Accepted but now a no-op: skipping Warren is the default. Kept so the
    # deployed n8n command (`--dry-run --skip-warren`) keeps parsing.
    parser.add_argument("--skip-warren", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args()


def _load_watchlist_symbols() -> tuple[str, ...]:
    """Watchlist symbols for the tension tier (VPS watchlist.json, else example).

    Off the critical path: any read/parse failure returns () and the EOD run
    proceeds on the portfolio alone.
    """
    from market_intelligence.registry_check import _resolve_runtime_path, load_runtime_symbols

    try:
        return tuple(load_runtime_symbols(_resolve_runtime_path("watchlist")))
    except Exception as exc:
        logger.error("watchlist load failed (tension tier skipped): %s", exc)
        return ()


def main() -> None:
    """Run the CLI and print a JSON payload for n8n."""
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    result = run_eod_anomaly_pipeline(
        history_days=args.history_days,
        dry_run=args.dry_run,
        skip_warren=not args.with_warren,
        journal_path=_RUNS_LOG_PATH,
        tension_journal_path=_TENSION_LOG_PATH,
        watchlist_symbols=_load_watchlist_symbols(),
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
