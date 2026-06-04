# 1. Constat & reformulation du problème

Le pipeline actuel (recherche web Warren, 1×/jour à la clôture) souffre d'une **latence structurelle** : la news arrive après que le prix a déjà bougé. Sur un univers de micro/small-caps spéculatives (quantique, nucléaire SMR, IA défense, AR), le prix bouge **souvent avant** que la news soit publique (accumulation, rumeur, flux, squeeze).

**Le problème n'est donc pas la prédiction, c'est la latence de détection.**

- Prédire la direction d'un mouvement *avant* qu'il survienne, de façon fiable, n'est pas exploitable (marchés efficients en forme faible ; les figures classiques de l'AT n'ont pas d'edge prédictif robuste).
- Ce qui EST exploitable : **détecter qu'un mouvement est en train de démarrer**, plus vite que la news, via des anomalies numériques prix/volume. C'est un outil d'**attention**, pas un oracle.

> Objectif de la refonte : ajouter une couche de détection intraday quasi-temps-réel (déclenchement « quand ») complémentaire de la couche news/macro existante (contexte « pourquoi »).
>

# 2. Vérifications humaines post-déploiement

Ce qu'un humain doit contrôler pour s'assurer que la feature fonctionne — beaucoup d'erreurs ici sont **silencieuses** (le système tourne sans planter mais sur de mauvaises bases).

### A. Intégrité des données (à faire AVANT de faire confiance à quoi que ce soit)

- **Spot-check J+1** : comparer 2–3 cours de clôture récupérés (dont **MMED** et **PLX**) avec Yahoo / ProRealTime. Si MMED ne correspond pas à MiniMed, toute l'analyse est fausse silencieusement.
- Vérifier que les **16 tickers** ont renvoyé des données ; repérer ceux flaggés manquants (mauvais symbole, delisting, panne API).
- Confirmer que les **tickers macro à symbole spécial** (^VIX, ^TNX, DXY, pétrole) ont bien résolu — ce sont eux qui cassent le plus souvent côté API.

### B. Qualité du signal (sur quelques semaines d'observation)

- **Test du gate** : un jour de risk-off généralisé (IWM en forte baisse), vérifier que le système n'a PAS alerté sur tous les titres beta. Si tout sonne, le gate ne fonctionne pas.
- À l'inverse, un jour où un titre a eu une vraie news idiosyncratique : vérifier qu'il a bien alerté.
- **Compter les alertes/jour** : trop (6–8/jour) → seuils trop laxes / queues épaisses mal gérées → monter le multiplicateur, vérifier l'échelle MAD. Zéro pendant des semaines sur des titres volatils → trop serré.
- **IPO récentes** (MiniMed ~3 mois, OKLO/SMR/RGTI) : confirmer que le fallback historique court s'active et ne produit pas de z aberrants ni d'erreurs.

### C. Dédup (nécessite une observation multi-jours)

- Suivre un titre en tendance plusieurs jours : confirmer **UNE** alerte, pas une par jour.
- Confirmer qu'un vrai nouvel événement (inversion de direction / nouveau catalyseur) **ré-alerte** bien.

### D. Qualité Warren

- Lire quelques sorties Warren d'un œil critique : quand une alerte tombe **sans news**, Warren dit-il « squeeze/flux possible » ou **invente-t-il** une raison ? S'il confabule, durcir le prompt.
- Vérifier que les données **insider EDGAR** sont réelles et fraîches (recouper avec EDGAR pour un titre ayant eu une transaction connue).
- Vérifier le **flag squeeze** sur un nom notoirement shorté (cohérence avec la réalité).

### E. Ops, coût, planning

- **Coût** : surveiller la dépense API Warren/Haiku — confirmer que la dédup limite bien les appels (seules les nouvelles alertes idiosyncratiques déclenchent Warren).
- **Coexistence** : confirmer que la Couche A (news) tourne toujours, que les deux couches ne double-envoient pas, et que le macro est calculé une seule fois.
- **Telegram** : digest reçu, formaté, découpé si long ; jours sans alerte se comportent comme prévu.
- **Planning / fuseaux** : confirmer l'exécution après la clôture US. ⚠️ Les changements d'heure US et UE ne sont pas synchrones → l'heure de Paris de la clôture US se décale ~2 fois/an. Caler le trigger sur la clôture US plutôt que sur une heure de Paris fixe, ou ajuster aux transitions DST.