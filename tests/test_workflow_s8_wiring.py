from __future__ import annotations

import json
from pathlib import Path

WORKFLOW_PATH = Path(__file__).parent.parent / "workflow.json"


def _workflow() -> dict:
    return json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _nodes_by_name(workflow: dict) -> dict[str, dict]:
    return {node["name"]: node for node in workflow["nodes"]}


def _edge_targets(workflow: dict, source: str, output_index: int = 0) -> list[str]:
    connection = workflow["connections"].get(source, {})
    outputs = connection.get("main", [])
    if output_index >= len(outputs):
        return []
    return [edge["node"] for edge in outputs[output_index]]


def _reachable(workflow: dict, source: str) -> set[str]:
    seen: set[str] = set()
    stack = [source]
    while stack:
        node = stack.pop()
        for target in _edge_targets(workflow, node):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


LAYER_A_NODES = [
    "Layer A News Schedule 14:00 UTC Mon-Fri",
    "Read Tickers",
    "Prepare Haiku Request",
    "Claude Haiku API",
    "Extract Raw News",
    "Aggregate All News",
    "Call Warren Filter",
    "Extract Filter Result",
    "If New Items",
    "Call Memorize",
]


def test_layer_a_nodes_fully_removed() -> None:
    """Epic 6 S1: the whole Layer A news chain is gone from nodes and wiring."""
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)
    connections = workflow["connections"]
    serialized = json.dumps(connections)
    for name in LAYER_A_NODES:
        assert name not in nodes, f"{name} still present as a node"
        assert name not in connections, f"{name} still a connection source"
        assert name not in serialized, f"{name} still referenced as a target"


def test_layer_a_synthesis_nodes_removed() -> None:
    """Synthesis chain and the Layer A memorize node must not exist."""
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    assert "Prepare Warren Synthesis" not in nodes
    assert "Call Warren Synthesis" not in nodes
    assert "Extract Warren Synthesis" not in nodes
    assert "Call Memorize" not in nodes

    connections = workflow["connections"]
    assert "Prepare Warren Synthesis" not in connections
    assert "Call Warren Synthesis" not in connections
    assert "Extract Warren Synthesis" not in connections
    assert "/synthesize" not in json.dumps(connections)


def test_eod_branch_uses_registry_runner_and_reuses_telegram_nodes() -> None:
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    schedule = nodes["Layer B EOD Schedule 21h30 UTC"]
    assert schedule["parameters"]["rule"]["interval"][0]["expression"] == (
        "30 21 * * 1-5"
    )
    # Le param timezone par-nœud est inerte dans ScheduleTrigger : c'est
    # settings.timezone (UTC) au niveau workflow qui gouverne. EOD doit partir 21:30 UTC.
    assert "timezone" not in schedule["parameters"]
    assert workflow["settings"]["timezone"] == "UTC"
    assert _edge_targets(workflow, "Layer B EOD Schedule 21h30 UTC") == [
        "Run EOD Anomaly Pipeline S0-S7"
    ]

    runner = nodes["Run EOD Anomaly Pipeline S0-S7"]
    command = runner["parameters"]["command"]
    assert "python3 -m market_intelligence.eod_orchestrator" in command
    assert "--history-days 280" in command
    assert "portfolio.json" not in command
    assert "watchlist.json" not in command

    assert _edge_targets(workflow, "Run EOD Anomaly Pipeline S0-S7") == [
        "Parse EOD Anomaly Result"
    ]
    assert _edge_targets(workflow, "Parse EOD Anomaly Result") == ["If EOD Survivors"]
    assert _edge_targets(workflow, "If EOD Survivors", output_index=0) == [
        "Prepare EOD Digest for Telegram"
    ]
    # Sprint 3 two-phase: EOD uses a dedicated Telegram output ending in commit;
    # the false branch commits too, both gated by the run_id guard node.
    assert _edge_targets(workflow, "If EOD Survivors", output_index=1) == [
        "Has Run Id"
    ]
    assert _edge_targets(workflow, "Prepare EOD Digest for Telegram") == [
        "Split EOD for Telegram"
    ]
    assert _edge_targets(workflow, "Aggregate for Telegram") == ["Split for Telegram"]


