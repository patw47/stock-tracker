#!/usr/bin/env bash
# Déploiement Stock Tracker — exécuté SUR le VPS en tant que queenp (sudo passwordless).
# Le code est déjà à jour (git pull fait par deploy.yml juste avant l'appel).
# Étapes : import du workflow (écriture sqlite directe), restart des 3 services,
# healthchecks. Émet des lignes STATUS_* en fin, parsées par deploy.yml.
#
# Pas de `set -e` : on collecte l'état même en cas d'échec partiel pour pouvoir rapporter.
set -uo pipefail

REPO="/opt/apps/stock-tracker"
N8N_DB="$REPO/n8n-data/.n8n/database.sqlite"
N8N_DATA="$REPO/n8n-data"
N8N_PORT="${N8N_PORT:-5680}"
WARREN_PORT="18795"

log() { echo "[deploy] $*"; }

# id du workflow canonique, lu depuis workflow.json (évite toute dérive).
WID=$(python3 -c "import json;print(json.load(open('$REPO/workflow.json'))['id'])")

# Seed runtime ticker lists on a fresh install (gitignored; edited live via Telegram).
for L in watchlist portfolio; do
  [ -f "$REPO/$L.json" ] || cp "$REPO/$L.example.json" "$REPO/$L.json"
done

# 0. Dépendances Python du bridge Warren (warren_server.py utilise pydantic + requests).
#    Installées dans le python système (/usr/bin/python3, celui du service warren-server).
if [ -f "$REPO/requirements.txt" ]; then
  log "install deps python (requirements.txt)"
  sudo python3 -m pip install -r "$REPO/requirements.txt" --break-system-packages -q \
    || sudo python3 -m pip install -r "$REPO/requirements.txt" -q \
    || log "WARN pip install a échoué — warren-server peut ne pas démarrer"
fi

# 1. Stop n8n avant l'import (évite tout verrou sqlite concurrent).
log "stop stock-tracker"
sudo systemctl stop stock-tracker
sleep 2

# 2. Import du workflow par écriture sqlite directe (queenp possède la DB).
#    Active veille-boursiere-001 et désactive les autres workflows.
log "import workflow.json -> sqlite"
N8N_DB="$N8N_DB" WORKFLOW_JSON="$REPO/workflow.json" \
  python3 "$REPO/deploy/import_workflow.py"
import_rc=$?

# 3. Relancer la stack Warren (openclaw + bridge). Le service n8n reste ARRÊTÉ pour l'instant.
log "restart openclaw-warren"; sudo systemctl restart openclaw-warren; sleep 2
log "restart warren-server";  sudo systemctl restart warren-server;  sleep 3

# 4. Démarrer le service n8n.
log "start stock-tracker"; sudo systemctl restart stock-tracker
sleep 8

# 5. Healthchecks.
st_active=$(systemctl is-active stock-tracker  2>/dev/null || echo inactive)
oc_active=$(systemctl is-active openclaw-warren 2>/dev/null || echo inactive)
ws_active=$(systemctl is-active warren-server   2>/dev/null || echo inactive)

n8n_health="fail"
code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$N8N_PORT/healthz" 2>/dev/null || echo 000)
[ "$code" = "200" ] && n8n_health="ok"

# Bridge Warren : pas de route GET, donc toute réponse HTTP (ex. 501) = process vivant.
warren_http="fail"
wcode=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$WARREN_PORT/" 2>/dev/null || echo 000)
[ "$wcode" != "000" ] && warren_http="ok"

# "warren tourne" = gateway openclaw active + bridge service active + port qui répond.
warren_status="inactive"
if [ "$oc_active" = "active" ] && [ "$ws_active" = "active" ] && [ "$warren_http" = "ok" ]; then
  warren_status="active"
fi

overall="ok"
if [ "$st_active" != "active" ] || [ "$n8n_health" != "ok" ] \
   || [ "$warren_status" != "active" ] || [ "$import_rc" -ne 0 ]; then
  overall="fail"
fi

log "résumé: stock-tracker=$st_active n8n=$n8n_health(http=$code) warren=$warren_status (openclaw=$oc_active bridge=$ws_active http=$wcode) import_rc=$import_rc"

# Lignes machine-lisibles consommées par deploy.yml.
echo "STATUS_STOCK_TRACKER=$st_active"
echo "STATUS_N8N_HEALTH=$n8n_health"
echo "STATUS_WARREN=$warren_status"
echo "STATUS_OVERALL=$overall"
