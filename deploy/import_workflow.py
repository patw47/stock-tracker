#!/usr/bin/env python3
"""Importe workflow.json dans la base n8n par écriture SQLite directe.

Contourne `n8n import:workflow`, bloqué par les permissions de
`.n8n/config` (EACCES sous `sudo -u warren`). Ce script est lancé en tant
que queenp — propriétaire de `database.sqlite` — avec n8n arrêté.

- UPSERT du workflow canonique (id lu dans workflow.json) avec active=1
- désactive tout AUTRE workflow actif pour éviter les doublons de briefing
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

    # Un seul workflow actif à la fois.
    if "active" in cols:
        cur.execute(
            "UPDATE workflow_entity SET active=0 WHERE id<>? AND active=1", (wid,)
        )
        if cur.rowcount:
            print(f"[import] deactivated {cur.rowcount} other active workflow(s)")

    con.commit()

    print("[import] état final :")
    for r in cur.execute("SELECT id, active, name FROM workflow_entity"):
        print(f"[import]   {r['id']} active={r['active']} {r['name']}")

    con.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — on veut un code de sortie net pour la CI
        print(f"[import] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