def test_two_phase_commit_node_wiring() -> None:
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    commit = nodes["Commit Dedup State"]
    assert commit["type"] == "n8n-nodes-base.executeCommand"
    command = commit["parameters"]["command"]
    assert "dedup_admin commit" in command
    assert "--run-id" in command
    assert "Parse EOD Anomaly Result" in command  # run_id sourced from parse node
    assert ".run_id" in command

    # EOD send path reaches commit only after the dedicated EOD Telegram send,
    # via the run_id guard node.
    assert _edge_targets(workflow, "Split EOD for Telegram") == ["Send EOD Telegram"]
    assert _edge_targets(workflow, "Send EOD Telegram") == ["Has Run Id"]
    assert "Commit Dedup State" in _reachable(workflow, "If EOD Survivors")


def test_executecommand_expressions_are_prefixed_with_equals() -> None:
    """Any executeCommand carrying an {{ }} expression MUST start with '=',
    else n8n passes the literal braces to the shell (commit would never run)."""
    workflow = _workflow()
    for node in workflow["nodes"]:
        if node["type"] != "n8n-nodes-base.executeCommand":
            continue
        command = node["parameters"]["command"]
        if "{{" in command:
            assert command.startswith("="), (
                f"{node['name']} has an expression but no '=' prefix"
            )


def test_run_id_guard_before_commit_on_both_branches() -> None:
    """run_id-null runs (dry-run / deploy validation) must not reach Commit."""
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    guard = nodes["Has Run Id"]
    assert guard["type"] == "n8n-nodes-base.if"
    payload = json.dumps(guard["parameters"])
    assert ".run_id" in payload  # guard tests the run_id
    assert "notEmpty" in payload

    # Both paths funnel through the guard; only its true branch reaches commit.
    assert _edge_targets(workflow, "Send EOD Telegram") == ["Has Run Id"]
    assert _edge_targets(workflow, "If EOD Survivors", output_index=1) == ["Has Run Id"]
    assert _edge_targets(workflow, "Has Run Id", output_index=0) == ["Commit Dedup State"]
    assert _edge_targets(workflow, "Has Run Id", output_index=1) == []


def test_commit_not_reachable_from_shared_telegram() -> None:
    """Commit must be EOD-exclusive: the shared Telegram chain must not trigger it."""
    workflow = _workflow()

    # Shared Telegram chain does not reach commit.
    assert "Commit Dedup State" not in _reachable(workflow, "Aggregate for Telegram")
    # EOD no longer flows into the shared aggregate node.
    assert "Aggregate for Telegram" not in _reachable(workflow, "If EOD Survivors")


def test_eod_send_path_is_dedicated_not_shared() -> None:
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    send_eod = nodes["Send EOD Telegram"]
    assert send_eod["type"] == "n8n-nodes-base.telegram"
    # Dedicated EOD Telegram carries its own credentials (cloned from shared send).
    assert "credentials" in send_eod
    # Shared Send Telegram is fed only by the heartbeat / monthly chains.
    assert "Send Telegram" not in _reachable(workflow, "If EOD Survivors")


def test_eod_branch_has_no_llm_or_warren_before_survivor_gate() -> None:
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    pre_gate = _reachable(workflow, "Layer B EOD Schedule 21h30 UTC")
    assert "If EOD Survivors" in pre_gate
    pre_gate.remove("If EOD Survivors")
    pre_gate -= _reachable(workflow, "If EOD Survivors")

    for node_name in pre_gate:
        node = nodes[node_name]
        payload = json.dumps(node, sort_keys=True)
        assert "anthropic.com" not in payload
        assert "Claude" not in node_name
        assert "Warren" not in node_name
        assert "/filter" not in payload
        assert "/synthesize" not in payload


