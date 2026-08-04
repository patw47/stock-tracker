from __future__ import annotations

import argparse
import fcntl
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_RUNTIME_DIR = Path(__file__).parent.parent / "runtime" / "market_intelligence"
RUNS_LOG_PATH = _RUNTIME_DIR / "runs.jsonl"
OUTCOMES_PATH = _RUNTIME_DIR / "outcomes.jsonl"

HORIZONS = (1, 5, 20)
MAX_HORIZON = max(HORIZONS)
# Wait this many calendar days after an event before deciding it is unmeasurable;
# below this an absent close just means "data not fetched yet", not "no data".
# 20 trading days span ~28 calendar days with no holiday, but a single NYSE
# holiday in the window pushes the 20th close to J+29/J+30, so 28 would freeze
# ~30-40% of events prematurely. 40 leaves a comfortable holiday margin.
READY_MIN_CALENDAR_DAYS = 40

MEASURED = "measured"
UNAVAILABLE = "unavailable"

# () -> {canonical_symbol: pandas Series of closes indexed by date}
CloseFetcher = Callable[[], "dict"]


@dataclass(frozen=True)
class AlertEvent:
    """One measurable event from the run journal (a survivor or gated candidate)."""

    ticker: str
    as_of: date
    direction: str | None  # "up" | "down" | None (unknown)
    outcome: str


@dataclass(frozen=True)
class Summary:
    """Counts for one outcome-tracking pass."""

    measured: int
    unavailable: int
    skipped: int


def _direction_from_z(z_resid: object) -> str | None:
    """Derive the detected direction from the residual z-score sign.

    candidates_detail persists z_resid but not the direction; the residual sign is
    the dominant driver of CandidateAlert.direction, so it is used as the proxy.
    """
    if not isinstance(z_resid, (int, float)):
        return None
    if z_resid > 0:
        return "up"
    if z_resid < 0:
        return "down"
    return None


def _is_measurable_outcome(outcome: str) -> bool:
    """True for sent alerts (survived) and gated candidates; False otherwise."""
    return outcome == "survived" or outcome.startswith("gated_dedup")


def event_id(event: AlertEvent) -> str:
    """Stable id for one event: ticker + as_of."""
    return f"{event.ticker}:{event.as_of.isoformat()}"


def iter_events(runs_path: Path = RUNS_LOG_PATH) -> list[AlertEvent]:
    """Parse the run journal into measurable events, de-duplicated by event id.

    Only official runs (``dry_run`` false) contribute; each survivor or gated
    candidate becomes one event. Malformed lines are logged and skipped.
    """
    if not runs_path.exists():
        return []
    events: dict[str, AlertEvent] = {}
    for line in runs_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Skipping malformed runs.jsonl line: %s", exc)
            continue
        if record.get("dry_run"):
            continue
        as_of_raw = record.get("as_of")
        if not as_of_raw:
            continue
        try:
            as_of = date.fromisoformat(str(as_of_raw))
        except ValueError:
            continue
        for detail in record.get("candidates_detail", []):
            outcome = str(detail.get("outcome", ""))
            if not _is_measurable_outcome(outcome):
                continue
            event = AlertEvent(
                ticker=str(detail.get("ticker", "")),
                as_of=as_of,
                direction=_direction_from_z(detail.get("z_resid")),
                outcome=outcome,
            )
            events.setdefault(event_id(event), event)
    return list(events.values())


def load_outcome_states(outcomes_path: Path = OUTCOMES_PATH) -> dict[str, str]:
    """Return the last recorded status per event id in the outcomes journal.

    Only ``measured`` is terminal. An ``unavailable`` state is kept so we don't
    re-append it every run, but it never freezes the event: a later run can still
    upgrade it to ``measured`` once the data becomes available.
    """
    if not outcomes_path.exists():
        return {}
    states: dict[str, str] = {}
    for line in outcomes_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            states[record["event_id"]] = record["status"]
        except (json.JSONDecodeError, KeyError):
            continue
    return states


