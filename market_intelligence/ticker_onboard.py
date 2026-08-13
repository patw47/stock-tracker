from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from market_intelligence.registry_check import (
    CLASSIFICATIONS_PATH,
    REGISTRY_PATH,
    SECTOR_FACTORS_PATH,
    SINGLE_FACTORS_PATH,
    load_state,
)

logger = logging.getLogger(__name__)

DEFAULT_CLASSIFICATION = "speculative"

ONBOARDED = "onboarded"
ALREADY_PRESENT = "already_present"
INVALID = "invalid"
OFFBOARDED = "offboarded"
NOT_PRESENT = "not_present"


@dataclass(frozen=True)
class OnboardResult:
    """Outcome of onboarding one symbol into the Layer B referentials."""

    symbol: str
    status: str  # ONBOARDED | ALREADY_PRESENT | INVALID
    expected_name: str = ""
    generated: tuple[str, ...] = ()
    manual_actions: tuple[str, ...] = ()
    reason: str = ""


def _validate_symbol(symbol: str):
    """Validate a symbol against yfinance, returning a ValidationResult.

    Uses an empty expected_name so the check only asserts the symbol resolves to a
    real security and captures its actual name. Imported lazily so the module stays
    importable (and unit-testable) without touching the network layer.
    """
    from market_intelligence.registry_schema import TickerEntry
    from market_intelligence.symbol_validator import validate_ticker

    return validate_ticker(TickerEntry(symbol=symbol, api_symbol=symbol, expected_name=""))


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to path atomically, preserving the pretty 2-space layout.

    Creates the parent directory: the state tree does not exist on a machine that
    has never run the bridge (Epic 10 S4), and rebuilding it is exactly what the
    first run is for.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def onboard_ticker(
    symbol: str,
    *,
    registry_path: Path = REGISTRY_PATH,
    classifications_path: Path = CLASSIFICATIONS_PATH,
    single_factors_path: Path = SINGLE_FACTORS_PATH,
    sector_factors_path: Path = SECTOR_FACTORS_PATH,
) -> OnboardResult:
    """Generate the missing Layer B entries for a new symbol, with safe defaults.

    The symbol is validated first; on any validation failure nothing is written.
    For a valid symbol the registry entry (name fetched), a ``speculative``
    classification and a ``single_factor_symbols`` entry are created if missing —
    never a guessed sector-factor mapping. Idempotent: an already-covered symbol
    leaves every file untouched.

    Writes only to state (Epic 10 S4): the registry, the classification table and
    the single-factor list. ``sector_factors_path`` is read to know whether the
    symbol already has an ETF mapping, and is never written — that mapping is
    configuration, chosen in a PR.
    """
    sym = symbol.strip().upper()
    if not sym:
        return OnboardResult(symbol=symbol, status=INVALID, reason="Symbole vide.")

    validation = _validate_symbol(sym)
    if validation.status != "ok":
        return OnboardResult(
            symbol=sym,
            status=INVALID,
            reason=f"Symbole invalide ({validation.status}): {validation.reason}",
        )
    expected_name = validation.actual_name

    registry = load_state(registry_path)
    thresholds = load_state(classifications_path)
    factors = load_state(single_factors_path)
    sector_map = json.loads(sector_factors_path.read_text(encoding="utf-8"))

    registry_symbols = {t["symbol"] for t in registry.get("portfolio_tickers", [])}
    classifications = thresholds.setdefault("classifications", {})
    single_factor = factors.setdefault("single_factor_symbols", [])
    factor_covered = set(sector_map.get("sector_factors", {})) | set(single_factor)

    generated: list[str] = []
    reg_dirty = thr_dirty = fac_dirty = False

    if sym not in registry_symbols:
        registry.setdefault("portfolio_tickers", []).append(
            {"symbol": sym, "api_symbol": sym, "expected_name": expected_name}
        )
        generated.append(f"registry.json → portfolio_tickers (expected_name='{expected_name}')")
        reg_dirty = True

    if sym not in classifications:
        classifications[sym] = DEFAULT_CLASSIFICATION
        generated.append(f"classifications.json → classification '{DEFAULT_CLASSIFICATION}'")
        thr_dirty = True

    if sym not in factor_covered:
        single_factor.append(sym)
        generated.append("single_factor_symbols.json → entrée ajoutée (aucun ETF deviné)")
        fac_dirty = True

    if reg_dirty:
        _atomic_write_json(registry_path, registry)
    if thr_dirty:
        _atomic_write_json(classifications_path, thresholds)
    if fac_dirty:
        _atomic_write_json(single_factors_path, factors)

    if not generated:
        return OnboardResult(
            symbol=sym,
            status=ALREADY_PRESENT,
            expected_name=expected_name,
        )

    manual_actions = (
        f"Choisir un ETF facteur sectoriel pour {sym} dans sector_factors.json en PR "
        "si pertinent (défaut retenu : single_factor).",
    )
    logger.info("Onboarded %s: %s", sym, "; ".join(generated))
    return OnboardResult(
        symbol=sym,
        status=ONBOARDED,
        expected_name=expected_name,
        generated=tuple(generated),
        manual_actions=manual_actions,
    )


