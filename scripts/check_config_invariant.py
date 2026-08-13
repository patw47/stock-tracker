"""Verrouille la moitié CONFIGURATION du référentiel contre une altération.

L'Epic 10 S4 sépare, *à l'intérieur* des fichiers, ce qui se décide de ce qui suit
la cohorte. La moitié qui se décide — les seuils d'alerte et la table des facteurs
sectoriels — doit traverser le déplacement sans qu'une seule valeur change. Ce
script prend son empreinte et la compare à une référence figée **avant** la
migration.

Pourquoi une empreinte et pas ``git diff`` : les fichiers d'état sont désormais
gitignorés, donc un diff y est vide par construction et vrai par vacuité. Et
l'invariant ne peut pas porter sur l'état — il change tous les jours, une égalité
permanente y serait intenable et le critère ne pourrait jamais être vert. Il porte
donc sur la configuration seule, avec une tolérance nulle.

Après la migration, le script garde son utilité : il empêche qu'un chemin
automatique (onboarding, offboarding, synchronisation) réécrive un fichier de
configuration. Un changement volontaire de seuil se fait en PR, puis
``--update`` regénère la référence — c'est la seule façon de la faire bouger.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_intelligence.registry_check import (  # noqa: E402 - after sys.path
    ALERT_THRESHOLDS_PATH,
    DATA_DIR,
    SECTOR_FACTORS_PATH,
)

REFERENCE_PATH = DATA_DIR / "config_invariant.json"


def config_snapshot() -> dict:
    """Le contenu de configuration couvert par l'invariant, clés et valeurs."""
    thresholds = json.loads(ALERT_THRESHOLDS_PATH.read_text(encoding="utf-8"))
    factors = json.loads(SECTOR_FACTORS_PATH.read_text(encoding="utf-8"))
    return {
        "alert_thresholds.thresholds": thresholds["thresholds"],
        "sector_factors.market_factor": factors["market_factor"],
        "sector_factors.correlation_threshold": factors["correlation_threshold"],
        "sector_factors.sector_factors": factors["sector_factors"],
    }


def digest(snapshot: dict) -> str:
    """SHA-256 d'une sérialisation canonique : mêmes valeurs ⇒ même empreinte."""
    blob = json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update",
        action="store_true",
        help="regénère la référence (uniquement après un changement de seuil voulu, en PR)",
    )
    args = parser.parse_args(argv)

    snapshot = config_snapshot()
    current = digest(snapshot)

    if args.update:
        REFERENCE_PATH.write_text(
            json.dumps(
                {"sha256": current, "covers": sorted(snapshot)},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[config-invariant] référence regénérée : {current}")
        return 0

    reference = json.loads(REFERENCE_PATH.read_text(encoding="utf-8"))
    if current == reference["sha256"]:
        print(f"[config-invariant] OK — {len(snapshot)} entrées, sha256={current[:16]}…")
        return 0

    print(f"[config-invariant] ÉCHEC — attendu {reference['sha256']}, obtenu {current}")
    print("La configuration a changé. Si c'est voulu, relancer avec --update en PR.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
