"""Reconcile watchlist.json with the smallcaps-screener v5 tracking journal.

Every ticker that entered a v5 washout cohort is watched by the tension tier for
its judgment window (entry at qualification, exit at J+63), and nothing else is
touched: entries this bridge creates carry ``source: "smallcaps-v5"`` and only
those are ever removed. Telegram-added tickers are untouchable (safety rule #1).

This is a RECONCILIATION, not an event stream: it derives the target state from
the current tracking journal and converges to it, so it is idempotent and robust
to missed runs. Every failure mode (no snapshot, malformed payload, suspiciously
empty tracking) is a logged no-op — the watchlist is never purged by accident.

Transport (Epic 10 S1): the workstation runs the screener behind a home router,
so the VPS cannot call it. The workstation PUSHES ``/api/scan`` to
``runtime/screener/latest.json`` whenever a scan lands (``deploy/screener-push.*``)
and the bridge reads that file. Only a snapshot strictly newer than the one
already applied is acted upon — replaying an old one would remove every ticker
qualified since.

Layer B (registry, classification, sector factors) is deliberately NOT touched:
that stays a human decision in a PR.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from agents.warren.manage_tickers import _load, _save, _today
from market_intelligence.registry_check import REPO_ROOT

logger = logging.getLogger(__name__)

# The runtime list only, never the committed watchlist.example.json fallback that
# registry_check resolves to in a dev checkout: the bridge writes, and it must
# not touch a fixture — nor create the runtime file, which would shadow the
# example for the whole pipeline. Absent file = logged no-op.
WATCHLIST_PATH = REPO_ROOT / "watchlist.json"
SOURCE_TAG = "smallcaps-v5"
HORIZON_DAYS = 63  # v5 judgment horizon: a bridged ticker leaves at days_held >= 63
DEFAULT_CAP = 150
DEFAULT_API_URL = "http://localhost:8000"

# The workstation pushes /api/scan here (deploy/screener-push.sh); the VPS only
# ever reads a file. `runtime/` is gitignored: a snapshot is execution state, and
# a deploy `git reset --hard` must not resurrect an old one.
SNAPSHOT_PATH = REPO_ROOT / "runtime" / "screener" / "latest.json"
# Watchlist key holding the scanned_at of the last APPLIED snapshot. Kept inside
# watchlist.json so the guard state travels with the file it protects.
SCANNED_AT_KEY = "v5_scanned_at"


def _symbol(entry: dict) -> str:
    return str(entry.get("symbol", "")).strip().upper()


def _iter_rows(node: object):
    """Yield every mapping carrying both ``ticker`` and ``days_held``.

    The smallcaps-screener repo is not vendored here and the shape of the v5
    section is not frozen (as of 2026-08-04 the windows come as one flat
    ``v5.tracking`` list carrying a ``window`` field, but they could just as well
    be nested per window). Rather than guessing a key path that would silently
    yield nothing the day it moves, we walk the section and pick up the tracking
    rows by their own fields — the ``windows[*].cohort`` rows have no
    ``days_held`` and are therefore ignored.
    """
    if isinstance(node, dict):
        if "ticker" in node and "days_held" in node:
            yield node
            return
        for value in node.values():
            yield from _iter_rows(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_rows(item)


def parse_tracking(payload: object) -> dict[str, int] | None:
    """Max ``days_held`` per ticker over the union of the v5 tracking windows.

    A ticker present in several windows yields a single entry (the longest
    holding wins). Returns ``None`` when the payload is not usable at all, which
    the caller must treat as a no-op — never as an empty tracking journal.

    Scoped to the ``v5`` section on purpose: the live payload also carries a
    ``v4_tracking`` journal (the previous protocol, a different cohort, 79 of its
    215 tickers shared with v5 on 2026-08-04). Walking the whole payload would
    silently bridge v4 members too. No ``v5`` section = unusable, not empty.
    """
    section = payload.get("v5") if isinstance(payload, dict) else None
    if not isinstance(section, (dict, list)):
        logger.error(
            "v5 bridge: /api/scan payload carries no usable v5 section (%s) - no-op",
            type(section).__name__,
        )
        return None
    tracked: dict[str, int] = {}
    for row in _iter_rows(section):
        symbol = str(row.get("ticker", "")).strip().upper()
        try:
            days = int(row["days_held"])
        except (TypeError, ValueError):
            continue  # fail-soft: one bad row never sinks the run
        if symbol:
            tracked[symbol] = max(tracked.get(symbol, days), days)
    return tracked


def _scanned_at(payload: object, key: str = "scanned_at") -> datetime | None:
    """Parse a scan timestamp; ``None`` when absent or unparseable.

    Normalised to UTC so two snapshots are always comparable: the screener writes
    an offset-aware timestamp today, but a naive one would silently compare as a
    different type and crash the guard rather than protect it.
    """
    raw = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def read_snapshot(snapshot_path: str | os.PathLike | None = None) -> dict | None:
    """Read the scan snapshot the workstation pushed; ``None`` (logged) on failure.

    Absent file, unreadable file, invalid JSON and non-object payload are all the
    same thing here: no usable snapshot, so a logged no-op — never an exception,
    never an empty tracking journal (which would look like a purge order).
    """
    path = Path(snapshot_path or SNAPSHOT_PATH)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        logger.error("v5 bridge: snapshot %s unreadable (%s) - no-op", path, exc)
        return None
    except ValueError as exc:
        logger.error("v5 bridge: snapshot %s is not valid JSON (%s) - no-op", path, exc)
        return None
    if not isinstance(payload, dict):
        logger.error(
            "v5 bridge: snapshot %s is a %s, not an object - no-op",
            path,
            type(payload).__name__,
        )
        return None
    return payload


def fetch_payload(
    *,
    api_url: str | None = None,
    snapshot_path: str | os.PathLike | None = None,
    timeout: int = 20,
) -> dict | None:
    """Return the scan payload; ``None`` on any failure (always a logged no-op).

    Two sources, file first: the VPS reads the snapshot pushed by the workstation
    (``deploy/screener-push.sh``). The HTTP source is kept for a run made ON the
    workstation, where ``/api/scan`` is actually reachable — from the VPS it never
    was, which is the whole reason this sprint exists.
    """
    if api_url is None:
        return read_snapshot(snapshot_path)
    url = f"{api_url.rstrip('/') or DEFAULT_API_URL}/api/scan"
    try:
        with urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
            if getattr(response, "status", 200) != 200:
                logger.error("v5 bridge: %s returned HTTP %s - no-op", url, response.status)
                return None
            return json.loads(response.read())
    except (URLError, OSError, ValueError) as exc:
        logger.error("v5 bridge: %s unreachable (%s) - no-op", url, exc)
        return None


def _result(
    added: list[str],
    removed: list[str],
    unchanged: int,
    anomalies: list[str],
) -> dict:
    """Log the run summary line and return it (orchestrator log style)."""
    logger.info(
        "v5 bridge run complete: added=%d removed=%d unchanged=%d anomalies=%d",
        len(added),
        len(removed),
        unchanged,
        len(anomalies),
    )
    return {
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
        "anomalies": anomalies,
    }


def reconcile(
    tracked: dict[str, int],
    *,
    scanned_at: datetime | None = None,
    watchlist_path: str | os.PathLike | None = None,
    cap: int = DEFAULT_CAP,
) -> dict:
    """Converge the watchlist toward the target state derived from ``tracked``."""
    path = Path(watchlist_path or WATCHLIST_PATH)
    if not path.exists():
        anomaly = f"{path} does not exist - no-op (the bridge never creates it)"
        logger.error("v5 bridge: %s", anomaly)
        return _result([], [], 0, [anomaly])
    data = _load(str(path))

    # Monotonicity guard. Age alone breaks nothing (entry_date is immutable,
    # days_held drifts one notch per trading day), but a REGRESSION does: an old
    # snapshot replayed would remove every ticker qualified since. Exact test, no
    # constant to tune — only a strictly newer scan may be applied.
    applied = _scanned_at(data, SCANNED_AT_KEY)
    if applied is not None and (scanned_at is None or scanned_at <= applied):
        anomaly = (
            f"snapshot scanned_at={scanned_at.isoformat() if scanned_at else 'unknown'} "
            f"is not newer than the applied {applied.isoformat()} - refused, no write"
        )
        logger.warning("v5 bridge: %s", anomaly)
        return _result([], [], 0, [anomaly])

    entries = data["tickers"]
    ours = {_symbol(t) for t in entries if t.get("source") == SOURCE_TAG}
    present = {_symbol(t) for t in entries}

    if not tracked and ours:
        # An empty journal while our tickers are still under horizon is an
        # anomaly (scan not run yet, upstream glitch), never a reason to purge:
        # an entry only survives a previous run if days_held was still < 63.
        anomaly = (
            f"empty tracking while {len(ours)} {SOURCE_TAG} ticker(s) are still "
            f"under horizon ({', '.join(sorted(ours))}) - no-op, not a purge"
        )
        logger.warning("v5 bridge: %s", anomaly)
        return _result([], [], len(ours), [anomaly])

    keep = {symbol for symbol, days in tracked.items() if days < HORIZON_DAYS}
    removed = sorted(ours - keep)  # J+63 reached, or gone from the journal
    room = max(cap - len(ours - set(removed)), 0)
    candidates = sorted(keep - present)
    added, excluded = candidates[:room], candidates[room:]

    anomalies = []
    if excluded:
        anomaly = f"cap of {cap} bridged tickers reached, excluded: {', '.join(excluded)}"
        logger.warning("v5 bridge: %s", anomaly)
        anomalies.append(anomaly)

    if added or removed:
        drop = set(removed)
        # Both conditions, always: a symbol is only ever dropped when it carries
        # our own provenance tag. A manual entry sharing the symbol stays.
        data["tickers"] = [
            t for t in entries
            if not (_symbol(t) in drop and t.get("source") == SOURCE_TAG)
        ]
        data["tickers"].extend(
            {"symbol": symbol, "added": _today(), "source": SOURCE_TAG} for symbol in added
        )
        # Memorised only on an APPLIED snapshot: a fresh snapshot that changes
        # nothing must leave the file byte-identical (conditional write).
        if scanned_at is not None:
            data[SCANNED_AT_KEY] = scanned_at.isoformat()
        _save(str(path), data)

    return _result(added, removed, len(keep & present), anomalies)


def run(
    *,
    api_url: str | None = None,
    snapshot_path: str | os.PathLike | None = None,
    watchlist_path: str | os.PathLike | None = None,
    cap: int = DEFAULT_CAP,
) -> dict:
    """Read the scan snapshot and reconcile the watchlist with its v5 journal."""
    payload = fetch_payload(api_url=api_url, snapshot_path=snapshot_path)
    if payload is None:
        return _result([], [], 0, ["v5 snapshot unavailable"])
    tracked = parse_tracking(payload)
    if tracked is None:
        return _result([], [], 0, ["v5 tracking unavailable"])
    return reconcile(
        tracked,
        scanned_at=_scanned_at(payload),
        watchlist_path=watchlist_path,
        cap=cap,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
