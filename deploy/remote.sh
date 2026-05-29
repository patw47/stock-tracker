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

# 3. Restart des services dans l'ordre des dépendances.
log "restart openclaw-warren"; sudo systemctl restart openclaw-warren; sleep 2
log "restart warren-server";  sudo systemctl restart warren-server;  sleep 2
log "restart stock-tracker";  sudo systemctl restart stock-tracker

# 4. Laisser n8n démarrer.
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

# 6. Run manuel de validation : exécute le workflow une fois pour vérifier la chaîne
#    bout-en-bout et recevoir le briefing Telegram (si news). Lancé en tant que warren,
#    seul à pouvoir lire .n8n/config et donc déchiffrer les credentials.
#    NB: pipeline SKIP -> pas de briefing si aucune news du jour (comportement normal).
log "execute workflow $WID (run manuel de validation)"
sudo -u warren env \
  N8N_USER_FOLDER="$N8N_DATA" \
  N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS=false \
  n8n execute --id="$WID"
trigger_rc=$?
if [ "$trigger_rc" -eq 0 ]; then trigger="ok"; else trigger="fail"; fi
log "execute rc=$trigger_rc"

overall="ok"
if [ "$st_active" != "active" ] || [ "$n8n_health" != "ok" ] \
   || [ "$warren_status" != "active" ] || [ "$import_rc" -ne 0 ] \
   || [ "$trigger" != "ok" ]; then
  overall="fail"
fi

log "résumé: stock-tracker=$st_active n8n=$n8n_health(http=$code) warren=$warren_status (openclaw=$oc_active bridge=$ws_active http=$wcode) import_rc=$import_rc trigger=$trigger"

# Lignes machine-lisibles consommées par deploy.yml.
echo "STATUS_STOCK_TRACKER=$st_active"
echo "STATUS_N8N_HEALTH=$n8n_health"
echo "STATUS_WARREN=$warren_status"
echo "STATUS_TRIGGER=$trigger"
echo "STATUS_OVERALL=$overall"
