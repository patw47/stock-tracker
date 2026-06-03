from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import yfinance as yf

from market_intelligence.registry_schema import (
    QuarantineEntry,
    TickerEntry,
    append_quarantine,
    load_quarantine,
    load_registry,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidationResult:
    symbol: str
    api_symbol: str
    status: str  # "ok" | "not_found" | "name_mismatch" | "api_error"
    actual_name: str
    reason: str


def validate_ticker(entry: TickerEntry) -> ValidationResult:
    """Validate a single ticker against the yfinance API."""
    try:
        info = yf.Ticker(entry.api_symbol).info
        long_name: str = info.get("longName") or ""
        short_name: str = info.get("shortName") or ""
        actual_name = long_name or short_name
        if not actual_name:
            return ValidationResult(
                symbol=entry.symbol,
                api_symbol=entry.api_symbol,
                status="not_found",
                actual_name="",
                reason="No longName or shortName in yfinance response",
            )
        expected_lower = entry.expected_name.lower()
        actual_lower = actual_name.lower()
        if expected_lower not in actual_lower and actual_lower not in expected_lower:
            return ValidationResult(
                symbol=entry.symbol,
                api_symbol=entry.api_symbol,
                status="name_mismatch",
                actual_name=actual_name,
                reason=f"Expected '{entry.expected_name}', got '{actual_name}'",
            )
        return ValidationResult(
            symbol=entry.symbol,
            api_symbol=entry.api_symbol,
            status="ok",
            actual_name=actual_name,
            reason="",
        )
    except Exception as exc:
        return ValidationResult(
            symbol=entry.symbol,
            api_symbol=entry.api_symbol,
            status="api_error",
            actual_name="",
            reason=str(exc)[:200],
        )


def run_validation() -> list[ValidationResult]:
    """Load registry, validate all portfolio + macro tickers, quarantine non-ok results."""
    registry = load_registry()
    existing_quarantine = {e.symbol for e in load_quarantine()}
    tickers: list[TickerEntry] = list(registry.portfolio_tickers) + list(registry.macro_tickers)

    results: list[ValidationResult] = []
    for entry in tickers:
        result = validate_ticker(entry)
        results.append(result)
        logger.info("Validated %s (%s): %s", result.symbol, result.api_symbol, result.status)
        if result.status != "ok" and result.symbol not in existing_quarantine:
            append_quarantine(
                QuarantineEntry(
                    symbol=result.symbol,
                    reason=result.reason,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
            )
            existing_quarantine.add(result.symbol)

    col_w = (8, 12, 14, 40, 60)
    header = (
        f"{'Symbol':<{col_w[0]}} {'API Symbol':<{col_w[1]}} "
        f"{'Status':<{col_w[2]}} {'Actual Name':<{col_w[3]}} Reason"
    )
    print(f"\n{header}")
    print("-" * sum(col_w))
    for r in results:
        print(
            f"{r.symbol:<{col_w[0]}} {r.api_symbol:<{col_w[1]}} "
            f"{r.status:<{col_w[2]}} {r.actual_name:<{col_w[3]}} {r.reason[:col_w[4]]}"
        )

    ok_count = sum(1 for r in results if r.status == "ok")
    print(f"\n{ok_count}/{len(results)} tickers validated OK")

    return results


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    run_validation()
