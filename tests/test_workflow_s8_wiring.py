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


def test_legacy_warren_news_layer_remains_connected() -> None:
    workflow = _workflow()

    assert _edge_targets(workflow, "Layer A News Schedule 16h Mon-Fri") == [
        "Read Tickers"
    ]
    assert _edge_targets(workflow, "Read Tickers") == ["Prepare Haiku Request"]
    assert _edge_targets(workflow, "Claude Haiku API") == ["Extract Raw News"]
    assert _edge_targets(workflow, "Call Warren Filter") == ["Extract Filter Result"]
    assert _edge_targets(workflow, "If New Items", output_index=0) == ["Call Memorize"]
    assert _edge_targets(workflow, "If New Items", output_index=1) == []
    assert _edge_targets(workflow, "Split for Telegram") == ["Send Telegram"]


def test_layer_a_synthesis_nodes_removed() -> None:
    """Sprint 2: synthesis chain and Layer A → Telegram link must not exist."""
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    assert "Prepare Warren Synthesis" not in nodes
    assert "Call Warren Synthesis" not in nodes
    assert "Extract Warren Synthesis" not in nodes
    assert "Call Memorize" in nodes

    connections = workflow["connections"]
    assert "Prepare Warren Synthesis" not in connections
    assert "Call Warren Synthesis" not in connections
    assert "Extract Warren Synthesis" not in connections
    assert "/synthesize" not in json.dumps(connections)


def test_layer_a_does_not_reach_telegram() -> None:
    """Layer A true branch (If New Items) must not reach Telegram nodes."""
    workflow = _workflow()
    layer_a_true_reachable = _reachable(workflow, "If New Items")
    assert "Aggregate for Telegram" not in layer_a_true_reachable
    assert "Split for Telegram" not in layer_a_true_reachable
    assert "Send Telegram" not in layer_a_true_reachable


def test_call_memorize_targets_correct_endpoint() -> None:
    """Call Memorize node POSTs to /memorize."""
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)
    node = nodes["Call Memorize"]
    assert node["type"] == "n8n-nodes-base.httpRequest"
    assert "/memorize" in node["parameters"]["url"]


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
    assert _edge_targets(workflow, "If EOD Survivors", output_index=1) == []
    assert _edge_targets(workflow, "Prepare EOD Digest for Telegram") == [
        "Aggregate for Telegram"
    ]
    assert _edge_targets(workflow, "Aggregate for Telegram") == ["Split for Telegram"]


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


def test_deploy_trigger_exercises_news_and_eod_branches() -> None:
    workflow = _workflow()

    assert _edge_targets(workflow, "Execute Trigger (deploy)") == [
        "Read Tickers",
        "Run EOD Anomaly Pipeline S0-S7",
    ]


def test_macro_brief_schedule_wired_to_telegram() -> None:
    """Macro Brief schedule triggers independently and flows to Telegram."""
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    schedule = nodes["Macro Brief Schedule 16h Mon-Fri"]
    # 14:00 UTC = 16h Paris (été), sous settings.timezone=UTC.
    assert schedule["parameters"]["rule"]["interval"][0]["expression"] == "0 14 * * 1-5"
    assert "timezone" not in schedule["parameters"]

    assert _edge_targets(workflow, "Macro Brief Schedule 16h Mon-Fri") == [
        "Call Warren Macro Brief"
    ]
    assert _edge_targets(workflow, "Call Warren Macro Brief") == ["Extract Macro Brief"]
    assert _edge_targets(workflow, "Extract Macro Brief") == ["Aggregate for Telegram"]

    # Macro brief path must NOT contain Layer A news nodes (independent pipeline)
    reachable = _reachable(workflow, "Macro Brief Schedule 16h Mon-Fri")
    assert "Read Tickers" not in reachable
    assert "Claude Haiku API" not in reachable
    assert "If New Items" not in reachable

    # Must reach Telegram
    assert "Aggregate for Telegram" in reachable
    assert "Split for Telegram" in reachable
    assert "Send Telegram" in reachable


def test_macro_brief_call_targets_correct_endpoint() -> None:
    """Call Warren Macro Brief node POSTs to /macro-brief."""
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    call_node = nodes["Call Warren Macro Brief"]
    assert call_node["type"] == "n8n-nodes-base.httpRequest"
    assert "/macro-brief" in call_node["parameters"]["url"]
