from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

import pandas as pd

from market_intelligence.dedup_hysteresis import DeduplicatedAlert
from market_intelligence.registry_schema import Registry, load_registry

logger = logging.getLogger(__name__)

_TEN_YEAR_SYMBOL: Final[str] = "^TNX"
_IWM_SYMBOL: Final[str] = "IWM"
_OIL_SYMBOL: Final[str] = "OIL"
_VIX_SYMBOL: Final[str] = "^VIX"
_DXY_SYMBOL: Final[str] = "DXY"


class MacroSnapshotError(Exception):
    """Base error for Sprint 6 macro snapshot enrichment."""


@dataclass(frozen=True)
class MacroSnapshot:
    """Represent the deterministic Sprint 6 macro regime snapshot."""

    as_of: str | None
    ten_year_yield: float | None
    iwm_close: float | None
    iwm_pct_change: float | None
    oil_close: float | None
    oil_pct_change: float | None
    vix_close: float | None
    dxy_close: float | None
    data_issues: tuple[str, ...]


@dataclass(frozen=True)
class MacroEnrichedAlert:
    """Attach one shared macro snapshot to a deduplicated alert."""

    alert: DeduplicatedAlert
    macro_snapshot: MacroSnapshot


class MacroSnapshotCache:
    """Cache a Sprint 6 macro snapshot for the current anomaly run."""

    def __init__(self) -> None:
        self._snapshot: MacroSnapshot | None = None

    def get(
        self,
        frames: Mapping[str, pd.DataFrame],
        *,
        registry: Registry | None = None,
        builder: Callable[
            [Mapping[str, pd.DataFrame], Registry | None], MacroSnapshot
        ] = lambda source_frames, source_registry: build_macro_snapshot(
            source_frames, registry=source_registry
        ),
    ) -> MacroSnapshot:
        """Return the cached snapshot, building it at most once."""
        if self._snapshot is None:
            self._snapshot = builder(frames, registry)
            logger.info(
                "Built macro snapshot: as_of=%s issues=%d",
                self._snapshot.as_of,
                len(self._snapshot.data_issues),
            )
        return self._snapshot


def _latest_as_of(
    frames: Mapping[str, pd.DataFrame],
    symbols: Sequence[str],
) -> str | None:
    latest: date | None = None
    for symbol in symbols:
        frame = frames.get(symbol)
        if frame is None:
            continue
        if frame.empty:
            continue
        index = frame.index
        if len(index) == 0:
            continue
        value = pd.Timestamp(index.max()).date()
        latest = value if latest is None else max(latest, value)
    return None if latest is None else latest.isoformat()


def _close_values(
    symbol: str,
    frames: Mapping[str, pd.DataFrame],
    issues: list[str],
) -> tuple[float | None, float | None]:
    frame = frames.get(symbol)
    if frame is None:
        issues.append(f"macro_missing:{symbol}")
        return None, None
    if frame.empty:
        issues.append(f"macro_empty:{symbol}")
        return None, None
    if "Close" not in frame.columns:
        issues.append(f"macro_missing_close:{symbol}")
        return None, None

    closes = pd.to_numeric(frame["Close"], errors="coerce").dropna()
    if closes.empty:
        issues.append(f"macro_empty_close:{symbol}")
        return None, None

    latest = float(closes.iloc[-1])
    pct_change: float | None = None
    if len(closes) >= 2 and float(closes.iloc[-2]) != 0:
        pct_change = round((latest / float(closes.iloc[-2]) - 1) * 100, 4)
    else:
        issues.append(f"macro_insufficient_history:{symbol}")
    return latest, pct_change


def _ten_year_yield(raw_close: float | None) -> float | None:
    if raw_close is None:
        return None
    return round(raw_close / 10, 4) if raw_close > 20 else raw_close


def _expected_macro_symbols(registry: Registry) -> tuple[str, ...]:
    return tuple(entry.symbol for entry in registry.macro_tickers)


def build_macro_snapshot(
    frames: Mapping[str, pd.DataFrame],
    *,
    registry: Registry | None = None,
) -> MacroSnapshot:
    """Build one deterministic macro snapshot from already-fetched S0 frames."""
    source_registry = registry or load_registry()
    issues: list[str] = []
    for symbol in _expected_macro_symbols(source_registry):
        if symbol not in frames:
            issues.append(f"macro_missing:{symbol}")

    ten_year_close, _ = _close_values(_TEN_YEAR_SYMBOL, frames, issues)
    iwm_close, iwm_pct_change = _close_values(_IWM_SYMBOL, frames, issues)
    oil_close, oil_pct_change = _close_values(_OIL_SYMBOL, frames, issues)
    vix_close, _ = _close_values(_VIX_SYMBOL, frames, issues)
    dxy_close, _ = _close_values(_DXY_SYMBOL, frames, issues)

    return MacroSnapshot(
        as_of=_latest_as_of(frames, _expected_macro_symbols(source_registry)),
        ten_year_yield=_ten_year_yield(ten_year_close),
        iwm_close=iwm_close,
        iwm_pct_change=iwm_pct_change,
        oil_close=oil_close,
        oil_pct_change=oil_pct_change,
        vix_close=vix_close,
        dxy_close=dxy_close,
        data_issues=tuple(dict.fromkeys(issues)),
    )


def attach_macro_snapshot(
    alerts: Sequence[DeduplicatedAlert],
    frames: Mapping[str, pd.DataFrame],
    *,
    cache: MacroSnapshotCache | None = None,
    registry: Registry | None = None,
    builder: Callable[
        [Mapping[str, pd.DataFrame], Registry | None], MacroSnapshot
    ] = lambda source_frames, source_registry: build_macro_snapshot(
        source_frames, registry=source_registry
    ),
) -> tuple[MacroEnrichedAlert, ...]:
    """Attach one cached macro snapshot to every post-dedup alert."""
    if not alerts:
        return ()
    snapshot_cache = cache or MacroSnapshotCache()
    snapshot = snapshot_cache.get(frames, registry=registry, builder=builder)
    return tuple(MacroEnrichedAlert(alert=alert, macro_snapshot=snapshot) for alert in alerts)
