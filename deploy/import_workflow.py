#!/usr/bin/env python3
"""Importe workflow.json dans la base n8n par écriture SQLite directe.

Contourne `n8n import:workflow`, bloqué par les permissions de
`.n8n/config` (EACCES sous `sudo -u warren`). Ce script est lancé en tant
que queenp — propriétaire de `database.sqlite` — avec n8n arrêté.

- UPSERT du workflow canonique (id lu dans workflow.json) avec active=1
- **publie** la version : n8n 2.20 sépare le brouillon (workflow_entity.nodes) de
  la version publiée effectivement exécutée (workflow_entity.activeVersionId ->
  workflow_history.nodes, pointée par workflow_published_version). Écrire uniquement
  workflow_entity.nodes ne suffit PAS : n8n continuerait de lancer l'ancienne version
  publiée. On crée donc une nouvelle version workflow_history et on repointe
  activeVersionId / workflow_published_version / workflow_publish_history.
- désactive tout AUTRE workflow (active=0, triggerCount=0, dépublié) pour éviter les
  doublons de briefing (ex. le legacy 48dff814-… "Veille Boursière Quotidienne")

Variables d'environnement :
  N8N_DB        chemin de database.sqlite
  WORKFLOW_JSON chemin de workflow.json
"""
import datetime
import json
import os
import sqlite3
import sys
import uuid

DB = os.environ.get("N8N_DB", "/opt/apps/stock-tracker/n8n-data/.n8n/database.sqlite")
WF = os.environ.get("WORKFLOW_JSON", "/opt/apps/stock-tracker/workflow.json")


