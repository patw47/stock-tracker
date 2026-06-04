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

**Python deps** — `warren_server.py` / `agents/warren` need `pydantic` + `requests`
(`requirements.txt`); `remote.sh` pip-installs them into the system python (the one
`warren-server.service` runs) before restarting.

**Required GitHub secrets** — `TELEGRAM_ORCHESTRATION_BOT_TOKEN`,
`TELEGRAM_ORCHESTRATION_CHAT_ID`, `PROJECT_NAME` (status message). No SSH secrets needed
(self-hosted runner). Runner uses the repo-scoped `GITHUB_TOKEN` for the git fetch.
