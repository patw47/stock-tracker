from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent
_REGISTRY_PATH = _REPO_ROOT / "market_intelligence" / "data" / "registry.json"
_QUARANTINE_PATH = _REPO_ROOT / "market_intelligence" / "data" / "quarantine.json"


@dataclass(frozen=True)
class TickerEntry:
    symbol: str
    api_symbol: str
    expected_name: str


@dataclass(frozen=True)
class QuarantineEntry:
    symbol: str
    reason: str
    timestamp: str


@dataclass(frozen=True)
class Registry:
    portfolio_tickers: tuple[TickerEntry, ...]
    macro_tickers: tuple[TickerEntry, ...]
    alias_map: dict[str, str]
    factor_tickers: tuple[TickerEntry, ...] = ()

    def resolve_api_symbol(self, symbol: str) -> str:
        """Return the API symbol to use for a given canonical symbol."""
        return self.alias_map.get(symbol, symbol)

    def all_tickers(self) -> tuple[TickerEntry, ...]:
        """Return portfolio + macro + factor tickers combined.

        Factor tickers are the sector/thematic ETFs used by the beta gate. They must
        be fetched (so their EOD frames are available for factor neutralisation) but
        they are NOT portfolio tickers, so they never generate candidate alerts, and
        NOT macro tickers, so they never pollute the macro snapshot completeness check.
        """
        return self.portfolio_tickers + self.macro_tickers + self.factor_tickers


def _parse_ticker(raw: dict[str, str]) -> TickerEntry:
    return TickerEntry(
        symbol=raw["symbol"],
        api_symbol=raw["api_symbol"],
        expected_name=raw["expected_name"],
    )


def load_registry() -> Registry:
    """Load the canonical ticker registry from market_intelligence/data/registry.json."""
    raw = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    portfolio = tuple(_parse_ticker(t) for t in raw["portfolio_tickers"])
    macro = tuple(_parse_ticker(t) for t in raw["macro_tickers"])
    factor = tuple(_parse_ticker(t) for t in raw.get("factor_tickers", []))
    alias_map: dict[str, str] = raw.get("alias_map", {})
    logger.debug(
        "Registry loaded: %d portfolio, %d macro, %d factor tickers",
        len(portfolio),
        len(macro),
        len(factor),
    )
    return Registry(
        portfolio_tickers=portfolio,
        macro_tickers=macro,
        alias_map=alias_map,
        factor_tickers=factor,
    )


def load_quarantine() -> list[QuarantineEntry]:
    """Load all quarantined ticker entries."""
    raw = json.loads(_QUARANTINE_PATH.read_text(encoding="utf-8"))
    return [
        QuarantineEntry(
            symbol=e["symbol"],
            reason=e["reason"],
            timestamp=e["timestamp"],
        )
        for e in raw["quarantined"]
    ]


def append_quarantine(entry: QuarantineEntry) -> None:
    """Append a new entry to the quarantine file, deduplicating by symbol."""
    raw = json.loads(_QUARANTINE_PATH.read_text(encoding="utf-8"))
    existing = [e for e in raw["quarantined"] if e["symbol"] != entry.symbol]
    existing.append(
        {"symbol": entry.symbol, "reason": entry.reason, "timestamp": entry.timestamp}
    )
    raw["quarantined"] = existing
    _QUARANTINE_PATH.write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info("Quarantine updated: %s (%s)", entry.symbol, entry.reason)
