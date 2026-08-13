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
    verdict = watchdog.evaluate(records, "unknown", TODAY, TODAY)
    assert verdict.level == "hard"
    assert verdict.should_send is True
    assert verdict.message and "aucun run officiel" in verdict.message


def test_official_run_today_fresh_is_ok_no_message() -> None:
    verdict = watchdog.evaluate([_rec()], "success", TODAY, TODAY)
    assert verdict.level == "ok"
    assert verdict.should_send is False
    assert verdict.message is None
    assert verdict.divergence is False


@pytest.mark.parametrize("as_of", ["2026-07-01", None])
def test_stale_as_of_is_soft_not_hard(as_of: str | None) -> None:
    verdict = watchdog.evaluate([_rec(as_of=as_of)], "success", TODAY, TODAY)
    assert verdict.level == "soft"
    assert verdict.should_send is True
    assert verdict.level != "hard"
    assert verdict.message and "pas fraîches" in verdict.message


@pytest.mark.parametrize("sqlite_status", ["none", "error"])
def test_divergence_journal_ran_sqlite_no_success(sqlite_status: str) -> None:
    verdict = watchdog.evaluate([_rec()], sqlite_status, TODAY, TODAY)
    assert verdict.should_send is True
    assert verdict.divergence is True
    assert verdict.message and "divergence" in verdict.message


def test_sqlite_unknown_never_overrides_primary() -> None:
    verdict = watchdog.evaluate([_rec()], "unknown", TODAY, TODAY)
    assert verdict.level == "ok"
    assert verdict.should_send is False
    assert verdict.divergence is False


def test_dry_run_records_never_count_as_official() -> None:
    records = [_rec(dry_run=True), _rec(day="2026-07-02", dry_run=True)]
    verdict = watchdog.evaluate(records, "unknown", TODAY, TODAY)
    assert verdict.level == "hard"


# --------------------------------------------------------------------------- #
# Alarme screener (Epic 10 S1) — l'instantané du poste arrive-t-il encore ?
# --------------------------------------------------------------------------- #
# TODAY = jeudi 2026-07-02. Le lundi 2026-06-29 est donc à 3 jours de bourse
# (mar/mer/jeu) : le seuil est franchi. Le mardi 2026-06-30 n'est qu'à 2.
STALE_SNAPSHOT = date(2026, 6, 29)
RECENT_SNAPSHOT = date(2026, 6, 30)


def test_snapshot_three_trading_days_old_raises_the_alarm() -> None:
    """Le cas nominal du critère : le pont ne reçoit plus rien depuis 3 jours."""
    verdict = watchdog.evaluate([_rec()], "success", TODAY, STALE_SNAPSHOT)

    assert watchdog._trading_days_since(STALE_SNAPSHOT, TODAY) == 3
    assert verdict.should_send is True
    assert verdict.message and "aucun instantané frais depuis 3 jours de bourse" in verdict.message
    assert "2026-06-29" in verdict.message


def test_snapshot_of_the_day_carries_no_alarm() -> None:
    """Le critère doit pouvoir être vert : instantané du jour, run EOD nominal."""
    verdict = watchdog.evaluate([_rec()], "success", TODAY, TODAY)

    assert verdict.should_send is False
    assert verdict.message is None


def test_snapshot_below_threshold_carries_no_alarm() -> None:
    verdict = watchdog.evaluate([_rec()], "success", TODAY, RECENT_SNAPSHOT)

    assert watchdog._trading_days_since(RECENT_SNAPSHOT, TODAY) == 2
    assert verdict.should_send is False


def test_no_snapshot_at_all_raises_the_alarm() -> None:
    """Aucun instantané reçu = la panne d'origine, elle doit parler."""
    verdict = watchdog.evaluate([_rec()], "success", TODAY, None)

    assert verdict.should_send is True
    assert verdict.message and "aucun instantané reçu du poste" in verdict.message


def test_snapshot_alarm_never_swallows_the_eod_verdict() -> None:
    """Deux pannes indépendantes : le message porte les deux, le hard reste hard."""
    verdict = watchdog.evaluate([_rec(dry_run=True)], "unknown", TODAY, STALE_SNAPSHOT)

    assert verdict.level == "hard"
    assert verdict.message
    assert "aucun run officiel" in verdict.message
    assert "aucun instantané frais" in verdict.message


def test_trading_days_skips_the_weekend() -> None:
    # vendredi 2026-07-03 -> lundi 2026-07-06 : un seul jour de bourse écoulé.
    assert watchdog._trading_days_since(date(2026, 7, 3), date(2026, 7, 6)) == 1
    assert watchdog._trading_days_since(TODAY, TODAY) == 0
    # Instantané daté du futur (horloge du poste en avance) : jamais négatif.
    assert watchdog._trading_days_since(date(2026, 7, 10), TODAY) == 0


def test_read_snapshot_date_is_defensive(tmp_path: Path) -> None:
    import json

    good = tmp_path / "latest.json"
    good.write_text(json.dumps({"scanned_at": "2026-08-13T08:10:01.679854+00:00"}), encoding="utf-8")
    assert watchdog.read_snapshot_date(good) == date(2026, 8, 13)

    assert watchdog.read_snapshot_date(tmp_path / "absent.json") is None

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert watchdog.read_snapshot_date(broken) is None

    no_key = tmp_path / "nokey.json"
    no_key.write_text(json.dumps({"v5": {}}), encoding="utf-8")
    assert watchdog.read_snapshot_date(no_key) is None


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


def _snapshot(tmp_path: Path, scanned_at: str) -> Path:
    import json

    path = tmp_path / "latest.json"
    path.write_text(json.dumps({"scanned_at": scanned_at}), encoding="utf-8")
    return path


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
            "--snapshot", str(_snapshot(tmp_path, f"{today}T08:10:00+00:00")),
        ]
    )

    assert rc == 0
    assert sent == []


def test_main_sends_when_the_screener_stopped_pushing(tmp_path: Path, monkeypatch) -> None:
    """Run EOD nominal, mais plus d'instantané depuis longtemps : ça doit parler."""
    sent: list = []
    monkeypatch.setattr(watchdog, "send_telegram", lambda t, c, m: sent.append(m))
    today = _today_iso()
    journal = _journal(
        tmp_path,
        [{"timestamp": f"{today}T21:30:00+00:00", "as_of": today, "dry_run": False}],
    )

    rc = watchdog.main(
        [
            "--runs-log", str(journal),
            "--n8n-db", str(tmp_path / "none.sqlite"),
            "--env-file", str(_env_file(tmp_path)),
            "--snapshot", str(_snapshot(tmp_path, "2026-01-05T08:10:00+00:00")),
        ]
    )

    assert rc == 0
    assert len(sent) == 1 and "jours de bourse" in sent[0]


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
