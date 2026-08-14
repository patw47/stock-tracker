#!/usr/bin/env bash
# Pousse l'instantané du screener local vers le VPS (Epic 10 S1).
#
# Le screener tourne sur le poste, derrière une box, sans adresse stable : le VPS
# ne peut pas l'appeler (sept exécutions consécutives en "Connection refused").
# Le sens du tuyau est donc inversé — le poste pousse, le VPS lit un fichier.
#
# Déclenché par screener-push.path à l'apparition d'un instantané dans
# $SCREENER_BACKUPS, jamais par une horloge : la cadence du screener est
# irrégulière (un scan au démarrage du conteneur, cadré par un cache de 12 h),
# et une poussée planifiée enverrait un fichier périmé les jours sans démarrage.
#
# Ce qui est poussé est la réponse de /api/scan, pas le fichier de backup : les
# backups sont volontairement amputés du journal de suivi v5 (dérivable, cf.
# _write_snapshot côté screener) et le pont a besoin de days_held. Le backup ne
# sert que de signal « un scan vient d'avoir lieu ».
set -uo pipefail

API_URL=${SMALLCAPS_API_URL:-http://localhost:8000}
REMOTE=${SCREENER_PUSH_REMOTE:-hetzner-vps}
REMOTE_PATH=${SCREENER_PUSH_PATH:-/opt/apps/stock-tracker/runtime/screener/latest.json}

# ponytail: sleep fixe pour laisser le scan finir d'écrire son instantané. Il ne
# débounce PAS (deux poussées observées à 12 s d'écart pour un seul scan) : dette
# acceptée à la clôture de l'Epic 10, le coût est du réseau gâché, le pont étant
# idempotent et refusant tout instantané plus vieux que le dernier accepté.
sleep 10

tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT

if ! curl -fsS -m 120 "$API_URL/api/scan" -o "$tmp"; then
  echo "screener-push: $API_URL/api/scan injoignable — poussée abandonnée" >&2
  exit 0
fi

# Garde-fou : ne jamais pousser un payload que le pont refuserait de toute façon.
# Une réponse tronquée ou sans journal de suivi écraserait un instantané valide.
if ! python3 -c '
import json, sys
payload = json.load(open(sys.argv[1]))
assert isinstance(payload.get("scanned_at"), str), "scanned_at absent"
rows = (payload.get("v5") or {}).get("tracking") or []
assert rows, "journal de suivi v5 vide"
' "$tmp"; then
  echo "screener-push: payload inutilisable — poussée abandonnée" >&2
  exit 0
fi

# Écriture atomique côté VPS : le pont peut lire pendant la poussée, il ne doit
# jamais tomber sur un fichier à moitié écrit.
if ssh -o BatchMode=yes -o ConnectTimeout=20 "$REMOTE" \
     "mkdir -p \"\$(dirname '$REMOTE_PATH')\" && cat > '$REMOTE_PATH.tmp' && mv '$REMOTE_PATH.tmp' '$REMOTE_PATH'" \
     < "$tmp"; then
  echo "screener-push: $(wc -c <"$tmp") octets poussés vers $REMOTE:$REMOTE_PATH"
else
  echo "screener-push: poussée vers $REMOTE échouée — re-tentée au prochain scan" >&2
fi
