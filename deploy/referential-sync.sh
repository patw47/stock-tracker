#!/usr/bin/env bash
# Reporte vers git les changements de référentiels faits depuis Telegram
# (onboarding/offboarding via /modifyportfolio, /modifywatchlist).
# Déclenché par referential-sync.path à chaque écriture dans market_intelligence/data/.
# [skip ci] : commit de données pur, pas de redéploiement (les fichiers sont déjà
# à jour sur le VPS — le prochain deploy réel fera un pull fast-forward propre).
set -uo pipefail
REPO=/opt/apps/stock-tracker
cd "$REPO" || exit 1

# ponytail: sleep fixe pour agréger les écritures multi-fichiers d'un même
# onboarding ; passer à un vrai debounce si des rafales posent problème.
sleep 5

git add market_intelligence/data/
git diff --cached --quiet && exit 0

git commit -m "chore(referential): sync tickers depuis Telegram [skip ci]" \
  -m "Commit automatique (referential-sync.path) après /modifyportfolio ou /modifywatchlist."

# Rebase avant push : un merge concurrent sur main ne doit pas bloquer la synchro.
git pull --rebase --autostash origin main || true
git push origin main || echo "referential-sync: push échoué — re-tenté au prochain déclenchement" >&2
