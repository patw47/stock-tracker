from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "watchdog_eod.py"
_spec = importlib.util.spec_from_file_location("watchdog_eod", _MODULE_PATH)
watchdog = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
sys.modules["watchdog_eod"] = watchdog  # needed for dataclass + annotations resolution
_spec.loader.exec_module(watchdog)

TODAY = date(2026, 7, 2)
WID = "veille-boursiere-001"


def _rec(*, day: str = "2026-07-02", as_of: str | None = "2026-07-02", dry_run: bool = False) -> dict:
    return {
        "timestamp": f"{day}T21:30:05.123456+00:00",
        "as_of": as_of,
        "dry_run": dry_run,
        "candidate_count": 1,
        "survivor_count": 0,
    }


def _make_n8n_db(path: Path, rows: list[tuple[str, str, str]], *, with_status: bool = True) -> None:
    con = sqlite3.connect(path)
    if with_status:
        con.execute(
            "CREATE TABLE execution_entity "
            "(id INTEGER PRIMARY KEY, workflowId TEXT, status TEXT, startedAt TEXT)"
        )
        con.executemany(
            "INSERT INTO execution_entity (workflowId, status, startedAt) VALUES (?, ?, ?)",
            rows,
        )
    else:
        con.execute(
            "CREATE TABLE execution_entity (id INTEGER PRIMARY KEY, workflowId TEXT, startedAt TEXT)"
        )
    con.commit()
    con.close()


# --------------------------------------------------------------------------- #
# evaluate() — pure detection logic
# --------------------------------------------------------------------------- #
def test_no_official_run_today_is_hard() -> None:
    records = [_rec(day="2026-07-01"), _rec(day="2026-07-02", dry_run=True)]
    verdict = watchdog.evaluate(records, "unknown", TODAY)
    assert verdict.level == "hard"
    assert verdict.should_send is True
    assert verdict.message and "aucun run officiel" in verdict.message


def test_official_run_today_fresh_is_ok_no_message() -> None:
    verdict = watchdog.evaluate([_rec()], "success", TODAY)
    assert verdict.level == "ok"
    assert verdict.should_send is False
    assert verdict.message is None
    assert verdict.divergence is False


@pytest.mark.parametrize("as_of", ["2026-07-01", None])
def test_stale_as_of_is_soft_not_hard(as_of: str | None) -> None:
    verdict = watchdog.evaluate([_rec(as_of=as_of)], "success", TODAY)
    assert verdict.level == "soft"
    assert verdict.should_send is True
    assert verdict.level != "hard"
    assert verdict.message and "pas fraîches" in verdict.message


@pytest.mark.parametrize("sqlite_status", ["none", "error"])
def test_divergence_journal_ran_sqlite_no_success(sqlite_status: str) -> None:
    verdict = watchdog.evaluate([_rec()], sqlite_status, TODAY)
    assert verdict.should_send is True
    assert verdict.divergence is True
    assert verdict.message and "divergence" in verdict.message


def test_sqlite_unknown_never_overrides_primary() -> None:
    verdict = watchdog.evaluate([_rec()], "unknown", TODAY)
    assert verdict.level == "ok"
    assert verdict.should_send is False
    assert verdict.divergence is False


def test_dry_run_records_never_count_as_official() -> None:
    records = [_rec(dry_run=True), _rec(day="2026-07-02", dry_run=True)]
    verdict = watchdog.evaluate(records, "unknown", TODAY)
    assert verdict.level == "hard"


# --------------------------------------------------------------------------- #
# query_n8n_last_execution() — read-only, defensive
# --------------------------------------------------------------------------- #
def test_query_n8n_success_error_none(tmp_path: Path) -> None:
    today_iso = f"{TODAY.isoformat()}T21:30:10.000Z"
    yesterday_iso = "2026-07-01T21:30:10.000Z"

    success_db = tmp_path / "success.sqlite"
    _make_n8n_db(success_db, [(WID, "success", today_iso)])
    assert watchdog.query_n8n_last_execution(success_db, WID, TODAY) == "success"

    error_db = tmp_path / "error.sqlite"
    _make_n8n_db(error_db, [(WID, "error", today_iso)])
    assert watchdog.query_n8n_last_execution(error_db, WID, TODAY) == "error"

    old_db = tmp_path / "old.sqlite"
    _make_n8n_db(old_db, [(WID, "success", yesterday_iso)])
    assert watchdog.query_n8n_last_execution(old_db, WID, TODAY) == "none"


