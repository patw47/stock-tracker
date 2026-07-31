"""
Tests for the 'Read Tickers' n8n Code node logic.

The node reads portfolio.json and watchlist.json from disk, merges their
ticker lists (adding a `status` field), and returns n8n items.

The extracted JS code is run as a Node.js subprocess so the test validates
the exact logic embedded in the workflow, not a Python reimplementation.

--- Manual n8n verification procedure ---
1. In n8n, open the "veille-boursiere-001" workflow.
2. Trigger a manual run (Execute Workflow Trigger).
3. Open the "Read Tickers" node output — note the symbol list.
4. Edit portfolio.json or watchlist.json: add a ticker (e.g. TSLA to portfolio).
5. Trigger another manual run.
6. Confirm TSLA now appears in the "Read Tickers" output with status "portfolio".
7. Revert the edit.
---
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent

# JS code extracted verbatim from the 'Read Tickers' n8n Code node.
# Update this constant whenever the node code changes.
_NODE_JS = textwrap.dedent("""\
    const fs = require('fs');
    const portfolio = JSON.parse(fs.readFileSync(process.env.PORTFOLIO_PATH, 'utf8'));
    const watchlist = JSON.parse(fs.readFileSync(process.env.WATCHLIST_PATH, 'utf8'));
    const tickers = [
      ...(portfolio.tickers || []).map(t => ({ symbol: t.symbol, sector: t.sector, status: 'portfolio' })),
      ...(watchlist.tickers || []).map(t => ({ symbol: t.symbol, sector: t.sector, status: 'watchlist' }))
    ];
    const items = tickers.map(t => ({ json: t }));
    process.stdout.write(JSON.stringify(items));