def _table_exists(cur: sqlite3.Cursor, name: str) -> bool:
    return (
        cur.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    return {r["name"] for r in cur.execute(f"PRAGMA table_info({table})")}


def _trigger_count(nodes: list[dict]) -> int:
    """Compte les nœuds trigger (schedule, webhook, executeWorkflowTrigger, …).

    Exclut le Manual Trigger. Sert au badge triggerCount de workflow_entity.
    """
    count = 0
    for node in nodes:
        node_type = str(node.get("type", "")).lower()
        if "trigger" in node_type and "manualtrigger" not in node_type:
            count += 1
    return count


def _publish_version(
    cur: sqlite3.Cursor,
    wid: str,
    wf: dict,
    nodes_json: str,
    connections_json: str,
) -> str | None:
    """Crée une version publiée et repointe les tables de publication n8n 2.20.

    Retourne le versionId publié, ou None si l'instance n8n est antérieure au
    versioning (aucune table workflow_history) — l'ancien comportement suffit alors.
    """
    we_cols = _columns(cur, "workflow_entity")
    if not _table_exists(cur, "workflow_history") or "activeVersionId" not in we_cols:
        return None

    new_vid = str(uuid.uuid4())
    hist_cols = _columns(cur, "workflow_history")
    row = {
        "versionId": new_vid,
        "workflowId": wid,
        "authors": "deploy-import",
        "nodes": nodes_json,
        "connections": connections_json,
        "name": wf.get("name"),
        "autosaved": 0,
    }
    # createdAt / updatedAt : on laisse le DEFAULT SQLite (format datetime natif n8n).
    row = {k: v for k, v in row.items() if k in hist_cols}
    columns = ", ".join(f'"{k}"' for k in row)
    placeholders = ", ".join("?" for _ in row)
    cur.execute(
        f"INSERT INTO workflow_history ({columns}) VALUES ({placeholders})",
        list(row.values()),
    )

    sets = ['"activeVersionId"=?']
    vals: list[object] = [new_vid]
    if "versionId" in we_cols:
        sets.append('"versionId"=?')
        vals.append(new_vid)
    cur.execute(
        f"UPDATE workflow_entity SET {', '.join(sets)} WHERE id=?", vals + [wid]
    )

    if _table_exists(cur, "workflow_published_version"):
        cur.execute("DELETE FROM workflow_published_version WHERE workflowId=?", (wid,))
        cur.execute(
            'INSERT INTO workflow_published_version ("workflowId","publishedVersionId") '
            "VALUES (?,?)",
            (wid, new_vid),
        )

    if _table_exists(cur, "workflow_publish_history"):
        cur.execute(
            'INSERT INTO workflow_publish_history ("workflowId","versionId","event") '
            "VALUES (?,?,?)",
            (wid, new_vid, "activated"),
        )

    return new_vid


def main() -> None:
    with open(WF, encoding="utf-8") as fh:
        wf = json.load(fh)

    wid = wf["id"]
    nodes = wf.get("nodes", [])
    now = datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
    nodes_json = json.dumps(nodes, ensure_ascii=False)
    connections_json = json.dumps(wf.get("connections", {}), ensure_ascii=False)
    trigger_count = _trigger_count(nodes)

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Colonnes réellement présentes (schéma variable selon la version n8n).
    cols = _columns(cur, "workflow_entity")

    candidates = {
        "id": wid,
        "name": wf.get("name"),
        "active": 1,
        "nodes": nodes_json,
        "connections": connections_json,
        "settings": json.dumps(wf.get("settings", {}), ensure_ascii=False),
        "updatedAt": now,
        "createdAt": now,
        "triggerCount": trigger_count,
    }
    vals = {k: v for k, v in candidates.items() if k in cols}

    exists = cur.execute(
        "SELECT 1 FROM workflow_entity WHERE id=?", (wid,)
    ).fetchone()

    if exists:
        cols_to_set = [k for k in vals if k not in ("id", "createdAt")]
        assignments = ", ".join(f'"{k}"=?' for k in cols_to_set)
        cur.execute(
            f"UPDATE workflow_entity SET {assignments} WHERE id=?",
            [vals[k] for k in cols_to_set] + [wid],
        )
        print(f"[import] updated workflow {wid}")
    else:
        keys = list(vals)
        column_list = ", ".join(f'"{k}"' for k in keys)
        placeholders = ", ".join("?" for _ in keys)
        cur.execute(
            f"INSERT INTO workflow_entity ({column_list}) VALUES ({placeholders})",
            [vals[k] for k in keys],
        )
        print(f"[import] inserted workflow {wid}")

    # PUBLICATION — indispensable sous n8n 2.20 : sinon n8n exécute l'ancienne
    # version publiée (draft non publié = jamais lancé).
    published_vid = _publish_version(cur, wid, wf, nodes_json, connections_json)
    if published_vid:
        print(
            f"[import] published version {published_vid} "
            f"({trigger_count} trigger node(s))"
        )
    else:
        print("[import] versioning absent (n8n < publish model) — skip publish")

    # Un seul workflow actif à la fois. Désactive + dépublie tout autre workflow.
    if "active" in cols:
        set_parts = ["active=0"]
        if "triggerCount" in cols:
            set_parts.append('"triggerCount"=0')
        if "isArchived" in cols:
            set_parts.append('"isArchived"=1')
        cur.execute(
            f"UPDATE workflow_entity SET {', '.join(set_parts)} WHERE id<>?",
            (wid,),
        )
        if cur.rowcount:
            print(f"[import] disabled {cur.rowcount} other workflow(s)")
        if _table_exists(cur, "workflow_published_version"):
            cur.execute(
                "DELETE FROM workflow_published_version WHERE workflowId<>?", (wid,)
            )

    con.commit()

    # ASSERTION de non-régression : la version publiée DOIT refléter workflow.json,
    # sinon n8n lancerait un autre jeu de triggers. Échec net pour la CI.
    if published_vid:
        active_vid = cur.execute(
            "SELECT activeVersionId FROM workflow_entity WHERE id=?", (wid,)
        ).fetchone()[0]
        published_nodes = cur.execute(
            "SELECT nodes FROM workflow_history WHERE versionId=?", (active_vid,)
        ).fetchone()
        published_trigger_count = (
            _trigger_count(json.loads(published_nodes[0])) if published_nodes else -1
        )
        if active_vid != published_vid:
            con.close()
            raise AssertionError(
                f"activeVersionId={active_vid} != published={published_vid}"
            )
        if published_trigger_count != trigger_count:
            con.close()
            raise AssertionError(
                f"published triggerCount={published_trigger_count} "
                f"!= workflow.json {trigger_count}"
            )

    print("[import] état final :")
    display_cols = ["id", "active", "name"]
    if "triggerCount" in cols:
        display_cols.append("triggerCount")
    if "activeVersionId" in cols:
        display_cols.append("activeVersionId")
    for r in cur.execute(f"SELECT {', '.join(display_cols)} FROM workflow_entity"):
        extras = []
        if "triggerCount" in cols:
            extras.append(f"triggerCount={r['triggerCount']}")
        if "activeVersionId" in cols:
            extras.append(f"activeVersionId={r['activeVersionId']}")
        suffix = " " + " ".join(extras) if extras else ""
        print(f"[import]   {r['id']} active={r['active']}{suffix} {r['name']}")

    con.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — on veut un code de sortie net pour la CI
        print(f"[import] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
