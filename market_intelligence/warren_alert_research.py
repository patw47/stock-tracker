from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

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
    from warren_server import call_warren

    return call_warren(prompt, "alert")


def _registry_lookup(registry: Registry) -> dict[str, TickerEntry]:
    return {entry.symbol: entry for entry in registry.portfolio_tickers}


def _missing_edgar(ticker: str, issue: str) -> EdgarForm4Result:
    return EdgarForm4Result(ticker=ticker, cik=None, filings=(), data_issues=(issue,))


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
) -> AlertResearchContext:
    """Build structured Sprint 7 context for exactly one post-dedup alert."""
    ticker = enriched_alert.alert.candidate.ticker
    ticker_registry = registry or load_registry()
    entry = _registry_lookup(ticker_registry).get(ticker)
    news_memory = _read_ticker_news_memory(ticker)
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
        "Si un statut ou une donnee est unknown/null, signale l'incertitude.",
        "Reponse attendue: catalyseur probable, preuves, signaux techniques/flux, risques de donnees, conclusion.",
        "",
    ]
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
) -> tuple[WarrenAlertAnalysis, ...]:
    """Call Warren once per post-dedup macro-enriched alert."""
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
        )
        prompt = build_alert_research_prompt(context)
        analyses.append(
            WarrenAlertAnalysis(
                ticker=enriched_alert.alert.candidate.ticker,
                prompt=prompt,
                analysis=warren_client(prompt),
                context=context,
            )
        )
    return tuple(analyses)


def macro_snapshot_ids(
    enriched_alerts: Sequence[MacroEnrichedAlert],
) -> tuple[int, ...]:
    """Expose snapshot object identity for tests and orchestration assertions."""
    return tuple(id(alert.macro_snapshot) for alert in enriched_alerts)
