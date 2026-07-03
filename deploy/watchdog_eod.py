#!/usr/bin/env python3
"""Watchdog EOD externe — Epic 2 Sprint 2.

Alerte sur Telegram si le run EOD officiel de 21:30 UTC n'a pas eu lieu ou a
échoué un jour ouvré, **même si n8n est mort**. Exécuté sur le VPS par un timer
systemd (~22:15 UTC lun-ven, user queenp), indépendant de n8n.

Vérification primaire (source de vérité) : le journal des runs
``runtime/market_intelligence/runs.jsonl`` (Epic 2 S1) contient-il un run
officiel (``dry_run == false``) daté d'aujourd'hui, avec des données fraîches ?

Vérification secondaire (lecture seule, best-effort) : n8n rapporte-t-il une
exécution réussie aujourd'hui dans ``database.sqlite`` ? Une divergence
journal↔n8n est signalée mais ne fait jamais planter le watchdog.

Le watchdog n'écrit JAMAIS dans la DB n8n et ne dépend pas de n8n pour alerter
(envoi Telegram via l'API bot directe, token lu depuis ``.env``).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

_REPO = Path("/opt/apps/stock-tracker")
_RUNS_LOG = _REPO / "runtime" / "market_intelligence" / "runs.jsonl"
_N8N_DB = _REPO / "n8n-data" / ".n8n" / "database.sqlite"
_ENV_FILE = _REPO / ".env"
_WORKFLOW_JSON = _REPO / "workflow.json"
_TELEGRAM_API = "https://api.telegram.org"

# n8n execution statuses treated as a failed run.
_ERROR_STATUSES = {"error", "crashed", "failed", "canceled"}


@dataclass(frozen=True)
class Verdict:
    """Outcome of the watchdog checks."""

    level: str  # "ok" | "soft" | "hard"
    should_send: bool
    divergence: bool
    message: str | None


# --------------------------------------------------------------------------- #
# Journal (primary source of truth)
# --------------------------------------------------------------------------- #
def parse_journal_tail(path: Path, max_lines: int = 500) -> list[dict]:
    """Return the parsed JSONL records (tolerant: skips blank/corrupt lines)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict] = []
    for line in text.splitlines()[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def _run_date(record: dict) -> date | None:
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp).astimezone(timezone.utc).date()
    except ValueError:
        return None


def _as_of_date(record: dict) -> date | None:
    value = record.get("as_of")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# n8n executions (secondary, read-only, best-effort)
# --------------------------------------------------------------------------- #
def _parse_sqlite_datetime(value: object) -> date | None:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        # n8n may store epoch milliseconds.
        try:
            seconds = value / 1000 if value > 1_000_000_000_000 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def query_n8n_last_execution(
    db_path: Path, workflow_id: str | None, today: date
) -> str:
    """Return today's n8n execution status: success | error | none | unknown.

    Read-only, defensive: any missing DB/table/column or error yields
    ``"unknown"`` — the watchdog never writes and never crashes on n8n.
    """
    if workflow_id is None or not Path(db_path).exists():
        return "unknown"
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return "unknown"
    try:
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cols = {row["name"] for row in cur.execute("PRAGMA table_info(execution_entity)")}
        if not cols or "status" not in cols or "workflowId" not in cols:
            return "unknown"
        time_col = next(
            (c for c in ("startedAt", "createdAt", "stoppedAt") if c in cols), None
        )
        if time_col is None:
            return "unknown"
        rows = cur.execute(
            f'SELECT "status" AS status, "{time_col}" AS started '
            'FROM execution_entity WHERE "workflowId"=?',
            (workflow_id,),
        ).fetchall()
    except sqlite3.Error:
        return "unknown"
    finally:
        con.close()

    today_statuses = [
        str(row["status"]).lower()
        for row in rows
        if _parse_sqlite_datetime(row["started"]) == today
    ]
    if not today_statuses:
        return "none"
    if "success" in today_statuses:
        return "success"
    if any(status in _ERROR_STATUSES for status in today_statuses):
        return "error"
    return "none"


def read_workflow_id(path: Path = _WORKFLOW_JSON) -> str | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("id")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