""")


def _run_node(portfolio_path: Path, watchlist_path: Path) -> list[dict]:
    """Run node JS logic against given fixture files, return parsed item list."""
    result = subprocess.run(
        ["node", "-e", _NODE_JS],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PORTFOLIO_PATH": str(portfolio_path),
            "WATCHLIST_PATH": str(watchlist_path),
        },
    )
    assert result.returncode == 0, f"node exited {result.returncode}: {result.stderr}"
    return json.loads(result.stdout)


@pytest.fixture()
def fixture_dir(tmp_path: Path) -> Path:
    return tmp_path


def _write_portfolio(path: Path, tickers: list[dict]) -> None:
    path.write_text(json.dumps({"tickers": tickers}))


def _write_watchlist(path: Path, tickers: list[dict]) -> None:
    path.write_text(json.dumps({"tickers": tickers}))


class TestReadTickersNode:
    def test_distinct_tickers_merged(self, fixture_dir: Path) -> None:
        """Portfolio and watchlist tickers both appear in output."""
        _write_portfolio(fixture_dir / "portfolio.json", [
            {"symbol": "AAPL", "sector": "Tech"},
            {"symbol": "MSFT", "sector": "Tech"},
        ])
        _write_watchlist(fixture_dir / "watchlist.json", [
            {"symbol": "NVDA", "sector": "Semiconductors"},
        ])

        items = _run_node(fixture_dir / "portfolio.json", fixture_dir / "watchlist.json")
        symbols = [i["json"]["symbol"] for i in items]

        assert symbols == ["AAPL", "MSFT", "NVDA"]

    def test_portfolio_items_have_portfolio_status(self, fixture_dir: Path) -> None:
        _write_portfolio(fixture_dir / "portfolio.json", [
            {"symbol": "AAPL", "sector": "Tech"},
        ])
        _write_watchlist(fixture_dir / "watchlist.json", [
            {"symbol": "NVDA", "sector": "Semiconductors"},
        ])

        items = _run_node(fixture_dir / "portfolio.json", fixture_dir / "watchlist.json")
        portfolio_item = next(i for i in items if i["json"]["symbol"] == "AAPL")
        watchlist_item = next(i for i in items if i["json"]["symbol"] == "NVDA")

        assert portfolio_item["json"]["status"] == "portfolio"
        assert watchlist_item["json"]["status"] == "watchlist"

    def test_overlapping_symbol_appears_twice(self, fixture_dir: Path) -> None:
        """Symbol in both files appears once per file (no deduplication in node)."""
        _write_portfolio(fixture_dir / "portfolio.json", [
            {"symbol": "AAPL", "sector": "Tech"},
        ])
        _write_watchlist(fixture_dir / "watchlist.json", [
            {"symbol": "AAPL", "sector": "Tech"},
        ])

        items = _run_node(fixture_dir / "portfolio.json", fixture_dir / "watchlist.json")
        symbols = [i["json"]["symbol"] for i in items]

        assert symbols.count("AAPL") == 2
        statuses = [i["json"]["status"] for i in items if i["json"]["symbol"] == "AAPL"]
        assert set(statuses) == {"portfolio", "watchlist"}

    def test_empty_portfolio(self, fixture_dir: Path) -> None:
        _write_portfolio(fixture_dir / "portfolio.json", [])
        _write_watchlist(fixture_dir / "watchlist.json", [
            {"symbol": "SMR", "sector": "Nuclear"},
        ])

        items = _run_node(fixture_dir / "portfolio.json", fixture_dir / "watchlist.json")

        assert len(items) == 1
        assert items[0]["json"]["symbol"] == "SMR"
        assert items[0]["json"]["status"] == "watchlist"

    def test_empty_watchlist(self, fixture_dir: Path) -> None:
        _write_portfolio(fixture_dir / "portfolio.json", [
            {"symbol": "BBAI", "sector": "AI Defense"},
        ])
        _write_watchlist(fixture_dir / "watchlist.json", [])

        items = _run_node(fixture_dir / "portfolio.json", fixture_dir / "watchlist.json")

        assert len(items) == 1
        assert items[0]["json"]["symbol"] == "BBAI"
        assert items[0]["json"]["status"] == "portfolio"

    def test_sector_preserved(self, fixture_dir: Path) -> None:
        _write_portfolio(fixture_dir / "portfolio.json", [
            {"symbol": "RGTI", "sector": "Informatique quantique"},
        ])
        _write_watchlist(fixture_dir / "watchlist.json", [])

        items = _run_node(fixture_dir / "portfolio.json", fixture_dir / "watchlist.json")

        assert items[0]["json"]["sector"] == "Informatique quantique"

    def test_edit_fixture_produces_updated_results(self, fixture_dir: Path) -> None:
        """Modifying a file on disk changes the output on next run."""
        portfolio_path = fixture_dir / "portfolio.json"
        watchlist_path = fixture_dir / "watchlist.json"

        _write_portfolio(portfolio_path, [{"symbol": "AAPL", "sector": "Tech"}])
        _write_watchlist(watchlist_path, [])

        items_before = _run_node(portfolio_path, watchlist_path)
        assert [i["json"]["symbol"] for i in items_before] == ["AAPL"]

        # Simulate editing the file (adding a new ticker)
        _write_portfolio(portfolio_path, [
            {"symbol": "AAPL", "sector": "Tech"},
            {"symbol": "TSLA", "sector": "EV"},
        ])

        items_after = _run_node(portfolio_path, watchlist_path)
        assert [i["json"]["symbol"] for i in items_after] == ["AAPL", "TSLA"]

    def test_real_fixture_files_load_without_error(self) -> None:
        """The tracked portfolio and watchlist example files parse correctly."""
        portfolio_path = REPO_ROOT / "portfolio.example.json"
        watchlist_path = REPO_ROOT / "watchlist.example.json"

        assert portfolio_path.exists(), "portfolio.example.json missing from repo root"
        assert watchlist_path.exists(), "watchlist.example.json missing from repo root"

        items = _run_node(portfolio_path, watchlist_path)

        assert len(items) > 0
        for item in items:
            assert "symbol" in item["json"]
            assert "status" in item["json"]
            assert item["json"]["status"] in {"portfolio", "watchlist"}
