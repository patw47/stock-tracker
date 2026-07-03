#!/usr/bin/env python3
"""Heartbeat hebdomadaire — Epic 2 Sprint 3.

Chaque vendredi après le run EOD, produit un court message Telegram (preuve de
vie + résumé de la semaine glissante lun-ven) à partir de ``runs.jsonl``. Le
silence des jours sans anomalie devient une information positive.

Template Python pur, **zéro LLM, zéro réseau** : le module imprime le message sur
stdout ; l'envoi Telegram est fait par n8n (``executeCommand`` → chaîne
Aggregate/Split/Send).
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_RUNS_LOG_PATH = (
    Path(__file__).parent.parent / "runtime" / "market_intelligence" / "runs.jsonl"
)


def week_window(today: date) -> tuple[date, date]:
    """Return (Monday, Friday) of ``today``'s week."""
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=4)


def _record_date(record: dict) -> date | None:
    timestamp = record.get("timestamp")
    if not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp).astimezone(timezone.utc).date()
    except ValueError:
        return None


def _official_this_week(records: Iterable[dict], today: date) -> list[dict]:
    monday, friday = week_window(today)
    kept: list[dict] = []
    for record in records:
        if record.get("dry_run") is not False:
            continue
        run_date = _record_date(record)
        if run_date is not None and monday <= run_date <= friday:
            kept.append(record)
    return kept


def build_heartbeat(records: Iterable[dict], today: date) -> str:
    """Build the weekly heartbeat message (pure). ``today`` is injected."""
    monday, friday = week_window(today)
    header = f"🫀 Heartbeat hebdo — semaine du {monday.isoformat()} au {friday.isoformat()}"

    official = _official_this_week(records, today)
    if not official:
        return f"{header}\nAucun run officiel cette semaine (lun-ven). À vérifier."

    total_candidates = sum(int(r.get("candidate_count", 0)) for r in official)
    alerts_sent = sum(
        int(r.get("survivor_count", 0)) for r in official if r.get("should_send") is True
    )

    reasons: Counter[str] = Counter()
    for record in official:
        for detail in record.get("candidates_detail", []):
            outcome = detail.get("outcome", "")
            if isinstance(outcome, str) and outcome.startswith("gated_dedup:"):
                reasons[outcome.split(":", 1)[1]] += 1

    issues: Counter[str] = Counter()
    for record in official:
        for issue in record.get("data_issues", []):
            if isinstance(issue, str):
                issues[issue] += 1

    if total_candidates == 0:
        return (
            f"{header}\nPipeline vivant : {len(official)} runs officiels, aucun "
            f"candidat cette semaine. Rien à signaler."
        )

    lines = [
        header,
        f"Runs officiels : {len(official)}",
        f"Candidats détectés : {total_candidates}",
        f"Alertes envoyées : {alerts_sent}",
    ]
    if reasons:
        lines.append("Suppressions dédup :")
        for reason, count in reasons.most_common():
            lines.append(f"  • {reason} : {count}")
    if issues:
        lines.append("Data issues fréquents :")
        for issue, count in issues.most_common(3):
            lines.append(f"  • {issue} : {count}")
    return "\n".join(lines)


def load_records(path: Path = _RUNS_LOG_PATH) -> list[dict]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict] = []
    for line in text.splitlines():
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


def main() -> None:
    records = load_records()
    print(build_heartbeat(records, datetime.now(timezone.utc).date()))


if __name__ == "__main__":
    main()
