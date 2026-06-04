from __future__ import annotations

import json
import logging
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Final, Literal

import yfinance as yf  # type: ignore[import-untyped]  # yfinance has no type metadata.

from market_intelligence.registry_schema import (
    QuarantineEntry,
    Registry,
    TickerEntry,
    load_quarantine,
    load_registry,
)

logger = logging.getLogger(__name__)

CoverageStatus = Literal["covered", "incomplete", "unsupported", "quarantined", "api_error"]

_CONFIG_PATH: Final[Path] = Path(__file__).parent / "data" / "short_interest_thresholds.json"


class ShortInterestError(Exception):
    """Base error for Sprint 4 short-interest context."""


class ShortInterestConfigError(ShortInterestError):
    """Raised when short-interest threshold configuration is invalid."""


@dataclass(frozen=True)
class ShortInterestConfig:
    """Define deterministic squeeze thresholds and unsupported symbols."""

    short_percent_float: float
    days_to_cover: float
    unsupported_symbols: frozenset[str]


@dataclass(frozen=True)
class ShortInterestResult:
    """Represent Yahoo short-interest context and its squeeze-prone decision."""

    ticker: str
    api_symbol: str
    short_percent_float: float | None
    shares_short: int | None
    days_to_cover: float | None
    squeeze_prone: bool | None
    coverage_status: CoverageStatus
    data_issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return asdict(self)


def _positive_finite(name: str, value: Any, *, maximum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ShortInterestConfigError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise ShortInterestConfigError(f"{name} must be greater than 0")
    if maximum is not None and number > maximum:
        raise ShortInterestConfigError(f"{name} must not exceed {maximum}")
    return number


def load_short_interest_config(path: Path = _CONFIG_PATH) -> ShortInterestConfig:
    """Load and validate Sprint 4 squeeze thresholds and coverage policy."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    thresholds = raw.get("thresholds")
    unsupported = raw.get("unsupported_symbols")
    if not isinstance(thresholds, dict) or not isinstance(unsupported, list):
        raise ShortInterestConfigError(
            "Config requires thresholds and unsupported_symbols"
        )
    if not all(isinstance(symbol, str) and symbol for symbol in unsupported):
        raise ShortInterestConfigError("unsupported_symbols must contain symbols")
    return ShortInterestConfig(
        short_percent_float=_positive_finite(
            "short_percent_float",
            thresholds.get("short_percent_float"),
            maximum=1.0,
        ),
        days_to_cover=_positive_finite(
            "days_to_cover", thresholds.get("days_to_cover")
        ),
        unsupported_symbols=frozenset(unsupported),
    )


def _finite_non_negative(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _shares_short(value: Any) -> int | None:
    number = _finite_non_negative(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _unknown(
    entry: TickerEntry,
    status: CoverageStatus,
    issues: tuple[str, ...],
) -> ShortInterestResult:
    return ShortInterestResult(
        ticker=entry.symbol,
        api_symbol=entry.api_symbol,
        short_percent_float=None,
        shares_short=None,
        days_to_cover=None,
        squeeze_prone=None,
        coverage_status=status,
        data_issues=issues,
    )


def evaluate_squeeze(
    entry: TickerEntry,
    info: dict[str, Any],
    config: ShortInterestConfig | None = None,
) -> ShortInterestResult:
    """Validate Yahoo metadata and calculate the Sprint 4 squeeze-prone flag."""
    squeeze_config = config or load_short_interest_config()
    issues: list[str] = []

    raw_percent = info.get("shortPercentOfFloat")
    short_percent = _finite_non_negative(raw_percent)
    if raw_percent is None:
        issues.append("missing_short_percent_float")
    elif short_percent is None or short_percent > 1:
        short_percent = None
        issues.append("invalid_short_percent_float")

    raw_ratio = info.get("shortRatio")
    days_to_cover = _finite_non_negative(raw_ratio)
    if raw_ratio is None:
        issues.append("missing_days_to_cover")
    elif days_to_cover is None:
        issues.append("invalid_days_to_cover")

    raw_shares = info.get("sharesShort")
    shares_short = _shares_short(raw_shares)
    if raw_shares is None:
        issues.append("missing_shares_short")
    elif shares_short is None:
        issues.append("invalid_shares_short")

    required_complete = short_percent is not None and days_to_cover is not None
    squeeze_prone: bool | None = None
    if short_percent is not None and days_to_cover is not None:
        squeeze_prone = (
            short_percent > squeeze_config.short_percent_float
            and days_to_cover > squeeze_config.days_to_cover
        )
    return ShortInterestResult(
        ticker=entry.symbol,
        api_symbol=entry.api_symbol,
        short_percent_float=short_percent,
        shares_short=shares_short,
        days_to_cover=days_to_cover,
        squeeze_prone=squeeze_prone,
        coverage_status="covered" if required_complete and not issues else "incomplete",
        data_issues=tuple(issues),
    )


def _identity_issue(entry: TickerEntry, info: dict[str, Any]) -> str | None:
    actual_name = info.get("longName") or info.get("shortName")
    if not isinstance(actual_name, str) or not actual_name.strip():
        return "ticker_identity_missing"
    expected = entry.expected_name.lower()
    actual = actual_name.lower()
    if expected not in actual and actual not in expected:
        return "ticker_identity_mismatch"
    return None


def fetch_short_interest(
    entry: TickerEntry,
    config: ShortInterestConfig | None = None,
) -> ShortInterestResult:
    """Fetch and evaluate Yahoo short-interest metadata for one ticker."""
    squeeze_config = config or load_short_interest_config()
    if entry.symbol in squeeze_config.unsupported_symbols:
        return _unknown(entry, "unsupported", ("short_interest_unsupported",))
    try:
        info = yf.Ticker(entry.api_symbol).info
    except Exception as exc:
        logger.warning("Short-interest fetch failed for %s: %s", entry.symbol, exc)
        return _unknown(entry, "api_error", ("short_interest_fetch_failed",))
    if not isinstance(info, dict):
        return _unknown(entry, "incomplete", ("invalid_short_interest_payload",))
    result = evaluate_squeeze(entry, info, squeeze_config)
    identity_issue = _identity_issue(entry, info)
    if identity_issue is None:
        return result
    return replace(
        result,
        squeeze_prone=None,
        coverage_status="incomplete",
        data_issues=result.data_issues + (identity_issue,),
    )


def fetch_all_short_interest(
    registry: Registry | None = None,
    quarantine: list[QuarantineEntry] | None = None,
    config: ShortInterestConfig | None = None,
) -> dict[str, ShortInterestResult]:
    """Return explicit Sprint 4 context for every portfolio ticker."""
    ticker_registry = registry or load_registry()
    quarantine_entries = load_quarantine() if quarantine is None else quarantine
    squeeze_config = config or load_short_interest_config()
    quarantined = {entry.symbol for entry in quarantine_entries}
    results: dict[str, ShortInterestResult] = {}

    for entry in ticker_registry.portfolio_tickers:
        if entry.symbol in quarantined:
            results[entry.symbol] = _unknown(
                entry, "quarantined", ("ticker_quarantined",)
            )
        elif entry.symbol in squeeze_config.unsupported_symbols:
            results[entry.symbol] = _unknown(
                entry, "unsupported", ("short_interest_unsupported",)
            )
        else:
            results[entry.symbol] = fetch_short_interest(entry, squeeze_config)
    return results
