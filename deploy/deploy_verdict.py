#!/usr/bin/env python3
"""Verdict de déploiement (STATUS_OVERALL) — Epic 9 Sprint 2.

Isole le calcul du verdict de deploy/remote.sh pour le rendre testable sans
VPS : les statuts sont fournis en entrée (variables d'environnement), aucun
appel systemctl/curl n'a lieu ici. Le résultat ("ok" ou "fail") est imprimé
sur stdout, lu par remote.sh.
"""

from __future__ import annotations

import os

# Doivent valoir "active" pour un déploiement ok : les services/gateway existants
# + les 4 timers systemd (Epic 9 S2 — avant ce sprint, seuls STOCK_TRACKER et
# WARREN comptaient). STATUS_REFERENTIAL_SYNC en faisait partie jusqu'à la clôture
# de l'Epic 10 : l'état étant sorti de git au S4, la path unit qu'il surveillait
# n'avait plus d'écrivain et a été supprimée.
_REQUIRED_ACTIVE = (
    "STATUS_STOCK_TRACKER",
    "STATUS_WARREN",
    "STATUS_WATCHDOG_TIMER",
    "STATUS_OUTCOME_TIMER",
    "STATUS_TENSION_TIMER",
    "STATUS_V5_TIMER",
)


def compute_overall(statuses: dict[str, str], *, import_rc: int, registry_rc: int) -> str:
    """Retourne "ok" si tout est actif, le healthcheck n8n ok, et les deux retours à 0 ; "fail" sinon."""
    if import_rc != 0 or registry_rc != 0:
        return "fail"
    if statuses.get("STATUS_N8N_HEALTH") != "ok":
        return "fail"
    if any(statuses.get(key) != "active" for key in _REQUIRED_ACTIVE):
        return "fail"
    return "ok"


def main() -> int:
    statuses = {key: os.environ.get(key, "") for key in (*_REQUIRED_ACTIVE, "STATUS_N8N_HEALTH")}
    import_rc = int(os.environ.get("IMPORT_RC", "1"))
    registry_rc = int(os.environ.get("REGISTRY_RC", "1"))
    print(compute_overall(statuses, import_rc=import_rc, registry_rc=registry_rc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