def _append_jsonl(path: Path, record: dict) -> None:
    """Append one JSONL line under an exclusive flock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _signed(raw_return: float, direction: str | None) -> float:
    """Sign a raw return by the detected direction (down = profit when price falls)."""
    return -raw_return if direction == "down" else raw_return


def measure_event(event: AlertEvent, closes, today: date) -> dict | None:
    """Return the outcome record for an event, or None if it is not yet due.

    ``closes`` is a pandas Series of closing prices indexed by date, or None. An
    event is measured once >= MAX_HORIZON trading days of closes follow its as_of;
    if the window can never complete (no data, delisted) and enough calendar time
    has passed, it is recorded as ``unavailable``. Never raises on missing data.
    """
    import pandas as pd

    base = {
        "event_id": event_id(event),
        "ticker": event.ticker,
        "as_of": event.as_of.isoformat(),
        "direction": event.direction,
        "outcome": event.outcome,
        "measured_at": today.isoformat(),
    }
    ready = (today - event.as_of).days >= READY_MIN_CALENDAR_DAYS

    def unavailable(reason: str) -> dict:
        return {**base, "status": UNAVAILABLE, "reason": reason}

    if closes is None or len(closes) == 0:
        return unavailable("no_data") if ready else None

    series = closes.copy()
    series.index = pd.to_datetime(series.index).normalize()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    anchor = pd.Timestamp(event.as_of)
    if anchor not in series.index:
        return unavailable("anchor_close_missing") if ready else None

    entry = float(series.loc[anchor])
    after = series[series.index > anchor]
    if entry == 0:
        return unavailable("zero_entry_close") if ready else None
    if len(after) < MAX_HORIZON:
        return unavailable("insufficient_history") if ready else None

    window = after.iloc[:MAX_HORIZON]
    record = {**base, "status": MEASURED, "entry_close": entry}
    for horizon in HORIZONS:
        raw = float(after.iloc[horizon - 1]) / entry - 1.0
        record[f"ret_{horizon}d"] = _signed(raw, event.direction)
    record["max_drawup"] = float(window.max()) / entry - 1.0
    record["max_drawdown"] = float(window.min()) / entry - 1.0
    return record


def run(
    *,
    runs_path: Path = RUNS_LOG_PATH,
    outcomes_path: Path = OUTCOMES_PATH,
    close_fetcher: CloseFetcher | None = None,
    today: date | None = None,
) -> Summary:
    """Measure every due event not yet recorded; append results idempotently."""
    today = today or datetime.now(timezone.utc).date()
    close_fetcher = close_fetcher or default_close_fetcher
    events = iter_events(runs_path)
    states = load_outcome_states(outcomes_path)

    closes_by_ticker = close_fetcher() if events else {}

    measured = unavailable = skipped = 0
    for event in events:
        eid = event_id(event)
        # measured is terminal; unavailable is retried (never frozen) so a
        # transient/degraded fetch can still be upgraded to measured later.
        if states.get(eid) == MEASURED:
            continue
        record = measure_event(event, closes_by_ticker.get(event.ticker), today)
        if record is None:
            skipped += 1
            continue
        if record["status"] == UNAVAILABLE and states.get(eid) == UNAVAILABLE:
            # Already flagged unavailable; don't append a duplicate, keep retrying.
            skipped += 1
            continue
        _append_jsonl(outcomes_path, record)
        states[eid] = record["status"]
        if record["status"] == MEASURED:
            measured += 1
        else:
            unavailable += 1
    logger.info(
        "outcome_tracker: measured=%d unavailable=%d skipped=%d (of %d events)",
        measured, unavailable, skipped, len(events),
    )
    return Summary(measured=measured, unavailable=unavailable, skipped=skipped)


def default_close_fetcher(days: int = 280) -> dict:
    """Fetch recent closes per ticker via fetch_eod, keyed by canonical symbol."""
    from market_intelligence.fetch_eod import fetch_all

    frames = fetch_all(days=days)
    return {
        symbol: frame["Close"]
        for symbol, frame in frames.items()
        if not frame.empty and "Close" in frame.columns
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `python3 -m market_intelligence.outcome_tracker`.

    Off the critical path: a failure is logged and returns 0 so it can never block
    the evening pipeline when hooked to the daily schedule.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    argparse.ArgumentParser(description="Suivi des outcomes prix (J+1/J+5/J+20) des alertes.").parse_args(argv)
    try:
        summary = run()
    except Exception as exc:  # pragma: no cover - defensive off-critical-path guard
        logger.error("outcome_tracker failed (non-blocking): %s", exc)
        return 0
    print(f"outcome_tracker: measured={summary.measured} "
          f"unavailable={summary.unavailable} skipped={summary.skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
