from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = Path(__file__).parent / "data"
REGISTRY_PATH = DATA_DIR / "registry.json"
ALERT_THRESHOLDS_PATH = DATA_DIR / "alert_thresholds.json"
SECTOR_FACTORS_PATH = DATA_DIR / "sector_factors.json"

BLOCKING = "blocking"
INFO = "info"


@dataclass(frozen=True)
class Issue:
    """A single referential coherence problem, with the file the user must fix."""

    severity: str  # BLOCKING | INFO
    symbol: str
    message: str


def _resolve_runtime_path(name: str) -> Path:
    """Return the runtime list path, falling back to its committed example.

    On the VPS ``portfolio.json`` / ``watchlist.json`` exist (seeded on deploy);
    in the repo only the ``.example.json`` versions are committed, so CI validates
    those.
    """
    runtime = REPO_ROOT / f"{name}.json"
    return runtime if runtime.exists() else REPO_ROOT / f"{name}.example.json"


def load_runtime_symbols(path: Path) -> list[str]:
    """Return the ticker symbols listed in a portfolio/watchlist file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return [t["symbol"] for t in data.get("tickers", [])]


def load_registry_symbols(path: Path = REGISTRY_PATH) -> set[str]:
    """Return the set of portfolio ticker symbols declared in the registry."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {t["symbol"] for t in data.get("portfolio_tickers", [])}


def load_classified_symbols(path: Path = ALERT_THRESHOLDS_PATH) -> set[str]:
    """Return the symbols that have an alert classification."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("classifications", {}))


def load_factor_covered_symbols(path: Path = SECTOR_FACTORS_PATH) -> set[str]:
    """Return symbols covered by a sector-factor mapping or a single-factor entry."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return set(data.get("sector_factors", {})) | set(data.get("single_factor_symbols", []))


def evaluate(
    runtime: dict[str, list[str]],
    registry_symbols: set[str],
    classified_symbols: set[str],
    factor_covered_symbols: set[str],
) -> list[Issue]:
    """Return every coherence issue between runtime lists and the referentials.

    ``runtime`` maps a file label (shown in messages) to its ticker symbols.
    Blocking issues: a runtime ticker missing from the registry, from the alert
    classifications, or from sector-factor coverage. Info issues: a registry entry
    absent from every runtime list (the VPS may run shorter local lists).
    """
    issues: list[Issue] = []
    all_runtime: set[str] = set()

    for label, symbols in runtime.items():
        for sym in symbols:
            all_runtime.add(sym)
            if sym not in registry_symbols:
                issues.append(Issue(
                    BLOCKING, sym,
                    f"{sym}: présent dans {label} mais absent de registry.json "
                    "(portfolio_tickers) — ajouter l'entrée au registre.",
                ))
            if sym not in classified_symbols:
                issues.append(Issue(
                    BLOCKING, sym,
                    f"{sym}: présent dans {label} mais aucune classification dans "
                    "alert_thresholds.json — ajouter 'calm' ou 'speculative'.",
                ))
            if sym not in factor_covered_symbols:
                issues.append(Issue(
                    BLOCKING, sym,
                    f"{sym}: présent dans {label} mais aucun facteur dans "
                    "sector_factors.json — ajouter un mapping ou single_factor_symbols.",
                ))

    for sym in sorted(registry_symbols - all_runtime):
        issues.append(Issue(
            INFO, sym,
            f"{sym}: présent dans registry.json mais absent des listes runtime "
            "(info — listes VPS possiblement plus courtes).",
        ))

    return issues


def run_check(portfolio_path: Path | None = None, watchlist_path: Path | None = None) -> int:
    """Validate runtime lists against the referentials; return an exit code.

    Returns 1 if any blocking incoherence is found, else 0. Prints one line per
    problem naming the file to fix.
    """
    portfolio_path = portfolio_path or _resolve_runtime_path("portfolio")
    watchlist_path = watchlist_path or _resolve_runtime_path("watchlist")

    runtime = {
        portfolio_path.name: load_runtime_symbols(portfolio_path),
        watchlist_path.name: load_runtime_symbols(watchlist_path),
    }
    issues = evaluate(
        runtime,
        load_registry_symbols(),
        load_classified_symbols(),
        load_factor_covered_symbols(),
    )

    blocking = [i for i in issues if i.severity == BLOCKING]
    info = [i for i in issues if i.severity == INFO]

    for issue in blocking:
        print(f"[BLOCKING] {issue.message}")
    for issue in info:
        print(f"[info]     {issue.message}")

    total = sum(len(v) for v in runtime.values())
    if blocking:
        print(f"\nregistry_check: {len(blocking)} incohérence(s) bloquante(s) sur "
              f"{total} tickers runtime — corriger les fichiers ci-dessus.")
        return 1

    print(f"registry_check OK — {total} tickers runtime cohérents "
          f"({len(info)} info).")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `python3 -m market_intelligence.registry_check`."""
    parser = argparse.ArgumentParser(
        description="Valide la cohérence des référentiels tickers "
                    "(portfolio/watchlist ↔ registry, classifications, facteurs).",
    )
    parser.add_argument("--portfolio", type=Path, default=None,
                        help="Chemin du fichier portfolio (défaut: portfolio.json | .example).")
    parser.add_argument("--watchlist", type=Path, default=None,
                        help="Chemin du fichier watchlist (défaut: watchlist.json | .example).")
    args = parser.parse_args(argv)
    return run_check(args.portfolio, args.watchlist)


if __name__ == "__main__":
    raise SystemExit(main())
