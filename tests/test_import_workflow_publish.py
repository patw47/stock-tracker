"""Guard for PM-0001: the deploy import must PUBLISH the workflow version.

n8n 2.20 runs a workflow's published version (workflow_history via
workflow_published_version / activeVersionId), not workflow_entity.nodes. Writing
only the draft leaves n8n running the old published version. import_workflow.py must
create a fresh published version reflecting workflow.json.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "deploy" / "import_workflow.py"

_SCHEMA = """
CREATE TABLE workflow_entity (
    id varchar PRIMARY KEY, name varchar, active integer,
    nodes text, connections text, settings text,
    createdAt text, updatedAt text, triggerCount integer,
    versionId varchar, activeVersionId varchar, isArchived integer DEFAULT 0
);
CREATE TABLE workflow_history (
    versionId varchar PRIMARY KEY, workflowId varchar, authors varchar NOT NULL,
    createdAt datetime DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f','NOW')),
    updatedAt datetime DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f','NOW')),
    nodes text NOT NULL, connections text NOT NULL, name varchar,
    autosaved boolean DEFAULT (0), description text
);
CREATE TABLE workflow_published_version (
    workflowId varchar PRIMARY KEY, publishedVersionId varchar,
    createdAt datetime DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f','NOW')),
    updatedAt datetime DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f','NOW'))
);
CREATE TABLE workflow_publish_history (
    id integer PRIMARY KEY, workflowId varchar, versionId varchar,
    event varchar NOT NULL, userId varchar,
    createdAt datetime DEFAULT (STRFTIME('%Y-%m-%d %H:%M:%f','NOW'))
);
"""


def _make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    con.commit()
    con.close()


def _workflow(wid: str = "wf1") -> dict:
    return {
        "id": wid,
        "name": "Test WF",
        "nodes": [
            {"id": "a", "name": "S1", "type": "n8n-nodes-base.scheduleTrigger", "parameters": {}},
            {"id": "b", "name": "S2", "type": "n8n-nodes-base.scheduleTrigger", "parameters": {}},
            {"id": "c", "name": "S3", "type": "n8n-nodes-base.scheduleTrigger", "parameters": {}},
            {"id": "d", "name": "Code", "type": "n8n-nodes-base.code", "parameters": {}},
        ],
        "connections": {},
        "settings": {"executionOrder": "v1", "timezone": "UTC"},
    }


def _run(db: Path, wf_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "N8N_DB": str(db), "WORKFLOW_JSON": str(wf_path)}
    return subprocess.run(
        [sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True
    )


def test_import_publishes_version_with_all_triggers(tmp_path: Path) -> None:
    db = tmp_path / "database.sqlite"
    _make_db(db)
    wf_path = tmp_path / "workflow.json"
    wf_path.write_text(json.dumps(_workflow()), encoding="utf-8")

    result = _run(db, wf_path)
    assert result.returncode == 0, result.stderr

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    entity = cur.execute("SELECT * FROM workflow_entity WHERE id='wf1'").fetchone()
    assert entity["active"] == 1
    assert entity["triggerCount"] == 3  # 3 schedule triggers; the Code node excluded
    active_vid = entity["activeVersionId"]
    assert active_vid

    published = cur.execute(
        "SELECT publishedVersionId FROM workflow_published_version WHERE workflowId='wf1'"
    ).fetchone()
    assert published["publishedVersionId"] == active_vid

    history = cur.execute(
        "SELECT nodes FROM workflow_history WHERE versionId=?", (active_vid,)
    ).fetchone()
    assert history is not None
    published_nodes = json.loads(history["nodes"])
    assert len(published_nodes) == 4
    triggers = [n for n in published_nodes if "Trigger" in n["type"] or "schedule" in n["type"].lower()]
    assert len(triggers) == 3

    event = cur.execute(
        "SELECT event FROM workflow_publish_history WHERE workflowId='wf1' ORDER BY id DESC"
    ).fetchone()
    assert event["event"] == "activated"
    con.close()


def test_reimport_repoints_to_new_published_version(tmp_path: Path) -> None:
    db = tmp_path / "database.sqlite"
    _make_db(db)
    wf_path = tmp_path / "workflow.json"
    wf_path.write_text(json.dumps(_workflow()), encoding="utf-8")

    assert _run(db, wf_path).returncode == 0
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    first = con.execute(
        "SELECT activeVersionId FROM workflow_entity WHERE id='wf1'"
    ).fetchone()["activeVersionId"]
    con.close()

    assert _run(db, wf_path).returncode == 0
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    second = con.execute(
        "SELECT activeVersionId FROM workflow_entity WHERE id='wf1'"
    ).fetchone()["activeVersionId"]
    published = con.execute(
        "SELECT publishedVersionId FROM workflow_published_version WHERE workflowId='wf1'"
    ).fetchone()["publishedVersionId"]
    con.close()

    assert second != first  # a brand new published version
    assert published == second  # pointer follows the newest version
