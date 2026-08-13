from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
DATA_DIR = Path(__file__).parent / "data"

# Configuration — decided, reviewed in a PR, versioned. A deploy `git reset --hard`
# is supposed to restore exactly these.
ALERT_THRESHOLDS_PATH = DATA_DIR / "alert_thresholds.json"
SECTOR_FACTORS_PATH = DATA_DIR / "sector_factors.json"
# The registry is split the same way, along the same line: the macro tickers, the
# beta-gate factor ETFs and the API alias map are structural decisions — and the
# bridge never rebuilds them, so a fresh machine would lose them for good if they
# left git with the rest.
REGISTRY_CONFIG_PATH = DATA_DIR / "registry_config.json"

# Execution state — follows the cohort since Epic 10 S2, so it changes every day
# and has no place in git: a deploy would destroy it, which is why an automatic
# commit had to exist at all. Lives under the gitignored runtime tree instead,
# like watchlist.json and portfolio.json before it. This module is the single
# place that resolves these paths — readers follow by importing them.
STATE_DIR = REPO_ROOT / "runtime" / "referential"
REGISTRY_PATH = STATE_DIR / "registry.json"  # portfolio_tickers only
CLASSIFICATIONS_PATH = STATE_DIR / "classifications.json"
SINGLE_FACTORS_PATH = STATE_DIR / "single_factor_symbols.json"

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


def load_state(path: Path) -> dict:
    """Read a state file; ``{}`` when it does not exist yet.

    A machine that has never run the bridge has no state at all — that is the
    normal first boot, not an error: the configuration is valid, the state is
    empty, and the first reconciliation rebuilds it. Only an absent file is
    tolerated; a corrupt one still raises, because silently treating garbage as
    "empty" would look exactly like a purge order.
    """
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_registry_symbols(path: Path | None = None) -> set[str]:
    """Return the set of portfolio ticker symbols declared in the registry.

    Resolved at call time, not at import: a default bound to the module constant
    would ignore any later redirection of the state tree, and a test redirecting it
    would silently read the real file — passing while proving nothing.
    """
    return {t["symbol"] for t in load_state(path or REGISTRY_PATH).get("portfolio_tickers", [])}


def load_classified_symbols(path: Path | None = None) -> set[str]:
    """Return the symbols that have an alert classification."""
    return set(load_state(path or CLASSIFICATIONS_PATH).get("classifications", {}))


def load_factor_covered_symbols(
    path: Path | None = None,
    single_factors_path: Path | None = None,
) -> set[str]:
    """Return symbols covered by a sector-factor mapping or a single-factor entry.

    Two sources since Epic 10 S4, one per side of the split: the sector map is
    configuration (an ETF choice, decided in a PR), the single-factor list is
    state (whatever the cohort brought in with no sector guessed for it).
    """
    config = Path(path or SECTOR_FACTORS_PATH)
    sector_map = json.loads(config.read_text(encoding="utf-8")).get("sector_factors", {})
    single = load_state(single_factors_path or SINGLE_FACTORS_PATH)
    return set(sector_map) | set(single.get("single_factor_symbols", []))


def evaluate(
    runtime: dict[str, list[str]],
    registry_symbols: set[str],
    classified_symbols: set[str],
    factor_covered_symbols: set[str],
    light_labels: frozenset[str] = frozenset(),
) -> list[Issue]:
    """Return every coherence issue between runtime lists and the referentials.

    ``runtime`` maps a file label (shown in messages) to its ticker symbols.
    Blocking issues: a runtime ticker missing from the registry, from the alert
    classifications, or from sector-factor coverage. Lists named in
    ``light_labels`` (the watchlist) are the tension tier — Layer C only, OHLCV
    fetched directly, no registry/classification/factor requirement — so their
    gaps are informational, never blocking. Info issues: a registry entry
    absent from every runtime list (the VPS may run shorter local lists).
    """
    issues: list[Issue] = []
    all_runtime: set[str] = set()

    for label, symbols in runtime.items():
        light = label in light_labels
        for sym in symbols:
            all_runtime.add(sym)
            if sym not in registry_symbols:
                if light:
                    issues.append(Issue(
                        INFO, sym,
                        f"{sym}: dans {label}, hors registre — couvert par le tier "
                        "tension (Layer C) uniquement, pas de détection d'anomalie "
                        "Layer B.",
                    ))
                    continue  # Layer B referentials don't apply to the tension tier
                issues.append(Issue(
                    BLOCKING, sym,
                    f"{sym}: présent dans {label} mais absent de registry.json "
                    "(portfolio_tickers) — ajouter l'entrée au registre.",
                ))
            if light:
                continue
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

    A machine with **no state file at all** has nothing to check yet (Epic 10 S4):
    the state is rebuilt by the first bridge run, and that is the normal first boot
    — CI included, since the state left git. Blocking a deploy on it would rebuild
    the very failure mode this epic removes. An existing but incoherent state is
    still blocking: the guard is skipped only when the state does not exist.
    """
    portfolio_path = portfolio_path or _resolve_runtime_path("portfolio")
    watchlist_path = watchlist_path or _resolve_runtime_path("watchlist")

    if not any(
        p.exists() for p in (REGISTRY_PATH, CLASSIFICATIONS_PATH, SINGLE_FACTORS_PATH)
    ):
        print(
            f"registry_check: aucun état de référentiel sous {STATE_DIR} — "
            "rien à vérifier (il sera reconstruit au premier passage du pont)."
        )
        return 0

    runtime = {
        portfolio_path.name: load_runtime_symbols(portfolio_path),
        watchlist_path.name: load_runtime_symbols(watchlist_path),
    }
    issues = evaluate(
        runtime,
        load_registry_symbols(),
        load_classified_symbols(),
        load_factor_covered_symbols(),
        light_labels=frozenset({watchlist_path.name}),
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