def offboard_ticker(
    symbol: str,
    *,
    registry_path: Path = REGISTRY_PATH,
    classifications_path: Path = CLASSIFICATIONS_PATH,
    single_factors_path: Path = SINGLE_FACTORS_PATH,
) -> OnboardResult:
    """Remove a symbol from the Layer B referentials (mirror of onboard_ticker).

    No network validation: the symbol is being retired, not vetted. Removes the
    registry entry, the classification and the single-factor entry. Idempotent: an
    absent symbol touches nothing.

    An ETF sector mapping, if the symbol has one, is deliberately left alone since
    Epic 10 S4: it is configuration, and an automatic path may not rewrite it. A
    mapping without a registry entry is inert — it only says which factor WOULD
    apply — whereas letting offboarding edit the config would make the migration
    invariant red on the first retired ticker.
    """
    sym = symbol.strip().upper()
    if not sym:
        return OnboardResult(symbol=symbol, status=INVALID, reason="Symbole vide.")

    registry = load_state(registry_path)
    thresholds = load_state(classifications_path)
    factors = load_state(single_factors_path)

    generated: list[str] = []

    tickers = registry.get("portfolio_tickers", [])
    kept = [t for t in tickers if t.get("symbol") != sym]
    if len(kept) != len(tickers):
        registry["portfolio_tickers"] = kept
        _atomic_write_json(registry_path, registry)
        generated.append("registry.json → entrée retirée")

    classifications = thresholds.get("classifications", {})
    if sym in classifications:
        del classifications[sym]
        _atomic_write_json(classifications_path, thresholds)
        generated.append("classifications.json → classification retirée")

    single_factor = factors.get("single_factor_symbols", [])
    if sym in single_factor:
        factors["single_factor_symbols"] = [s for s in single_factor if s != sym]
        _atomic_write_json(single_factors_path, factors)
        generated.append("single_factor_symbols.json → entrée retirée")

    if not generated:
        return OnboardResult(symbol=sym, status=NOT_PRESENT)
    logger.info("Offboarded %s: %s", sym, "; ".join(generated))
    return OnboardResult(symbol=sym, status=OFFBOARDED, generated=tuple(generated))


def format_result(result: OnboardResult) -> str:
    """Render an onboarding result as a short Telegram/CLI message."""
    if result.status == INVALID:
        return f"⛔ {result.symbol} : refusé — {result.reason} Aucun fichier modifié."
    if result.status == ALREADY_PRESENT:
        return f"ℹ️ {result.symbol} : déjà cohérent dans les référentiels, rien à générer."
    if result.status == NOT_PRESENT:
        return f"ℹ️ {result.symbol} : absent des référentiels, rien à retirer."
    if result.status == OFFBOARDED:
        lines = [f"🗑 {result.symbol} retiré des référentiels (plus de scan EOD) :"]
        lines += [f"  • {g}" for g in result.generated]
        return "\n".join(lines)
    lines = [f"🆕 {result.symbol} ({result.expected_name}) intégré aux référentiels :"]
    lines += [f"  • {g}" for g in result.generated]
    if result.manual_actions:
        lines.append("À faire manuellement :")
        lines += [f"  – {a}" for a in result.manual_actions]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `python3 -m market_intelligence.ticker_onboard SYMBOL`."""
    parser = argparse.ArgumentParser(
        description="Onboarding assisté d'un nouveau ticker dans les référentiels Layer B "
                    "(registry, classification speculative, single_factor).",
    )
    parser.add_argument("symbol", help="Symbole à intégrer, ex: NVDA")
    args = parser.parse_args(argv)

    result = onboard_ticker(args.symbol)
    print(format_result(result))
    return 1 if result.status == INVALID else 0


if __name__ == "__main__":
    raise SystemExit(main())
