from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

_RUNTIME_DIR: Final[Path] = (
    Path(__file__).parent.parent / "runtime" / "market_intelligence"
)
_ANALYSES_DIR: Final[Path] = _RUNTIME_DIR / "analyses"
_OUTCOMES_PATH: Final[Path] = _RUNTIME_DIR / "outcomes.jsonl"

MAX_PAST_ANALYSES: Final[int] = 2
_SUMMARY_MAX_CHARS: Final[int] = 300

from market_intelligence.edgar_form4 import (
    EdgarForm4Result,
    fetch_company_form4_filings,
)
from market_intelligence.macro_snapshot import MacroEnrichedAlert, MacroSnapshot
from market_intelligence.market_status import (
    MarketStatus,
    MarketStructureStatus,
    fetch_market_status,
)
from market_intelligence.registry_schema import Registry, TickerEntry, load_registry

WarrenClient = Callable[[str], str]
EdgarFetcher = Callable[[TickerEntry], EdgarForm4Result]
ResearchFetcher = Callable[[TickerEntry], tuple["ResearchItem", ...]]

_NO_CATALYST_RULE: Final[str] = (
    "Tu peux conclure explicitement: "
    "aucun catalyseur identifiable - flux/technique/squeeze probable."
)

# Le digest échappe la prose Warren (html.escape) : tout Markdown ou HTML
# s'affiche tel quel dans Telegram — d'où les règles texte brut ci-dessous.
_OUTPUT_FORMAT_RULES: Final[tuple[str, ...]] = (
    "Format de sortie (texte brut Telegram, AUCUN rendu riche) :",
    "- INTERDIT : Markdown (###, **, *, tableaux |...|) et balises HTML — "
    "ils s'affichent tels quels chez le lecteur.",
    "- Structure : titres courts en MAJUSCULES précédés d'un emoji, puis puces "
    "« • ». Une ligne vide entre sections. Jamais de tableau.",
    "- Phrases courtes ; pas de note de style « rapport », c'est un message.",
    "Pédagogie : à chaque terme technique cité (residual z, ATR expansion, "
    "squeeze-prone, rvol, beta gate...), garde le jargon PUIS ajoute sa "
    "traduction en français courant en une phrase — ex. « ATR expansion "
    "(la fourchette de variation quotidienne s'élargit : le titre bouge "
    "plus fort que d'habitude) ».",
)

_WARREN_MEMORY_DIR_DEFAULT: Final[str] = (
    "/home/warren/.openclaw/workspace-warren/memory/tickers"
)


def _read_ticker_news_memory(ticker: str) -> str | None:
    """Read Layer A news memory for ticker; return raw content or None if absent."""
    memory_dir = os.environ.get("WARREN_MEMORY_DIR", _WARREN_MEMORY_DIR_DEFAULT)
    path = Path(memory_dir) / f"{ticker}.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


class WarrenAlertResearchError(Exception):
    """Base error for Sprint 7 targeted Warren alert research."""


@dataclass(frozen=True)
class ResearchItem:
    """Represent one product or sector research snippet."""

    source: str
    title: str
    url: str | None
    summary: str

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return asdict(self)


@dataclass(frozen=True)
class PastAnalysis:
    """One prior Warren analysis of a ticker, paired with its measured outcome.

    ``outcome`` holds the S1 returns ({"ret_1d","ret_5d","ret_20d"}) when measured,
    {"status": "unavailable"} when the outcome could not be measured, or None when
    no outcome row exists yet — the prompt must never confabulate in that case.
    """

    as_of: str
    fire_reason: str
    z_resid: float | None
    summary: str
    outcome: dict[str, object] | None


