from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timezone
from pathlib import Path

from market_intelligence.outcome_tracker import (
    MEASURED,
    UNAVAILABLE,
    AlertEvent,
    _append_jsonl,
    default_close_fetcher,
    load_outcome_states,
    measure_event,
)
from market_intelligence.tension_signals import EXPECTED_MOVE_MULT

logger = logging.getLogger(__name__)

_RUNTIME_DIR = Path(__file__).parent.parent / "runtime" / "market_intelligence"
TENSION_LOG_PATH = _RUNTIME_DIR / "tension.jsonl"
TENSION_OUTCOMES_PATH = _RUNTIME_DIR / "tension_outcomes.jsonl"


def iter_episodes(path: Path = TENSION_LOG_PATH) -> list[dict]:
    """Episode starts from the tension journal, deduplicated by (ticker, as_of).

    First record wins (re-runs and dry-runs of the same bar are identical by
    construction). Malformed lines are logged and skipped.
    """
    if not path.exists():
        return []
    episodes: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Skipping malformed tension.jsonl line: %s", exc)
            continue
        ticker, as_of = record.get("symbol"), record.get("as_of")
        if not ticker or not as_of:
            continue
        if not (record.get("tension") and record.get("episode_start")):
            continue
        episodes.setdefault(f"{ticker}:{as_of}", record)
    return list(episodes.values())


def run(
    *,
    tension_path: Path = TENSION_LOG_PATH,
    outcomes_path: Path = TENSION_OUTCOMES_PATH,
    close_fetcher=None,
    today: date | None = None,
) -> tuple[int, int, int]:
    """Measure every due tension episode not yet recorded; append idempotently.

    Reuses outcome_tracker.measure_event unchanged (direction=None -> raw
    returns). Adds the pre-registered explosion event: max |move| over the
    20d window > EXPECTED_MOVE_MULT x expected_move_20d journaled at entry.
    """
    today = today or datetime.now(timezone.utc).date()
    close_fetcher = close_fetcher or default_close_fetcher
    episodes = iter_episodes(tension_path)
    states = load_outcome_states(outcomes_path)
    closes_by_ticker = close_fetcher() if episodes else {}

    measured = unavailable = skipped = 0
    for episode in episodes:
        ticker, as_of = str(episode["symbol"]), str(episode["as_of"])
        eid = f"{ticker}:{as_of}"
        if states.get(eid) == MEASURED:
            continue
        event = AlertEvent(
            ticker=ticker, as_of=date.fromisoformat(as_of),
            direction=None, outcome="tension_episode",
        )
        record = measure_event(event, closes_by_ticker.get(ticker), today)
        if record is None:
            skipped += 1
            continue
        if record["status"] == UNAVAILABLE and states.get(eid) == UNAVAILABLE:
            skipped += 1
            continue
        if record["status"] == MEASURED:
            expected = episode.get("expected_move_20d")
            max_abs = max(abs(record["max_drawup"]), abs(record["max_drawdown"]))
            record["max_abs_move"] = max_abs
            record["expected_move_20d"] = expected
            if isinstance(expected, (int, float)) and expected > 0:
                record["move_ratio"] = max_abs / expected
                record["explosion"] = max_abs > EXPECTED_MOVE_MULT * expected
            else:
                record["move_ratio"] = None
                record["explosion"] = None
        _append_jsonl(outcomes_path, record)
        states[eid] = record["status"]
        if record["status"] == MEASURED:
            measured += 1
        else:
            unavailable += 1
    logger.info(
        "tension_outcomes: measured=%d unavailable=%d skipped=%d (of %d episodes)",
        measured, unavailable, skipped, len(episodes),
    )
    return measured, unavailable, skipped


def report(outcomes_path: Path = TENSION_OUTCOMES_PATH) -> str:
    """Live validation readout: n measured episodes, explosion count, P(explosion).

    The decision criterion is pre-registered in docs/TENSION.md: promote to
    real alerting only if live lift >= 1.5 over >= 50 measured episodes
    (lift computed against per-ticker base rates via scripts/tension_backtest.py
    on the live period). This report prints the raw ingredients, not a verdict.
    """
    if not outcomes_path.exists():
        return "tension_outcomes: no outcomes recorded yet"
    seen: dict[str, dict] = {}
    for line in outcomes_path.read_text(encoding="utf-8").splitlines():
        try:
            record = json.loads(line)
            seen[record["event_id"]] = record
        except (json.JSONDecodeError, KeyError):
            continue
    done = [r for r in seen.values() if r.get("status") == MEASURED]
    verdicts = [r for r in done if r.get("explosion") is not None]
    hits = sum(1 for r in verdicts if r["explosion"])
    p = f"{100 * hits / len(verdicts):.0f}%" if verdicts else "—"
    return (
        f"tension_outcomes: episodes measured={len(done)} "
        f"explosions={hits}/{len(verdicts)} P(explosion)={p} "
        f"(criterion: lift>=1.5 on >=50 episodes, docs/TENSION.md)"
    )


def main(argv: list[str] | None = None) -> int:
    """CLI: `python3 -m market_intelligence.tension_outcomes [--report]`.

    Off the critical path: failures are logged and return 0 (same contract as
    outcome_tracker) so a scheduler hook can never block the evening pipeline.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Suivi des outcomes des épisodes de tension (Layer C).")
    parser.add_argument("--report", action="store_true", help="Afficher le readout de validation.")
    args = parser.parse_args(argv)
    try:
        if args.report:
            print(report())
            return 0
        measured, unavailable, skipped = run()
        print(f"tension_outcomes: measured={measured} unavailable={unavailable} skipped={skipped}")
    except Exception as exc:  # pragma: no cover - defensive off-critical-path guard
        logger.error("tension_outcomes failed (non-blocking): %s", exc)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
