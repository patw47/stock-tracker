---
name: tickerbrief
description: Brief à la demande sur un ticker précis depuis Telegram. Déclencheurs "brief TICKER", "point sur TICKER", "actu TICKER". Assemble mémoire news + news fraîches web + état anomalie EOD + secteur. Lecture seule — aucune écriture fichier.
metadata:
  openclaw:
    emoji: 📊
---

# tickerbrief

Brief complet à la demande sur un ticker : news fraîches du jour, rappel mémoire news,
dernier état anomalie EOD. **Lecture seule — ne jamais écrire dans aucun fichier.**

## Déclencheurs

- `brief TICKER`
- `point sur TICKER`
- `actu TICKER`

(TICKER = symbole boursier, casse insensible — normaliser en majuscules)

## Étapes (dans cet ordre)

### 1. Extraire et normaliser le ticker

Mettre le symbole en majuscules. Si aucun symbole valide n'est détectable, répondre :

```
Aucun ticker détecté dans le message.
```

### 2. Vérifier si le ticker est suivi

Lire `/opt/apps/stock-tracker/portfolio.json` et `/opt/apps/stock-tracker/watchlist.json`.
Extraire les symboles (champ `tickers[].symbol`).

**Ticker non suivi** (absent des deux fichiers) → effectuer uniquement une recherche web brute,
puis répondre :

```
⚠️ TICKER non suivi (hors portfolio et watchlist).
Recherche web brute uniquement — aucune donnée de suivi disponible.

[news brutes du jour]
```

Ne jamais inventer de données d'anomalie ou de mémoire pour un ticker non suivi.

### 3. Assembler le contexte (ticker suivi uniquement)

#### 3a. Mémoire news

Lire `/home/warren/.openclaw/workspace-warren/memory/tickers/SYMBOL.md`.

- Fichier absent → mentionner `(aucune mémoire enregistrée)`.
- Fichier présent → extraire les entrées (`## DATE` + texte), 3 max.

#### 3b. News fraîches du jour

Effectuer une web search : `SYMBOL stock news today`.

- Aucune news → mentionner `(aucune news trouvée aujourd'hui)`.
- Résultats → garder 2-3 points pertinents, dater chaque item.

#### 3c. État anomalie EOD

Lire `/opt/apps/stock-tracker/runtime/market_intelligence/dedup_state.json`.

- Fichier absent ou ticker absent de `tickers` → mentionner `(aucune alerte enregistrée)`.
- Ticker présent → extraire : `latched`, `direction`, `trigger_z_resid`, `last_alert_as_of`,
  `latched_since`.

#### 3d. Secteur

Lire `/opt/apps/stock-tracker/market_intelligence/data/sector_factors.json`.
Chercher le symbole dans `sector_factors` ou `single_factor_symbols`.

- Absent → mentionner `(secteur non mappé)`.

### 4. Composer la réponse

**Une seule réponse Telegram. Format signal-first.**

```
📊 SYMBOL — YYYY-MM-DD

🔴/🟢/⚪ ANOMALIE : [latché direction depuis DATE, z=X.XX | armé | aucune alerte]

📰 NEWS AUJOURD'HUI
[2-3 items datés, ou "(aucune news trouvée aujourd'hui)"]

🗃️ MÉMOIRE (3 dernières entrées)
[résumé mémoire, ou "(aucune mémoire enregistrée)"]

🏷️ SECTEUR : [ETF(s) ou "(secteur non mappé)"]
```

Règles de format :
- Pas de heading `#` de niveau 1+.
- Pas de tableau Markdown.
- Signal anomalie en premier (`🔴` latché / `🟢` armé-actif / `⚪` aucune alerte).
- Si toutes les sections contextuelles sont absentes, indiquer explicitement chaque absence.

## Règles absolues

- **Lecture seule** : ne jamais écrire dans `memory/tickers/`, `dedup_state.json`, ni aucun fichier.
- **Jamais d'invention** : si une donnée est absente, le dire explicitement plutôt qu'inventer.
- **Ticker non suivi** : réponse honnête + web brute uniquement.
- Ne pas déclencher de digest systématique ni mémoriser la demande.
