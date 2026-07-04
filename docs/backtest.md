# Backtest & calibration des seuils (Epic 5 S3)

Rejoue le pipeline de détection S0–S5 jour par jour sur l'historique des 16 tickers
pour répondre à deux questions :

1. Combien d'alertes/mois produit une combinaison de seuils ?
2. Quelle est la sensibilité au couple **(speculative_z, rearm_z)** ?

## Usage

```bash
python3 -m market_intelligence.backtest --start 2024-07-01 --end 2026-06-30
```

Options :

- `--start` / `--end` (`YYYY-MM-DD`, requis) : fenêtre simulée.
- `--out` : fichier JSONL de sortie (défaut `runtime/market_intelligence/backtest.jsonl`).
- `--state-dir` : répertoire des états dedup **éphémères** (défaut : dossier temp).

## Ce que fait le harness

- **Un seul fetch batch** de l'historique complet (rate limit yfinance), puis
  itération jour par jour en **tronquant** le cache à `bar_date <= jour simulé`
  (garantie no-look-ahead — le pipeline ne voit jamais une barre future).
- Pour chaque combinaison de la grille (par défaut `speculative_z ∈ {2.0, 2.5, 3.0}`
  × `rearm_z ∈ {0.8, 1.0, 1.5}`), rejoue tous les jours avec :
  - un **état dedup éphémère** (fichier tmp par combinaison) — l'hystérésis
    (rearm/escalation) évolue jour après jour sans jamais toucher
    `runtime/market_intelligence/dedup_state.json` de prod ;
  - un **analyzer stub** (zéro appel Warren/OpenClaw) et un **short_interest stub** ;
  - `dry_run=True` + `skip_warren=True`.
- Les overrides de seuils sont faits **en mémoire** (`dataclasses.replace`) : aucun
  fichier de configuration de prod n'est modifié.

## Sortie

- **JSONL** : une ligne par combinaison — `speculative_z`, `rearm_z`, `total_alerts`,
  `alerts_per_month`, `z_median`, `z_max`, `per_ticker`, `per_signal`.
- **Résumé texte** (stdout) : combinaisons triées par alertes/mois décroissantes,
  avec la médiane des z et les tickers les plus alertés.

## Interprétation

- `alerts_per_month` trop élevé ⇒ seuil trop permissif (bruit) ; trop bas ⇒ risque
  de manquer des mouvements. Croiser avec les `outcomes.jsonl` (S1) pour estimer le
  hit rate par seuil quand les fenêtres J+N existent.
- Ce sprint **ne change aucun seuil en prod** : il produit un rapport d'aide à la
  décision uniquement.