def test_query_n8n_missing_or_bad_schema_is_unknown(tmp_path: Path) -> None:
    # (a) missing DB file
    assert watchdog.query_n8n_last_execution(tmp_path / "nope.sqlite", WID, TODAY) == "unknown"

    # (b) DB without execution_entity table
    unrelated = tmp_path / "unrelated.sqlite"
    con = sqlite3.connect(unrelated)
    con.execute("CREATE TABLE other (id INTEGER)")
    con.commit()
    con.close()
    assert watchdog.query_n8n_last_execution(unrelated, WID, TODAY) == "unknown"

    # (c) execution_entity without a status column
    no_status = tmp_path / "nostatus.sqlite"
    _make_n8n_db(no_status, [], with_status=False)
    assert watchdog.query_n8n_last_execution(no_status, WID, TODAY) == "unknown"

    # (d) workflow_id unknown
    assert watchdog.query_n8n_last_execution(tmp_path / "x.sqlite", None, TODAY) == "unknown"


# --------------------------------------------------------------------------- #
# parse_journal_tail() — tolerant reader
# --------------------------------------------------------------------------- #
def test_parse_journal_tail_tolerant(tmp_path: Path) -> None:
    path = tmp_path / "runs.jsonl"
    path.write_text('{"a": 1}\n\n{not json\n{"b": 2}\n', encoding="utf-8")
    records = watchdog.parse_journal_tail(path)
    assert records == [{"a": 1}, {"b": 2}]

    assert watchdog.parse_journal_tail(tmp_path / "missing.jsonl") == []


# --------------------------------------------------------------------------- #
# main() — sends only when required; --check-only never sends
# --------------------------------------------------------------------------- #
def _env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text('TELEGRAM_TOKEN="tok"\nTELEGRAM_CHAT_ID=123\n', encoding="utf-8")
    return path


def _journal(tmp_path: Path, records: list[dict]) -> Path:
    import json

    path = tmp_path / "runs.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def test_main_sends_on_hard_verdict(tmp_path: Path, monkeypatch) -> None:
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(watchdog, "send_telegram", lambda t, c, m: sent.append((t, c, m)))
    # Only a dry-run today => no official run => hard.
    journal = _journal(tmp_path, [{"timestamp": f"{_today_iso()}T21:30:00+00:00", "as_of": _today_iso(), "dry_run": True}])

    rc = watchdog.main(
        [
            "--runs-log", str(journal),
            "--n8n-db", str(tmp_path / "none.sqlite"),
            "--env-file", str(_env_file(tmp_path)),
        ]
    )

    assert rc == 0
    assert len(sent) == 1
    assert sent[0][0] == "tok" and sent[0][1] == "123"


def test_main_no_send_when_ok(tmp_path: Path, monkeypatch) -> None:
    sent: list = []
    monkeypatch.setattr(watchdog, "send_telegram", lambda *a: sent.append(a))
    today = _today_iso()
    journal = _journal(
        tmp_path,
        [{"timestamp": f"{today}T21:30:00+00:00", "as_of": today, "dry_run": False}],
    )

    rc = watchdog.main(
        [
            "--runs-log", str(journal),
            "--n8n-db", str(tmp_path / "none.sqlite"),  # unknown secondary
            "--env-file", str(_env_file(tmp_path)),
        ]
    )

    assert rc == 0
    assert sent == []


def test_main_check_only_never_sends(tmp_path: Path, monkeypatch) -> None:
    sent: list = []
    monkeypatch.setattr(watchdog, "send_telegram", lambda *a: sent.append(a))
    journal = _journal(tmp_path, [{"timestamp": "2020-01-01T00:00:00+00:00", "as_of": "2020-01-01", "dry_run": False}])

    rc = watchdog.main(
        [
            "--check-only",
            "--runs-log", str(journal),
            "--n8n-db", str(tmp_path / "none.sqlite"),
            "--env-file", str(_env_file(tmp_path)),
        ]
    )

    assert rc == 0
    assert sent == []