# --------------------------------------------------------------------------- #
# Decision (pure)
# --------------------------------------------------------------------------- #
def _sqlite_hint(sqlite_status: str) -> str:
    return {
        "success": " (n8n rapporte pourtant une exécution réussie — le journal "
        "n'a peut-être pas été écrit)",
        "error": " (n8n rapporte une exécution en échec)",
        "none": " (aucune exécution n8n aujourd'hui)",
        "unknown": "",
    }.get(sqlite_status, "")


def evaluate(records: list[dict], sqlite_status: str, today: date) -> Verdict:
    """Decide whether to alert. Pure function — the testable core."""
    official = [record for record in records if record.get("dry_run") is False]
    today_official = [record for record in official if _run_date(record) == today]

    if not today_official:
        message = (
            f"🔴 Watchdog EOD — aucun run officiel détecté aujourd'hui "
            f"({today.isoformat()}). Le pipeline 21:30 UTC n'a pas tourné ou a "
            f"échoué avant journalisation.{_sqlite_hint(sqlite_status)}\n"
            f"Pistes : service stock-tracker down, activation du workflow échouée, "
            f"exécution n8n failed."
        )
        return Verdict("hard", True, False, message)

    latest = today_official[-1]
    as_of = _as_of_date(latest)
    if as_of is None or as_of < today:
        stale = as_of.isoformat() if as_of else "aucune barre fraîche"
        message = (
            f"🟡 Watchdog EOD — un run officiel a bien tourné aujourd'hui "
            f"({today.isoformat()}) mais sur des données pas fraîches "
            f"(as_of = {stale}). Jour férié US probable — vérifier si attendu."
        )
        return Verdict("soft", True, False, message)

    if sqlite_status in {"none", "error"}:
        message = (
            f"🟠 Watchdog EOD — divergence : runs.jsonl montre un run officiel "
            f"aujourd'hui ({today.isoformat()}) mais n8n ne rapporte pas "
            f"d'exécution réussie{_sqlite_hint(sqlite_status)}. Vérifier "
            f"l'exécution n8n."
        )
        return Verdict("ok", True, True, message)

    return Verdict("ok", False, False, None)


# --------------------------------------------------------------------------- #
# Telegram + env
# --------------------------------------------------------------------------- #
def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"{_TELEGRAM_API}/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        response.read()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="EOD run watchdog (Epic 2 S2).")
    parser.add_argument(
        "--check-only",
        "--dry-run",
        action="store_true",
        dest="check_only",
        help="Evaluate and print the verdict without sending any Telegram message.",
    )
    parser.add_argument("--runs-log", default=None)
    parser.add_argument("--n8n-db", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--workflow-json", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    runs_log = Path(args.runs_log) if args.runs_log else _RUNS_LOG
    n8n_db = Path(args.n8n_db) if args.n8n_db else _N8N_DB
    env_file = Path(args.env_file) if args.env_file else _ENV_FILE
    workflow_json = Path(args.workflow_json) if args.workflow_json else _WORKFLOW_JSON

    today = datetime.now(timezone.utc).date()
    records = parse_journal_tail(runs_log)
    workflow_id = read_workflow_id(workflow_json)
    sqlite_status = query_n8n_last_execution(n8n_db, workflow_id, today)
    verdict = evaluate(records, sqlite_status, today)

    print(
        f"[watchdog] date={today.isoformat()} level={verdict.level} "
        f"should_send={verdict.should_send} divergence={verdict.divergence} "
        f"sqlite={sqlite_status} records={len(records)}"
    )
    if verdict.message:
        print(verdict.message)

    if verdict.should_send and not args.check_only:
        env = read_env(env_file)
        token = env.get("TELEGRAM_TOKEN")
        chat_id = env.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            print(
                "[watchdog] TELEGRAM_TOKEN/TELEGRAM_CHAT_ID absents — alerte impossible",
                file=sys.stderr,
            )
            return 2
        assert verdict.message is not None
        send_telegram(token, chat_id, verdict.message)
        print("[watchdog] alerte Telegram envoyée")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
