#!/bin/bash
# Agent de veille boursière — démarrage n8n

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Charger nvm
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

# Charger les variables d'environnement
if [ -f "$SCRIPT_DIR/.env" ]; then
  export $(grep -v '^#' "$SCRIPT_DIR/.env" | xargs)
fi

# Variables n8n
export N8N_PORT=5680
export N8N_BASIC_AUTH_ACTIVE=true
export N8N_BASIC_AUTH_USER=admin
export N8N_BASIC_AUTH_PASSWORD=stockwatcher2026
export WATCHLIST_PATH="$SCRIPT_DIR/watchlist.json"
export N8N_USER_FOLDER="$SCRIPT_DIR/.n8n-data"
export GENERIC_TIMEZONE="Europe/Paris"
export N8N_DEFAULT_LOCALE=fr

echo "=========================================="
echo "  Agent de Veille Boursière — n8n"
echo "  Tickers : OKLO, SMR, HIMS, ALTD"
echo "  Schedule : 8h00 (Lun-Ven)"
echo "  Interface : http://localhost:5680"
echo "  Login    : admin / stockwatcher2026"
echo "=========================================="
echo ""

# Importer le workflow si pas encore fait
if ! n8n list:workflow 2>/dev/null | grep -q "Veille Boursière"; then
  echo "→ Import du workflow..."
  n8n import:workflow --input="$SCRIPT_DIR/workflow.json" && echo "✓ Workflow importé"
else
  echo "✓ Workflow déjà présent"
fi

echo "→ Démarrage de n8n..."
exec n8n start