def test_deploy_trigger_exercises_eod_branch() -> None:
    """Layer A removed: the deploy trigger now only exercises the EOD dry-run."""
    workflow = _workflow()

    assert _edge_targets(workflow, "Execute Trigger (deploy)") == [
        "Run EOD Pipeline (deploy dry-run)"
    ]


def test_deploy_trigger_no_longer_points_at_prod_eod_node() -> None:
    """Deploy validation must not touch the real (state-mutating) EOD node."""
    workflow = _workflow()
    assert "Run EOD Anomaly Pipeline S0-S7" not in _edge_targets(
        workflow, "Execute Trigger (deploy)"
    )


def test_deploy_dry_run_node_uses_dry_run_and_skip_warren() -> None:
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    node = nodes["Run EOD Pipeline (deploy dry-run)"]
    assert node["type"] == "n8n-nodes-base.executeCommand"
    command = node["parameters"]["command"]
    assert "python3 -m market_intelligence.eod_orchestrator" in command
    assert "--dry-run" in command
    assert "--skip-warren" in command

    # Still walks the shared EOD chain (parse/gate), just without side effects.
    assert _edge_targets(workflow, "Run EOD Pipeline (deploy dry-run)") == [
        "Parse EOD Anomaly Result"
    ]


def test_prod_eod_node_is_not_dry_run() -> None:
    """The 21:30 cron path must still persist state (no dry-run flags)."""
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    command = nodes["Run EOD Anomaly Pipeline S0-S7"]["parameters"]["command"]
    assert "--dry-run" not in command
    assert "--skip-warren" not in command
    assert "--history-days 280" in command

    assert _edge_targets(workflow, "Layer B EOD Schedule 21h30 UTC") == [
        "Run EOD Anomaly Pipeline S0-S7"
    ]


def test_schedule_labels_use_utc_not_16h() -> None:
    """Epic 3 S1: crons are UTC (0 14 = 14:00 UTC), labels must not say 16h."""
    workflow = _workflow()

    assert all("16h" not in node["name"] for node in workflow["nodes"])


def test_send_telegram_nodes_use_html_parse_mode() -> None:
    """Escaped HTML payload requires parse_mode HTML on every Telegram send."""
    workflow = _workflow()
    for node in workflow["nodes"]:
        if node["type"] == "n8n-nodes-base.telegram":
            assert node["parameters"]["additionalFields"]["parse_mode"] == "HTML"


def test_weekly_heartbeat_friday_wired_to_shared_telegram() -> None:
    """Epic 2 S3: Friday 21:50 UTC → run heartbeat → feed the shared Telegram chain."""
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    schedule = nodes["Weekly Heartbeat Schedule 21h50 Vendredi"]
    assert schedule["type"] == "n8n-nodes-base.scheduleTrigger"
    assert (
        schedule["parameters"]["rule"]["interval"][0]["expression"] == "50 21 * * 5"
    )
    assert workflow["settings"]["timezone"] == "UTC"

    runner = nodes["Run Weekly Heartbeat"]
    assert runner["type"] == "n8n-nodes-base.executeCommand"
    assert "market_intelligence.weekly_heartbeat" in runner["parameters"]["command"]
    # No n8n expression in the command => no '=' prefix trap.
    assert "{{" not in runner["parameters"]["command"]

    assert _edge_targets(workflow, "Weekly Heartbeat Schedule 21h50 Vendredi") == [
        "Run Weekly Heartbeat"
    ]
    assert _edge_targets(workflow, "Run Weekly Heartbeat") == [
        "Prepare Heartbeat for Telegram"
    ]
    assert _edge_targets(workflow, "Prepare Heartbeat for Telegram") == [
        "Aggregate for Telegram"
    ]
    # Reuses the shared Telegram chain and stays clear of the EOD commit path.
    reachable = _reachable(workflow, "Weekly Heartbeat Schedule 21h50 Vendredi")
    assert "Send Telegram" in reachable
    assert "Commit Dedup State" not in reachable


