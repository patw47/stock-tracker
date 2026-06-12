from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

import warren_server


class TestWriteMemory:
    """Unit tests for write_memory (format, prepend, cap)."""

    def test_creates_file_with_correct_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First write creates file with ## DATE header and stripped news."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        warren_server.write_memory("AAPL", "Apple rose 2%", "2026-06-12")
        content = (tmp_path / "AAPL.md").read_text()
        assert content == "## 2026-06-12\nApple rose 2%\n"

    def test_prepends_new_entry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Second write prepends newest entry; file has two entries."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        warren_server.write_memory("AAPL", "Day 1 news", "2026-06-11")
        warren_server.write_memory("AAPL", "Day 2 news", "2026-06-12")
        content = (tmp_path / "AAPL.md").read_text()
        assert content == "## 2026-06-12\nDay 2 news\n---\n## 2026-06-11\nDay 1 news\n"

    def test_caps_at_three_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fourth write keeps exactly 3 entries; oldest dropped."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        for i in range(1, 5):
            warren_server.write_memory("AAPL", f"Day {i} news", f"2026-06-{i:02d}")
        content = (tmp_path / "AAPL.md").read_text()
        entries = [e for e in content.split("\n---\n") if e.strip()]
        assert len(entries) == 3
        assert "Day 1 news" not in content

    def test_strips_whitespace_from_news(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Raw news with leading/trailing whitespace is stripped."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        warren_server.write_memory("AAPL", "  news with spaces  ", "2026-06-12")
        content = (tmp_path / "AAPL.md").read_text()
        assert content == "## 2026-06-12\nnews with spaces\n"


class TestHandleMemorize:
    """Tests for Handler.handle_memorize endpoint."""

    def test_new_tickers_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """NEW tickers in newTickers list → files created."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        handler = mock.MagicMock(spec=warren_server.Handler)
        body = {
            "newTickers": ["AAPL", "MSFT"],
            "allNews": {"AAPL": "Apple news", "MSFT": "Microsoft news", "GOOGL": "skip me"},
        }
        warren_server.Handler.handle_memorize(handler, body)
        assert (tmp_path / "AAPL.md").exists()
        assert (tmp_path / "MSFT.md").exists()

    def test_skip_tickers_not_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tickers NOT in newTickers list → no file created."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        handler = mock.MagicMock(spec=warren_server.Handler)
        body = {
            "newTickers": ["AAPL"],
            "allNews": {"AAPL": "Apple news", "GOOGL": "Google news"},
        }
        warren_server.Handler.handle_memorize(handler, body)
        assert not (tmp_path / "GOOGL.md").exists()

    def test_skip_does_not_overwrite_existing_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SKIP ticker with pre-existing memory file → file unchanged."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        existing = tmp_path / "GOOGL.md"
        existing.write_text("## 2026-06-11\nOld Google news\n")
        handler = mock.MagicMock(spec=warren_server.Handler)
        body = {
            "newTickers": [],
            "allNews": {"GOOGL": "New Google news"},
        }
        warren_server.Handler.handle_memorize(handler, body)
        assert existing.read_text() == "## 2026-06-11\nOld Google news\n"

    def test_written_file_content_format(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Written file matches ## DATE\\n<news>\\n format."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        handler = mock.MagicMock(spec=warren_server.Handler)
        body = {
            "newTickers": ["AAPL"],
            "allNews": {"AAPL": "Apple rose 2% on strong earnings"},
        }
        with mock.patch("warren_server.datetime") as mock_dt:
            mock_dt.date.today.return_value.isoformat.return_value = "2026-06-12"
            warren_server.Handler.handle_memorize(handler, body)
        content = (tmp_path / "AAPL.md").read_text()
        assert content.startswith("## 2026-06-12\nApple rose 2% on strong earnings")

    def test_three_entry_cap_via_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Three existing entries + one new → oldest dropped, still 3 entries."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        (tmp_path / "AAPL.md").write_text(
            "## 2026-06-11\nDay 3\n---\n## 2026-06-10\nDay 2\n---\n## 2026-06-09\nDay 1\n"
        )
        handler = mock.MagicMock(spec=warren_server.Handler)
        body = {"newTickers": ["AAPL"], "allNews": {"AAPL": "Day 4"}}
        with mock.patch("warren_server.datetime") as mock_dt:
            mock_dt.date.today.return_value.isoformat.return_value = "2026-06-12"
            warren_server.Handler.handle_memorize(handler, body)
        content = (tmp_path / "AAPL.md").read_text()
        entries = [e for e in content.split("\n---\n") if e.strip()]
        assert len(entries) == 3
        assert "Day 1" not in content

    def test_returns_success_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Endpoint returns JSON with status: ok and written list."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        handler = mock.MagicMock(spec=warren_server.Handler)
        body = {"newTickers": ["AAPL"], "allNews": {"AAPL": "Apple news"}}
        result = warren_server.Handler.handle_memorize(handler, body)
        data = json.loads(result)
        assert data["status"] == "ok"
        assert "AAPL" in data["written"]

    def test_empty_new_tickers_returns_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty newTickers list: no writes, returns ok."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        handler = mock.MagicMock(spec=warren_server.Handler)
        body = {"newTickers": [], "allNews": {"AAPL": "news"}}
        result = warren_server.Handler.handle_memorize(handler, body)
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["written"] == []
        assert not list(tmp_path.iterdir())

    def test_missing_body_keys_returns_ok(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Body with no keys: defaults to empty, returns ok without crash."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        handler = mock.MagicMock(spec=warren_server.Handler)
        result = warren_server.Handler.handle_memorize(handler, {})
        data = json.loads(result)
        assert data["status"] == "ok"

    def test_multiple_symbols_all_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All tickers in newTickers list get their own memory file."""
        monkeypatch.setattr(warren_server, "MEMORY_DIR", str(tmp_path))
        handler = mock.MagicMock(spec=warren_server.Handler)
        tickers = ["AAPL", "MSFT", "TSLA"]
        body = {
            "newTickers": tickers,
            "allNews": {t: f"{t} news today" for t in tickers},
        }
        warren_server.Handler.handle_memorize(handler, body)
        for sym in tickers:
            assert (tmp_path / f"{sym}.md").exists()
