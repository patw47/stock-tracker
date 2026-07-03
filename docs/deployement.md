## Deployment (CI/CD)

Auto-deploy on push to `main`, gated on green CI. The deploy job runs on a
**self-hosted GitHub Actions runner installed on the VPS** (`actions.runner.*` service,
user `queenp`) — GitHub cloud runners cannot SSH in (the VPS firewall blackholes their
return traffic; the handshake stalls to sshd's LoginGraceTime). Running on the VPS means
no SSH at all.

```
push main → CI green ──► deploy.yml (workflow_run, runs-on: self-hosted)
                              │
                              ├─ gate: "Notify PR merge conflicts" green on same SHA
                              ├─ git fetch (token-auth) + reset --hard origin/main
                              └─ deploy/remote.sh (local on VPS):
                                   pip install -r requirements.txt   (system python)
                                   stop stock-tracker
                                   import_workflow.py  (sqlite upsert, queenp owns DB)
                                   restart openclaw-warren → warren-server
                                   n8n execute --id=<wf>  (validation run, as warren, n8n stopped)
                                   start stock-tracker
                                   healthcheck services + n8n :5680/healthz + bridge :18795
                              → Telegram status (✅/❌ + PROJECT_NAME + per-service state)
```

**Roles** — `queenp` runs the infra (git, sqlite, systemctl via passwordless sudo, hosts
the runner); `warren` is only the OpenClaw agent user (limited; owns `.n8n/config`).

**Workflow import** — n8n CLI `import:workflow` is blocked (`EACCES` on `.n8n/config`,
ACL mask `---`). Instead `import_workflow.py` writes `workflow.json` straight into
`database.sqlite` (owned by `queenp`), upserts `veille-boursiere-001` as the single active
workflow, and deactivates any other (killed the legacy `48dff…`). n8n is stopped during
import to avoid sqlite locks.

**Validation run** — `remote.sh` runs `n8n execute --id` once **while the n8n service is
stopped** (so its task broker port 5679 is free) and **as `warren`** (only user that can
read `.n8n/config` to decrypt credentials). The workflow carries an **Execute Workflow
Trigger** node (besides the Schedule Trigger) because the CLI cannot start from a schedule
trigger. SKIP logic means no briefing is sent if there's no fresh news — expected; the
deploy status message still arrives.

**Python deps** — `warren_server.py` / `agents/warren` need `pydantic`, `requests` and
`anthropic` (macro snapshot via web search); `market_intelligence/` needs `numpy`,
`pandas`, `yfinance`, `pyarrow` (`requirements.txt`); `remote.sh` pip-installs them into
the system python (the one `warren-server.service` runs) before restarting.

**Service env** — `warren-server.service` must load `/opt/apps/stock-tracker/.env`
(`EnvironmentFile=` directive — applied via drop-in `warren-server.service.d/override.conf`
on the VPS, 2026-06-11) so the bridge has `ANTHROPIC_API_KEY` for the macro snapshot.
Without it the briefing silently falls back to hardcoded macro values.

**Required GitHub secrets** — `TELEGRAM_ORCHESTRATION_BOT_TOKEN`,
`TELEGRAM_ORCHESTRATION_CHAT_ID`, `PROJECT_NAME` (status message). No SSH secrets needed
(self-hosted runner). Runner uses the repo-scoped `GITHUB_TOKEN` for the git fetch.

## Watchdog EOD externe (Epic 2 — Observabilité, Sprint 2)

Garde-fou **indépendant de n8n** : alerte sur Telegram si le run EOD officiel de 21:30 UTC
n'a pas eu lieu ou a échoué un jour ouvré — même si n8n (ou tout le service) est mort.

- **`deploy/watchdog_eod.py`** — lancé par un **timer systemd** (`deploy/eod-watchdog.timer`,
  `OnCalendar=Mon..Fri *-*-* 22:15:00 UTC`, ~45 min après le cron 21:30) via
  `deploy/eod-watchdog.service` (`Type=oneshot`, `User=queenp`). Installé et activé par
  `remote.sh` à chaque deploy ; son état est rapporté par `STATUS_WATCHDOG_TIMER` (visible
  dans le message Telegram de déploiement, ligne « watchdog EOD »).
- **Détection** :
  - *Primaire (source de vérité)* — `runtime/market_intelligence/runs.jsonl` : dernier run
    officiel (`dry_run=false`). Pas de run daté d'aujourd'hui → **alerte rouge** (le pipeline
    n'a pas tourné / a échoué avant journalisation). Run présent mais `as_of` périmé/absent →
    **alerte jaune adoucie** (jour férié US probable, faux positif accepté). `as_of` du jour →
    OK.
  - *Secondaire (lecture seule, best-effort)* — `n8n-data/.n8n/database.sqlite`
    (`mode=ro`), table `execution_entity`. Divergence (journal dit « run » mais n8n ne
    rapporte aucune exécution réussie) → **alerte orange**. Toute DB/table/colonne
    manquante ou illisible → statut `unknown`, silencieux (jamais de crash, jamais d'écriture).
- **Envoi** : API bot Telegram directe (`urllib`, stdlib), `TELEGRAM_TOKEN` + `TELEGRAM_CHAT_ID`
  lus depuis `/opt/apps/stock-tracker/.env`. Le watchdog ne dépend donc pas de n8n pour alerter.

**Test manuel sur le VPS** (prouve l'indépendance vis-à-vis de n8n) :

```bash
# En tant que queenp. --check-only évalue sans envoyer.
sudo systemctl stop stock-tracker            # n8n arrêté
python3 /opt/apps/stock-tracker/deploy/watchdog_eod.py --check-only   # doit rendre un verdict
python3 /opt/apps/stock-tracker/deploy/watchdog_eod.py               # envoie l'alerte Telegram
sudo systemctl start stock-tracker

# Déclencher le timer immédiatement (sans attendre 22:15) :
sudo systemctl start eod-watchdog.service
systemctl status eod-watchdog.timer          # doit être active/waiting
journalctl -u eod-watchdog.service --no-pager | tail
```

Le watchdog écrit uniquement sur Telegram (aucune écriture disque/DB). Voir l'ADR
`decisions/2026-07-02_observabilite.md`.

## Mode dry-run du pipeline EOD (Sprint 1 — epic dédup transactionnel)

Le pipeline `python -m market_intelligence.eod_orchestrator` peut être exécuté **sans
jamais muter l'état de déduplication** (`runtime/market_intelligence/dedup_state.json`).
C'est le garde-fou contre l'incident du 2026-07-01 : un run manuel/validation qui latche
un ticker fait disparaître la vraie alerte du run officiel du soir.

Deux leviers, **l'un ou l'autre suffit** (ils se combinent en OR) :

| Levier | Effet |
|--------|-------|
| Flag CLI `--dry-run` | `deduplicate_alerts(readonly=True)` : les survivors sont calculés depuis l'état chargé mais `save_dedup_state` n'est **pas** appelé. Le flock exclusif est quand même pris (cohérence de lecture). |
| Env `ANOMALY_DEDUP_READONLY=1` | Identique à `--dry-run`. Valeurs vraies : `1`, `true`, `yes`, `on` (insensible à la casse). Utile pour forcer le read-only sans toucher la ligne de commande. |
| Flag CLI `--skip-warren` | Saute l'étage S6/S7 (macro + analyse Warren) : produit les survivors **sans appel LLM** (vérif rapide, coût nul). **Exige le dry-run** (`--dry-run` ou l'env) : sans lui, `run_eod_anomaly_pipeline` lève `ValueError` avant tout fetch — commiter l'état sans envoyer de digest latcherait les survivors comme « alertés » sans que rien ne parte (la classe d'incident visée par cet epic). |

Le JSON de sortie porte un champ **`dry_run: true|false`** (vrai dès que le flag **ou**
l'env est actif) pour que n8n et les humains sachent si l'état a été persisté.

```bash
# Vérif rapide sans effet de bord ni coût LLM
python -m market_intelligence.eod_orchestrator --dry-run --skip-warren

# Équivalent via l'environnement (ex. cron de validation)
ANOMALY_DEDUP_READONLY=1 python -m market_intelligence.eod_orchestrator --skip-warren
```

Garantie : `save_dedup_state` est **la seule** écriture d'état persistante de tout le
chemin S0-S7 (macro cache en mémoire, mémoire tickers S7 en lecture seule). En dry-run,
`dedup_state.json` est inchangé octet pour octet (et n'est pas créé s'il n'existe pas).

## Validation deploy sans effet de bord (Sprint 2)

`Execute Trigger (deploy)` (dans `workflow.json`) ne pointe **plus** vers le nœud de
production `Run EOD Anomaly Pipeline S0-S7`. Il exécute désormais un nœud séparé
**`Run EOD Pipeline (deploy dry-run)`** :

```
cd /opt/apps/stock-tracker && python3 -m market_intelligence.eod_orchestrator \
    --history-days 280 --dry-run --skip-warren
```

Ainsi chaque validation de déploiement (le `n8n execute --id` de `remote.sh`) parcourt
toute la chaîne EOD (Parse → If EOD Survivors) **sans muter l'état dedup ni appeler le
LLM**. `--skip-warren` force `should_send=false` → `If EOD Survivors` est toujours faux en
validation → aucun Telegram parti. Le **cron 21:30** (`Layer B EOD Schedule 21h30 UTC`)
reste branché sur le nœud de prod réel (sans `--dry-run`), donc l'alerte du soir persiste
toujours son état normalement.

## Outil admin de l'état dedup + reset post-incident

`python3 -m market_intelligence.dedup_admin` inspecte/répare `dedup_state.json` sans édition
JSON à la main sur le VPS :

```bash
# Voir l'état par ticker (latched/armed, direction, dernière alerte, observations…)
python3 -m market_intelligence.dedup_admin show

# Ré-armer UN ticker pollué (retire son latch, laisse les autres intacts)
python3 -m market_intelligence.dedup_admin reset --ticker NUAI

# Tout ré-armer (état vidé, schéma valide conservé)
python3 -m market_intelligence.dedup_admin reset --all
```

Écritures via `save_dedup_state` (temp+`os.replace` atomique) sous le même flock
(`_state_lock`) que le pipeline. Un fichier corrompu/invalide est **refusé** (exit ≠ 0,
aucune écriture). Chemin résolu au runtime : `--state-path` > env
`ANOMALY_DEDUP_STATE_PATH` > défaut `runtime/market_intelligence/dedup_state.json`.

**Procédure post-incident** — un latch pollué = un ticker marqué « alerté » dans l'état
alors qu'aucun Telegram n'est parti (ex. run manuel avant le patch dry-run, incident
2026-07-01 sur NUAI). Avant le run du soir :

```bash
python3 -m market_intelligence.dedup_admin show                 # localiser le latch fautif
python3 -m market_intelligence.dedup_admin reset --ticker NUAI  # ré-armer
```

Au run 21:30 suivant, l'anomalie refire normalement (mieux vaut un doublon qu'une alerte
perdue). Voir l'ADR `decisions/2026-07-02_dedup-transactionnel.md`.

## Commit d'état post-envoi — two-phase (Sprint 3)

Le run officiel **ne remplace plus** `dedup_state.json` pendant le pipeline. Il calcule
l'état candidat depuis l'état réel et le **stage** dans `dedup_state.pending.json`
(schéma + `run_id` + `as_of`). Le JSON de sortie porte `run_id` + `pending_state_path`.
L'état réel n'est promu **qu'après** confirmation d'envoi Telegram, par une étape séparée.

```
Run EOD (officiel) ──► écrit dedup_state.pending.json (run_id=R)  [réel intact]
       │ JSON: {run_id: R, pending_state_path: …, survivor_count, should_send}
       ▼
If EOD Survivors ── true ──► Split EOD → Send EOD Telegram ──┐
                └─ false ───────────────────────────────────┤
                                                             ▼
                                            Has Run Id (run_id non-null ?)
                                              │ oui                │ non
                                              ▼                    ▼
                                     Commit Dedup State         (skip)
                        dedup_admin commit --run-id R  (os.replace atomique)
                        promeut pending → dedup_state.json, supprime le pending
```

**Nœud `Has Run Id`** : garde placé avant `Commit Dedup State` sur les deux chemins. La
validation deploy (dry-run) traverse `If EOD Survivors` (branche false) avec `run_id=null` ;
le garde bloque alors le commit (`run_id` vide → skip) au lieu de faire échouer la validation.

**Préfixe `=` obligatoire** : la commande du nœud `Commit Dedup State` commence par `=` pour
que n8n **évalue** l'expression `{{ $('Parse EOD Anomaly Result').item.json.run_id }}` (sans
le `=`, les accolades partiraient littéralement au shell et le commit échouerait chaque
soir). Un test wiring vérifie que toute commande `executeCommand` contenant `{{` est préfixée.

- **Échec d'envoi Telegram** → le nœud `Commit Dedup State` n'est jamais atteint → le pending
  n'est pas promu → l'état réel n'avance pas → **l'alerte refire au run suivant**.
- **Sans survivor** (branche false) → commit immédiat : `last_observed_as_of` /
  `latch_observations` avancent quand même (pas d'alerte à envoyer, mais l'état progresse).
- **`dedup_admin commit --run-id R`** refuse (`rc 1`, aucune écriture) si le pending porte un
  autre `run_id` (pending écrasé par un run plus récent) ; **no-op `rc 0`** si aucun pending
  (idempotent : multi-chunk Telegram, ou branche deploy dry-run qui n'écrit jamais de
  pending). Le `run_id` du nœud vient de `{{ $('Parse EOD Anomaly Result').item.json.run_id }}`.
- **Sortie Telegram EOD dédiée** (`Send EOD Telegram`, credentials clonés) : le commit est
  **EOD-exclusif**. Le macro-brief 16h garde la chaîne partagée `Aggregate → Split → Send
  Telegram` et ne déclenche jamais de commit.
- **Pending orphelin** (crash entre calcul et commit) : le run suivant recalcule **toujours
  depuis l'état réel** (l'orphelin n'est jamais lu), l'écrase et logge un warning.
**`NODES_EXCLUDE=[]` (REQUIS dans `.env`)** — n8n 2.20+ exclut par défaut le nœud
`n8n-nodes-base.executeCommand`. Le workflow l'utilise (« Run EOD Anomaly Pipeline S0-S7 »
lance `python3 -m market_intelligence.eod_orchestrator`). Sans cet override, n8n throw
`Unrecognized node type: executeCommand` à l'activation et **n'arme aucun trigger** (le
workflow entier reste inactif). `NODES_EXCLUDE=[]` retire l'exclusion. ⚠️ Autorise
l'exécution de commandes shell par les workflows n8n (user `warren`) — voulu ici. Cf. PM-0001.

**Publication de version (n8n 2.20)** — n8n exécute la **version publiée** d'un workflow
(`workflow_entity.activeVersionId` → `workflow_history`, via `workflow_published_version`),
pas `workflow_entity.nodes`. `import_workflow.py` crée donc une nouvelle version publiée et
repointe `activeVersionId` / `workflow_published_version` / `workflow_publish_history` à
chaque import, sinon n8n continuerait de lancer l'ancienne version publiée (le draft
importé serait ignoré). Une assertion post-import vérifie que la version publiée reflète
`workflow.json`. Cf. PM-0001.
