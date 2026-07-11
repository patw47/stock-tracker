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

# 2b. Validation de cohérence des référentiels tickers (Epic 4 S1) sur les
#     fichiers runtime réels du VPS, AVANT le restart. Une divergence
#     (ticker sans registre / classification / facteur) => STATUS_OVERALL=fail.
log "validate ticker referential (registry_check)"
( cd "$REPO" && python3 -m market_intelligence.registry_check \
    --portfolio "$REPO/portfolio.json" --watchlist "$REPO/watchlist.json" )
registry_rc=$?

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

# 5b. Watchdog EOD (Epic 2 S2) : installe/active le timer systemd. Indépendant de
#     n8n — alerte Telegram si le run 21:30 UTC manque. N'influence pas overall
#     (garde-fou externe) mais son état est rapporté via STATUS_WATCHDOG_TIMER.
log "install/enable watchdog EOD timer"
sudo cp "$REPO/deploy/eod-watchdog.service" /etc/systemd/system/eod-watchdog.service
sudo cp "$REPO/deploy/eod-watchdog.timer"   /etc/systemd/system/eod-watchdog.timer
sudo systemctl daemon-reload
sudo systemctl enable --now eod-watchdog.timer >/dev/null 2>&1
wd_timer=$(systemctl is-active eod-watchdog.timer 2>/dev/null || echo inactive)

# 5c. Outcome tracker (Epic 5 S1) : timer systemd, ~1h après le run EOD, jours
#     ouvrés. Hors chemin critique (mesure a posteriori des alertes) — n'influence
#     pas overall, état rapporté via STATUS_OUTCOME_TIMER.
log "install/enable outcome tracker timer"
sudo cp "$REPO/deploy/outcome-tracker.service" /etc/systemd/system/outcome-tracker.service
sudo cp "$REPO/deploy/outcome-tracker.timer"   /etc/systemd/system/outcome-tracker.timer
sudo systemctl daemon-reload
sudo systemctl enable --now outcome-tracker.timer >/dev/null 2>&1
ot_timer=$(systemctl is-active outcome-tracker.timer 2>/dev/null || echo inactive)

# 5d. Tension outcomes (Layer C) : timer systemd, juste après outcome-tracker,
#     jours ouvrés. Hors chemin critique — n'influence pas overall, état
#     rapporté via STATUS_TENSION_TIMER.
log "install/enable tension outcomes timer"
sudo cp "$REPO/deploy/tension-outcomes.service" /etc/systemd/system/tension-outcomes.service
sudo cp "$REPO/deploy/tension-outcomes.timer"   /etc/systemd/system/tension-outcomes.timer
sudo systemctl daemon-reload
sudo systemctl enable --now tension-outcomes.timer >/dev/null 2>&1
tn_timer=$(systemctl is-active tension-outcomes.timer 2>/dev/null || echo inactive)

# 5e. Referential sync (Telegram → git) : path unit sur market_intelligence/data/,
#     commit [skip ci] + push après onboarding/offboarding. Hors chemin critique.
log "install/enable referential sync path unit"
sudo cp "$REPO/deploy/referential-sync.service" /etc/systemd/system/referential-sync.service
sudo cp "$REPO/deploy/referential-sync.path"    /etc/systemd/system/referential-sync.path
sudo systemctl daemon-reload
sudo systemctl enable --now referential-sync.path >/dev/null 2>&1
rs_path=$(systemctl is-active referential-sync.path 2>/dev/null || echo inactive)

overall="ok"
if [ "$st_active" != "active" ] || [ "$n8n_health" != "ok" ] \
   || [ "$warren_status" != "active" ] || [ "$import_rc" -ne 0 ] \
   || [ "$registry_rc" -ne 0 ]; then
  overall="fail"
fi

log "résumé: stock-tracker=$st_active n8n=$n8n_health(http=$code) warren=$warren_status (openclaw=$oc_active bridge=$ws_active http=$wcode) watchdog_timer=$wd_timer outcome_timer=$ot_timer tension_timer=$tn_timer import_rc=$import_rc registry_rc=$registry_rc"

# Lignes machine-lisibles consommées par deploy.yml.
echo "STATUS_STOCK_TRACKER=$st_active"
echo "STATUS_N8N_HEALTH=$n8n_health"
echo "STATUS_WARREN=$warren_status"
echo "STATUS_WATCHDOG_TIMER=$wd_timer"
echo "STATUS_OUTCOME_TIMER=$ot_timer"
echo "STATUS_TENSION_TIMER=$tn_timer"
echo "STATUS_REFERENTIAL_SYNC=$rs_path"
echo "STATUS_OVERALL=$overall"
