"""Epic 3 Sprint 2 — Bridge Warren multi-thread + retry envoi.

Covers the sprint acceptance criteria:
  - two concurrent bridge requests are served in parallel (not serialized);
  - concurrent memory writes of the same ticker never interleave the file;
  - Send Telegram nodes are configured with retry and stay fail-visible;
  - the bridge HTTP nodes carry explicit timeouts >= 240 s.
"""
from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import warren_server  # noqa: E402

WORKFLOW_PATH = REPO_ROOT / "workflow.json"

SLOW = 0.5  # seconds a mocked Warren call blocks the handler


def _workflow() -> dict:
    return json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _nodes_by_name(workflow: dict) -> dict[str, dict]:
    return {node["name"]: node for node in workflow["nodes"]}


# --------------------------------------------------------------------------
# Criterion 1 — two concurrent requests served in parallel
# --------------------------------------------------------------------------
def test_two_requests_served_in_parallel(monkeypatch):
    """Two /filter calls hitting a slow Warren must overlap, not serialize.

    The real Handler runs on ThreadingHTTPServer; with a mocked slow Warren
    call, two concurrent requests should finish in ~SLOW, not ~2*SLOW.
    """
    # Force the /filter dedup branch (needs non-empty memory) then block in Warren.
    monkeypatch.setattr(warren_server, "read_memory", lambda sym: "prior memory entry")
    monkeypatch.setattr(warren_server, "build_warren_prompt", lambda query: query)

    def slow_warren(message, tag):
        time.sleep(SLOW)
        return json.dumps({"result": {"finalAssistantVisibleText": '{"new":["AAA"],"skip":[],"reasons":{}}'}})

    monkeypatch.setattr(warren_server, "call_warren", slow_warren)

    server = ThreadingHTTPServer(("127.0.0.1", 0), warren_server.Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        def call():
            conn = HTTPConnection("127.0.0.1", port, timeout=10)
            payload = json.dumps({"news": {"AAA": "fresh headline"}})
            conn.request("POST", "/filter", body=payload,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
            conn.close()
            return resp.status, data

        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: call(), range(2)))
        elapsed = time.monotonic() - start
    finally:
        server.shutdown()
        server.server_close()

    assert all(status == 200 for status, _ in results)
    # Serialized execution would take ~2*SLOW; parallel stays well under 1.5*SLOW.
    assert elapsed < SLOW * 1.5, f"requests serialized: {elapsed:.2f}s for 2x{SLOW}s"


# --------------------------------------------------------------------------
# Criterion 2 — concurrent writes of the same ticker do not interleave
# --------------------------------------------------------------------------
def test_concurrent_write_memory_same_ticker(monkeypatch, tmp_path):
    """N threads writing the same symbol produce a well-formed file.

    Each surviving entry must equal one intact payload (no torn/interleaved
    lines), and the file keeps exactly MAX_MEMORY entries.
    """
    monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
    symbol = "ZZZ"
    date = "2026-07-03"
    n = 40

    def worker(i):
        warren_server.write_memory(symbol, f"news-{i:03d}", date)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(worker, range(n)))

    content = (tmp_path / f"{symbol}.md").read_text()
    entries = [e.strip() for e in content.split("\n---\n") if e.strip()]

    assert len(entries) == warren_server.MAX_MEMORY, f"expected {warren_server.MAX_MEMORY} entries, got {len(entries)}"
    for entry in entries:
        lines = entry.split("\n")
        assert lines[0] == f"## {date}", f"corrupt header in entry: {entry!r}"
        assert len(lines) == 2, f"interleaved entry: {entry!r}"
        assert lines[1].startswith("news-") and lines[1][5:].isdigit(), f"torn payload: {lines[1]!r}"


# --------------------------------------------------------------------------
# Criterion 3 — Send Telegram nodes retry and stay fail-visible
# --------------------------------------------------------------------------
def test_send_telegram_nodes_have_retry_and_stay_failing():
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    for name in ("Send Telegram", "Send EOD Telegram"):
        node = nodes[name]
        assert node.get("retryOnFail") is True, f"{name} missing retryOnFail"
        assert node.get("maxTries", 0) >= 2, f"{name} maxTries too low"
        # No continueOnFail: a final failure must leave the execution failed.
        assert node.get("continueOnFail", False) is False, f"{name} must stay fail-visible"


# --------------------------------------------------------------------------
# Bridge HTTP nodes carry explicit timeouts >= 240 s
# --------------------------------------------------------------------------
def test_bridge_http_nodes_have_explicit_timeout():
    workflow = _workflow()
    nodes = _nodes_by_name(workflow)

    for name in ("Call Warren Macro Brief",):
        node = nodes[name]
        timeout = node["parameters"].get("options", {}).get("timeout")
        assert timeout is not None, f"{name} has no explicit timeout"
        assert timeout >= 240000, f"{name} timeout {timeout}ms < 240000ms"
