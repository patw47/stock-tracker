# Layer C — tension

Statut : **alertes réelles** depuis 2026-07-10, sur décision utilisateur.
La mesure tourne en parallèle (journal quotidien + `tension_outcomes`) pour
vérifier si les données live confirment l'hypothèse. Le repère chiffré
ci-dessous sert à lire ces données, rien de plus : maintien, retrait ou
ajustement des alertes restent des décisions du propriétaire, à tout moment.
Note technique : garder les seuils stables garde les chiffres live comparables
au backtest phase 0 ; les changer remet le compteur de mesure à zéro.

## Hypothèse

Les explosions de prix sur small caps sont précédées de signes d'accumulation
silencieuse détectables sur barres de clôture : compression de volatilité
(squeeze) et volume élevé sans mouvement de prix. Un score de tension calculé
à la clôture anticipe le mouvement de plusieurs jours (lead médian ~13 j en
backtest), là où la détection d'anomalie (z-score sur le move du jour) ne fait
que photographier un mouvement terminé — mesuré dans `docs/RESULTS.md` :
hit rates directionnels 41-49 % ≈ pile ou face.

## Définitions (identiques à `scripts/tension_backtest.py` v1)

| élément | formule | seuil |
|---|---|---|
| SQUEEZE | percentile du bandwidth Bollinger 20j (std20/mean20 des closes) dans l'année glissante (252j, min 120) | pctl ≤ 10 % |
| QUIET (accumulation calme) | rvol5 = moyenne 5j de (volume / moyenne 20j antérieure) ; cum5 = rendement cumulé 5j | rvol5 ≥ 2.0 **et** \|cum5\| < 3 % |
| TENSION | SQUEEZE ou QUIET | — |
| épisode | jours de tension consécutifs regroupés ; l'événement = le **premier** jour | — |
| move attendu 20j | (ATR14 / close) × √20, journalisé à l'entrée | — |
| EXPLOSION | max \|close_{t+k}/close_t − 1\|, k ≤ 20, > **2 ×** move attendu | — |

Pas de direction prédite : la compression prédit l'expansion, pas le signe.

## Repère de lecture (benchmark noté le 2026-07-10, avant les données)

- Signal **confirmé** si : lift live ≥ **1.5** sur ≥ **50 épisodes mesurés**,
  lift = P(explosion | épisode) / base rate même-ticker (recalculé via
  `scripts/tension_backtest.py --start <début du live>`).
- Signal **infirmé** si : lift < 1.5 une fois 50 épisodes mesurés.
- Cadence attendue : ~6 épisodes/mois sur l'univers backtest (16 tickers) ;
  avec le registre à 166 (PR #53), ~10× plus → les 50 épisodes mesurables
  devraient être atteints en ~1-2 mois (mesure J+20 + 40 j calendaires de délai).
  Attention : le backtest phase 0 reste mesuré sur 16 tickers — les lifts live
  sur 166 ne sont comparables qu'au sens du même calcul, pas du même univers.
- Ce repère informe ; il ne décide de rien.

## Ce que le backtest (phase 0) a montré — et pas montré

Fenêtre 2022-01 → 2026-05, 16 tickers, événement vol-normalisé (base rate 8 %) :

| condition | épisodes | lift plein | lift 2022-01→2024-03 | lift 2024-04→2026-05 |
|---|--:|--:|--:|--:|
| SQUEEZE pctl≤10 % | 246 | 1.66 | 1.25 | 1.91 |
| QUIET rvol5≥2 | 109 | 1.35 | 1.04 | 2.04 |
| combiné | 338 | 1.50 | 1.11 | 1.92 |

- Le signal existe sur la fenêtre pleine (~2.6σ) mais est **instable entre
  régimes** : porté par 2024-2026, absent en 2022-2023 (bear market corrélé).
  D'où cette validation live au lieu d'un déploiement direct.
- Le ratio ATR brut (v0) ne discrimine pas (lift 0.97) ; seul le squeeze en
  **percentile** porte le signal. Le spike volume 1-jour est un anti-signal (0.71).
- 24 cellules testées en phase 0 → les meilleurs lifts sont optimistes
  (comparaisons multiples). Le critère live est le vrai test.

## Câblage opérationnel

- `market_intelligence/tension_signals.py` — calcul + journal + bloc digest.
  Journalisation quotidienne de **tous** les tickers (features complètes) dans
  `runtime/market_intelligence/tension.jsonl` via l'orchestrateur EOD
  (`tension_journal_path`, hors chemin critique, échec non bloquant).
- **Périmètre : tout le registre (166 depuis PR #53 — portefeuille + watchlist)
  + tout ticker watchlist pas encore au registre** (*tier tension*, filet de
  sécurité : OHLCV fetché en un appel batch via `fetch_symbols`, aucun prérequis
  registre/classification/facteur, symbole invalide → frame vide, jamais
  journalisé). Un ajout `/modifywatchlist` est donc couvert par la tension le
  soir même, avant son onboarding registre ; `registry_check` le signale en
  info, pas en bloquant.
- Bloc Telegram « ⚡ Tension — Layer C » ajouté au digest existant,
  uniquement sur les **débuts** d'épisode. Déterministe, zéro Warren/LLM.
- `python3 -m market_intelligence.tension_outcomes` — mesure les épisodes dus
  (J+20 complet, ≥ 40 j calendaires) → `tension_outcomes.jsonl` ; `--report`
  affiche n épisodes mesurés / explosions / P(explosion).
  Branché en timer systemd (`deploy/tension-outcomes.timer`, 22:35 UTC jours
  ouvrés, installé par `deploy/remote.sh`), même contrat qu'`outcome_tracker`.
- Couche anomalie (Layer B) : inchangée dans le digest. Sa valeur mesurée est
  la sélectivité (−62 % d'alertes vs naïf), pas la direction (`docs/RESULTS.md`).
