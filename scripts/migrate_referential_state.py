"""Déplace l'état du référentiel de market_intelligence/data/ vers runtime/referential/.

À jouer **avant** le déploiement qui apporte l'Epic 10 S4. Sans cela, le
``git reset --hard origin/main`` du déploiement supprime
``market_intelligence/data/registry.json`` — qui n'est plus versionné — et la
machine redémarre avec un registre vide : plus aucun ticker scanné jusqu'au
premier passage du pont.

Idempotent et non destructif : si l'état est déjà en place, le script ne fait
rien et sort 0. Les anciens fichiers ne sont pas supprimés — le déploiement s'en
charge pour ``registry.json``, et les deux autres perdent simplement les clés
d'état à la mise à jour du code.

    python3 scripts/migrate_referential_state.py [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_intelligence.registry_check import (  # noqa: E402 - after sys.path
    ALERT_THRESHOLDS_PATH,
    CLASSIFICATIONS_PATH,
    DATA_DIR,
    REGISTRY_PATH,
    SECTOR_FACTORS_PATH,
    SINGLE_FACTORS_PATH,
)

LEGACY_REGISTRY = DATA_DIR / "registry.json"


def _read(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict, dry_run: bool) -> None:
    if dry_run:
        print(f"[migrate] (dry-run) écrirait {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[migrate] {path} écrit")


def migrate(dry_run: bool = False) -> int:
    moved = 0

    if REGISTRY_PATH.exists():
        print(f"[migrate] {REGISTRY_PATH} déjà en place — rien à faire")
    elif (legacy := _read(LEGACY_REGISTRY)) is not None:
        _write(REGISTRY_PATH, legacy, dry_run)
        moved += 1
    else:
        print(f"[migrate] ⚠️  ni {REGISTRY_PATH} ni {LEGACY_REGISTRY} — registre vide")

    if CLASSIFICATIONS_PATH.exists():
        print(f"[migrate] {CLASSIFICATIONS_PATH} déjà en place — rien à faire")
    else:
        thresholds = _read(ALERT_THRESHOLDS_PATH) or {}
        classifications = thresholds.get("classifications")
        if classifications:
            _write(CLASSIFICATIONS_PATH, {"classifications": classifications}, dry_run)
            moved += 1
        else:
            print("[migrate] ⚠️  aucune classification à déplacer")

    if SINGLE_FACTORS_PATH.exists():
        print(f"[migrate] {SINGLE_FACTORS_PATH} déjà en place — rien à faire")
    else:
        factors = _read(SECTOR_FACTORS_PATH) or {}
        single = factors.get("single_factor_symbols")
        if single:
            _write(SINGLE_FACTORS_PATH, {"single_factor_symbols": single}, dry_run)
            moved += 1
        else:
            print("[migrate] ⚠️  aucune liste single_factor à déplacer")

    print(f"[migrate] terminé — {moved} fichier(s) déplacé(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="n'écrit rien, montre le plan")
    args = parser.parse_args(argv)
    return migrate(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