@dataclass(frozen=True)
class AlertResearchContext:
    """Collect all structured context passed to Warren for one deduped alert."""

    enriched_alert: MacroEnrichedAlert
    ticker_entry: TickerEntry | None
    edgar_form4: EdgarForm4Result
    product_research: tuple[ResearchItem, ...]
    sector_research: tuple[ResearchItem, ...]
    market_status: MarketStructureStatus
    data_issues: tuple[str, ...]
    ticker_news_memory: str | None = None
    past_analyses: tuple[PastAnalysis, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        candidate = self.enriched_alert.alert.candidate
        return {
            "alert": {
                "ticker": candidate.ticker,
                "as_of": candidate.as_of,
                "classification": candidate.classification,
                "direction": candidate.direction,
                "fire_reason": self.enriched_alert.alert.fire_reason,
                "signal_types": list(self.enriched_alert.alert.signal_types),
                "z_resid": candidate.z_resid,
                "residual_threshold": candidate.residual_threshold,
                "short_history_fallback_applied": (
                    candidate.short_history_fallback_applied
                ),
                "candidate_data_issues": list(candidate.data_issues),
            },
            "ticker_identity": (
                None
                if self.ticker_entry is None
                else {
                    "symbol": self.ticker_entry.symbol,
                    "api_symbol": self.ticker_entry.api_symbol,
                    "expected_name": self.ticker_entry.expected_name,
                }
            ),
            "squeeze": {
                "squeeze_prone": self.enriched_alert.alert.squeeze_prone,
            },
            "edgar_form4": self.edgar_form4.to_dict(),
            "market_status": self.market_status.to_dict(),
            "product_research": [item.to_dict() for item in self.product_research],
            "sector_research": [item.to_dict() for item in self.sector_research],
            "macro_snapshot": asdict(self.enriched_alert.macro_snapshot),
            "data_issues": list(self.data_issues),
            "ticker_news_memory": self.ticker_news_memory,
        }


@dataclass(frozen=True)
class WarrenAlertAnalysis:
    """Represent Warren's targeted explanation for one deduped alert."""

    ticker: str
    prompt: str
    analysis: str
    context: AlertResearchContext


def _empty_research(_: TickerEntry) -> tuple[ResearchItem, ...]:
    return ()


def _default_product_research(entry: TickerEntry) -> tuple[ResearchItem, ...]:
    # Lazy import to avoid circular dependency (web_research imports ResearchItem from here).
    from market_intelligence.web_research import fetch_ticker_news

    return fetch_ticker_news(entry)


def _default_sector_research(entry: TickerEntry) -> tuple[ResearchItem, ...]:
    # Lazy import to avoid circular dependency (web_research imports ResearchItem from here).
    from market_intelligence.web_research import fetch_sector_news_for_entry

    return fetch_sector_news_for_entry(entry)


def _default_warren_client(prompt: str) -> str:
    from warren_server import call_warren, extract_inner

    # call_warren returns the raw OpenClaw --json envelope (tool schemas,
    # finalPromptText, ...); only the extracted assistant text may reach the
    # digest / persisted analyses.
    return extract_inner(call_warren(prompt, "alert"))


def _registry_lookup(registry: Registry) -> dict[str, TickerEntry]:
    return {entry.symbol: entry for entry in registry.portfolio_tickers}


def _missing_edgar(ticker: str, issue: str) -> EdgarForm4Result:
    return EdgarForm4Result(ticker=ticker, cik=None, filings=(), data_issues=(issue,))


def _analyses_path(ticker: str, *, analyses_dir: Path = _ANALYSES_DIR) -> Path:
    return analyses_dir / f"{ticker}.jsonl"


def persist_analysis(analysis: WarrenAlertAnalysis, *, analyses_dir: Path = _ANALYSES_DIR) -> None:
    """Append one analysis record to the ticker's JSONL log (fail-soft).

    Warren stays off the critical path: a write failure is logged and swallowed,
    never propagated to the alert pipeline.
    """
    candidate = analysis.context.enriched_alert.alert.candidate
    record = {
        "as_of": candidate.as_of,
        "fire_reason": analysis.context.enriched_alert.alert.fire_reason,
        "z_resid": candidate.z_resid,
        "signal_types": list(analysis.context.enriched_alert.alert.signal_types),
        "analysis": analysis.analysis,
    }
    try:
        analyses_dir.mkdir(parents=True, exist_ok=True)
        path = _analyses_path(analysis.ticker, analyses_dir=analyses_dir)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        logger.warning("Could not persist analysis for %s: %s", analysis.ticker, exc)


def _load_outcomes_index(path: Path = _OUTCOMES_PATH) -> dict[str, dict]:
    """Map event_id → last outcome record from outcomes.jsonl (last-wins)."""
    index: dict[str, dict] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # A truncated/corrupt (e.g. non-UTF-8) file must never crash the alert
        # pipeline: Warren stays off the critical path.
        return index
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            index[record["event_id"]] = record
        except (json.JSONDecodeError, KeyError):
            continue
    return index


def _outcome_for(ticker: str, as_of: str | None, outcomes: dict[str, dict]) -> dict[str, object] | None:
    """Return the returns / unavailable marker / None for a past analysis."""
    if as_of is None:
        return None
    record = outcomes.get(f"{ticker}:{as_of}")
    if record is None:
        return None
    if record.get("status") == "measured":
        return {k: record.get(k) for k in ("ret_1d", "ret_5d", "ret_20d")}
    return {"status": record.get("status", "unavailable")}


def load_past_analyses(
    ticker: str,
    before_as_of: str | None,
    *,
    analyses_dir: Path = _ANALYSES_DIR,
    outcomes_path: Path = _OUTCOMES_PATH,
) -> tuple[PastAnalysis, ...]:
    """Return up to MAX_PAST_ANALYSES prior analyses of ticker, with outcomes.

    Fail-soft: any read/parse error yields an empty tuple so the alert prompt
    simply omits the self-critique section (Warren off the critical path).
    """
    path = _analyses_path(ticker, analyses_dir=analyses_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # Corrupt/non-UTF-8 log (e.g. append truncated mid-UTF-8 by a kill/OOM):
        # degrade to no self-critique section, never raise into the pipeline.
        return ()

    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    outcomes = _load_outcomes_index(outcomes_path)
    past: list[PastAnalysis] = []
    for record in records:
        as_of = record.get("as_of")
        if before_as_of is not None and isinstance(as_of, str) and as_of >= before_as_of:
            continue  # skip the current (or future) run's own analysis
        summary = str(record.get("analysis", "")).strip()[:_SUMMARY_MAX_CHARS]
        past.append(PastAnalysis(
            as_of=str(as_of),
            fire_reason=str(record.get("fire_reason", "")),
            z_resid=record.get("z_resid"),
            summary=summary,
            outcome=_outcome_for(ticker, as_of if isinstance(as_of, str) else None, outcomes),
        ))
    return tuple(past[-MAX_PAST_ANALYSES:])


def build_alert_research_context(
    enriched_alert: MacroEnrichedAlert,
    *,
    registry: Registry | None = None,
    edgar_fetcher: EdgarFetcher | None = None,
    product_research_fetcher: ResearchFetcher = _empty_research,
    sector_research_fetcher: ResearchFetcher = _empty_research,
    market_status_fetcher: Callable[[TickerEntry], MarketStructureStatus] = (
        fetch_market_status
    ),
    past_analyses_loader: Callable[[str, str | None], tuple[PastAnalysis, ...]] = (
        load_past_analyses
    ),
) -> AlertResearchContext:
    """Build structured Sprint 7 context for exactly one post-dedup alert."""
    candidate = enriched_alert.alert.candidate
    ticker = candidate.ticker
    ticker_registry = registry or load_registry()
    entry = _registry_lookup(ticker_registry).get(ticker)
    news_memory = _read_ticker_news_memory(ticker)
    past_analyses = past_analyses_loader(ticker, candidate.as_of)
    if entry is None:
        edgar = _missing_edgar(ticker, "ticker_registry_missing")
        return AlertResearchContext(
            enriched_alert=enriched_alert,
            ticker_entry=None,
            edgar_form4=edgar,
            product_research=(),
            sector_research=(),
            market_status=MarketStructureStatus(
                halt_status="unknown",
                ssr_status="unknown",
                data_issues=("ticker_registry_missing",),
            ),
            data_issues=("ticker_registry_missing",),
            ticker_news_memory=news_memory,
            past_analyses=past_analyses,
        )

    fetch_edgar = edgar_fetcher or fetch_company_form4_filings
    edgar = fetch_edgar(entry)
    product_research = product_research_fetcher(entry)
    sector_research = sector_research_fetcher(entry)
    market_status = market_status_fetcher(entry)
    issues = tuple(
        dict.fromkeys(
            edgar.data_issues
            + market_status.data_issues
            + tuple(enriched_alert.alert.candidate.data_issues)
        )
    )
    return AlertResearchContext(
        enriched_alert=enriched_alert,
        ticker_entry=entry,
        edgar_form4=edgar,
        product_research=product_research,
        sector_research=sector_research,
        market_status=market_status,
        data_issues=issues,
        ticker_news_memory=news_memory,
        past_analyses=past_analyses,
    )


def build_alert_research_prompt(context: AlertResearchContext) -> str:
    """Render the targeted Warren prompt for one anomaly alert."""
    payload = json.dumps(context.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    parts: list[str] = [
        "[ANOMALY-ALERT-RESEARCH S7]",
        "Objectif: expliquer le pourquoi plausible de cette alerte EOD sans confabuler.",
        "Utilise uniquement le contexte structure ci-dessous et la recherche produit/secteur fournie.",
        "Les Form 4 EDGAR sont des donnees structurees, pas une recherche web.",
        _NO_CATALYST_RULE,
        "Ne mentionne JAMAIS une donnée absente, unknown ou null : son absence "
        "suffit à signaler le manque, l'énumérer ne fait que polluer. Pas de "
        "section « risques de données » sans risque concret à signaler.",
        "Section catalyseur fondamental : si aucune actualité, recherche ou Form 4 "
        "exploitable, écris UNE seule ligne « Aucun catalyseur fondamental "
        "identifiable. » — n'énumère pas les sources vides une par une.",
        "Contexte macro : ne répète pas le brief macro du jour, il est déjà connu. "
        "Donne seulement sa conclusion pour CE titre en une phrase — ex. « Le "
        "contexte macro n'explique pas la baisse atypique sur ANAB : le mouvement "
        "est isolé. »",
        "Reponse attendue: catalyseur probable, preuves, signaux techniques/flux, conclusion.",
        *_OUTPUT_FORMAT_RULES,
        "",
    ]
    if context.past_analyses:
        parts += _render_self_critique(context.past_analyses)
    if context.ticker_news_memory is not None:
        parts += [
            "=== MÉMOIRE NEWS LAYER A (dernières sessions) ===",
            context.ticker_news_memory,
            "",
        ]
    parts += [
        "=== CONTEXTE STRUCTURE ===",
        payload,
    ]
    return "\n".join(parts)


def _format_outcome(outcome: dict[str, object] | None) -> str:
    """Render a past analysis outcome without confabulating on missing data."""
    if outcome is None:
        return "outcome réel: non encore mesuré (ne rien inférer)."
    if "status" in outcome:
        return f"outcome réel: {outcome['status']} (ne rien inférer)."
    def _pct(key: str) -> str:
        value = outcome.get(key)
        return f"{float(value):+.1%}" if isinstance(value, (int, float)) else "?"
    return f"outcome réel: J+1 {_pct('ret_1d')} · J+5 {_pct('ret_5d')} · J+20 {_pct('ret_20d')}"


def _render_self_critique(past_analyses: tuple[PastAnalysis, ...]) -> list[str]:
    """Render the ANALYSES PASSÉES + OUTCOMES self-critique section."""
    lines = [
        "=== ANALYSES PASSÉES + OUTCOMES (auto-critique) ===",
        "Confronte tes hypothèses passées à ce qui s'est réellement passé, ajuste ta "
        "confiance. Ne confabule jamais quand l'outcome est unavailable ou non mesuré.",
    ]
    for past in past_analyses:
        z = f"{past.z_resid:+.2f}" if isinstance(past.z_resid, (int, float)) else "?"
        lines.append(
            f"[{past.as_of}] fire={past.fire_reason} z={z} → hypothèse: {past.summary}"
        )
        lines.append(f"    {_format_outcome(past.outcome)}")
    lines.append("")
    return lines


def analyze_alerts(
    enriched_alerts: Sequence[MacroEnrichedAlert],
    *,
    registry: Registry | None = None,
    edgar_fetcher: EdgarFetcher | None = None,
    product_research_fetcher: ResearchFetcher = _default_product_research,
    sector_research_fetcher: ResearchFetcher = _default_sector_research,
    market_status_fetcher: Callable[[TickerEntry], MarketStructureStatus] = (
        fetch_market_status
    ),
    warren_client: WarrenClient = _default_warren_client,
    past_analyses_loader: Callable[[str, str | None], tuple[PastAnalysis, ...]] = (
        load_past_analyses
    ),
    analysis_writer: Callable[[WarrenAlertAnalysis], None] = persist_analysis,
) -> tuple[WarrenAlertAnalysis, ...]:
    """Call Warren once per post-dedup macro-enriched alert.

    Each analysis is persisted (fail-soft) so future alerts on the same ticker can
    feed Warren its prior hypotheses and their measured outcomes (self-critique).
    """
    if not enriched_alerts:
        return ()
    analyses: list[WarrenAlertAnalysis] = []
    for enriched_alert in enriched_alerts:
        context = build_alert_research_context(
            enriched_alert,
            registry=registry,
            edgar_fetcher=edgar_fetcher,
            product_research_fetcher=product_research_fetcher,
            sector_research_fetcher=sector_research_fetcher,
            market_status_fetcher=market_status_fetcher,
            past_analyses_loader=past_analyses_loader,
        )
        prompt = build_alert_research_prompt(context)
        analysis = WarrenAlertAnalysis(
            ticker=enriched_alert.alert.candidate.ticker,
            prompt=prompt,
            analysis=warren_client(prompt),
            context=context,
        )
        analysis_writer(analysis)
        analyses.append(analysis)
    return tuple(analyses)


def macro_snapshot_ids(
    enriched_alerts: Sequence[MacroEnrichedAlert],
) -> tuple[int, ...]:
    """Expose snapshot object identity for tests and orchestration assertions."""
    return tuple(id(alert.macro_snapshot) for alert in enriched_alerts)
