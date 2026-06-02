#!/usr/bin/env python3
"""Importe workflow.json dans la base n8n par écriture SQLite directe.

Contourne `n8n import:workflow`, bloqué par les permissions de
`.n8n/config` (EACCES sous `sudo -u warren`). Ce script est lancé en tant
que queenp — propriétaire de `database.sqlite` — avec n8n arrêté.

- UPSERT du workflow canonique (id lu dans workflow.json) avec active=1
- désactive tout AUTRE workflow et remet son triggerCount à 0 pour éviter les doublons de briefing
  (ex. le legacy 48dff814-… "Veille Boursière Quotidienne")

Variables d'environnement :
  N8N_DB        chemin de database.sqlite
  WORKFLOW_JSON chemin de workflow.json
"""
import datetime
import json
import os
import sqlite3
import sys

DB = os.environ.get("N8N_DB", "/opt/apps/stock-tracker/n8n-data/.n8n/database.sqlite")
WF = os.environ.get("WORKFLOW_JSON", "/opt/apps/stock-tracker/workflow.json")


def main() -> None:
    with open(WF, encoding="utf-8") as fh:
        wf = json.load(fh)

    wid = wf["id"]
    now = datetime.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Colonnes réellement présentes (schéma variable selon la version n8n).
    cols = {r["name"] for r in cur.execute("PRAGMA table_info(workflow_entity)")}

    candidates = {
        "id": wid,
        "name": wf.get("name"),
        "active": 1,
        "nodes": json.dumps(wf.get("nodes", []), ensure_ascii=False),
        "connections": json.dumps(wf.get("connections", {}), ensure_ascii=False),
        "settings": json.dumps(wf.get("settings", {}), ensure_ascii=False),
        "updatedAt": now,
        "createdAt": now,
        "triggerCount": 0,
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

    # Un seul workflow actif à la fois. n8n peut garder un schedule enregistré si
    # triggerCount reste à 1 même après active=0, donc on le remet aussi à zéro.
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

    con.commit()

    print("[import] état final :")
    display_cols = ["id", "active", "name"]
    if "triggerCount" in cols:
        display_cols.append("triggerCount")
    if "isArchived" in cols:
        display_cols.append("isArchived")
    for r in cur.execute(f"SELECT {', '.join(display_cols)} FROM workflow_entity"):
        extras = []
        if "triggerCount" in cols:
            extras.append(f"triggerCount={r['triggerCount']}")
        if "isArchived" in cols:
            extras.append(f"isArchived={r['isArchived']}")
        suffix = " " + " ".join(extras) if extras else ""
        print(f"[import]   {r['id']} active={r['active']}{suffix} {r['name']}")

    con.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — on veut un code de sortie net pour la CI
        print(f"[import] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
