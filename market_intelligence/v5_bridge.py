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

Layer B (registry, classification, sector factors) follows the cohort since Epic
10 S2: whatever the reconciliation adds is onboarded, whatever it removes is
offboarded — but only when the symbol is gone from BOTH runtime lists, so
portfolio tickers are structurally protected.
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
from market_intelligence.registry_check import (
    CLASSIFICATIONS_PATH,
    REGISTRY_PATH,
    REPO_ROOT,
    SECTOR_FACTORS_PATH,
    SINGLE_FACTORS_PATH,
)
from market_intelligence.ticker_onboard import (
    ALREADY_PRESENT,
    ONBOARDED,
    offboard_ticker,
    onboard_ticker,
)

logger = logging.getLogger(__name__)

# The runtime list only, never the committed watchlist.example.json fallback that
# registry_check resolves to in a dev checkout: the bridge writes, and it must
# not touch a fixture — nor create the runtime file, which would shadow the
# example for the whole pipeline. Absent file = logged no-op.
WATCHLIST_PATH = REPO_ROOT / "watchlist.json"
# Same rule for the portfolio: the runtime file only. It is read, never written —
# it is the list that protects its tickers from being offboarded.
PORTFOLIO_PATH = REPO_ROOT / "portfolio.json"
SOURCE_TAG = "smallcaps-v5"
HORIZON_DAYS = 63  # v5 judgment horizon: a bridged ticker leaves at days_held >= 63
# Cohort context carried from the screener's tracking journal to the watchlist
# entry, and from there to the alert block (Epic 10 S3). Kept verbatim: the
# renderer formats, it never recomputes.
COHORT_KEY = "cohort"
COHORT_FIELDS = ("entry_date", "entry_price", "days_held", "days_left", "ret", "status")
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


def _cohort_context(row: dict, days: int) -> dict:
    """Keep the tracking fields the digest renders; drop everything else.

    Absent or null fields are simply absent from the context: a row may carry
    ``days_left: None`` before the first close after entry, and the renderer
    omits whatever is missing rather than printing a hole.
    """
    context = {
        field: row[field]
        for field in COHORT_FIELDS
        if row.get(field) is not None and row[field] != ""
    }
    context["days_held"] = days
    return context


def parse_tracking(payload: object) -> dict[str, dict] | None:
    """Cohort context per ticker over the union of the v5 tracking windows.

    A ticker present in several windows yields a single entry (the longest
    holding wins). Returns ``None`` when the payload is not usable at all, which
    the caller must treat as a no-op — never as an empty tracking journal.

    Since Epic 10 S3 the whole judgment context travels (``entry_date``,
    ``entry_price``, ``days_held``, ``days_left``, ``ret``, ``status``), not just
    ``days_held``: the alert must be able to say where the thesis stands on that
    ticker, and this journal is the only place that knows.

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
    tracked: dict[str, dict] = {}
    for row in _iter_rows(section):
        symbol = str(row.get("ticker", "")).strip().upper()
        try:
            days = int(row["days_held"])
        except (TypeError, ValueError):
            continue  # fail-soft: one bad row never sinks the run
        if not symbol:
            continue
        known = tracked.get(symbol)
        if known is None or days > known["days_held"]:
            tracked[symbol] = _cohort_context(row, days)
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


def _portfolio_symbols(portfolio_path: str | os.PathLike | None) -> set[str] | None:
    """Symbols held in the portfolio; ``None`` when the list cannot be read.

    ``None`` is not an empty portfolio: it means "I don't know what is held", and
    the caller must then offboard nothing. Treating an unreadable list as empty
    would strip the referentials of every held ticker on a transient glitch.
    """
    path = Path(portfolio_path or PORTFOLIO_PATH)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(t.get("symbol", "")).strip().upper() for t in data["tickers"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        logger.error("v5 bridge: portfolio %s unreadable (%s) - no offboarding", path, exc)
        return None


def sync_referential(
    added: list[str],
    removed: list[str],
    *,
    watchlist_symbols: set[str],
    portfolio_path: str | os.PathLike | None = None,
    referential_paths: dict[str, Path] | None = None,
) -> dict[str, int]:
    """Mirror the cohort into the Layer B referentials, one symbol at a time.

    ``onboard_ticker`` / ``offboard_ticker`` are reused as they are: default
    classification, factor coverage and symbol validation stay theirs. Nothing is
    offboarded while the symbol still sits in a runtime list — the same guard the
    Telegram remove flow applies, so portfolio tickers are structurally safe.

    Fail-soft per symbol: validating a symbol is a network call, and dozens run
    back to back 45 minutes before the EOD run. A failure is a logged skip for
    that symbol alone, never an exception that sinks the ones after it.
    """
    state = {
        "registry_path": Path(REGISTRY_PATH),
        "classifications_path": Path(CLASSIFICATIONS_PATH),
        "single_factors_path": Path(SINGLE_FACTORS_PATH),
    }
    state.update(referential_paths or {})
    # Onboarding also READS the sector map (versioned config) to know whether the
    # symbol already has an ETF factor; offboarding never touches it.
    onboard_paths = {"sector_factors_path": Path(SECTOR_FACTORS_PATH), **state}
    counts = {"onboarded": 0, "offboarded": 0, "skipped": 0, "failed": 0}

    for symbol in added:
        try:
            result = onboard_ticker(symbol, **onboard_paths)
        except Exception as exc:  # noqa: BLE001 - network/IO of one symbol only
            logger.error("v5 bridge: onboarding %s failed (%s) - skipped", symbol, exc)
            counts["failed"] += 1
            continue
        if result.status == ONBOARDED:
            counts["onboarded"] += 1
        elif result.status == ALREADY_PRESENT:
            counts["skipped"] += 1
        else:
            logger.warning("v5 bridge: onboarding %s refused (%s)", symbol, result.reason)
            counts["failed"] += 1

    held = _portfolio_symbols(portfolio_path)
    for symbol in removed:
        if held is None or symbol in watchlist_symbols or symbol in held:
            counts["skipped"] += 1
            continue
        try:
            offboard_ticker(symbol, **state)
        except Exception as exc:  # noqa: BLE001 - one symbol never sinks the run
            logger.error("v5 bridge: offboarding %s failed (%s) - skipped", symbol, exc)
            counts["failed"] += 1
            continue
        counts["offboarded"] += 1

    return counts


def _result(
    added: list[str],
    removed: list[str],
    unchanged: int,
    anomalies: list[str],
    referential: dict[str, int] | None = None,
) -> dict:
    """Log the run summary line and return it (orchestrator log style)."""
    referential = referential or {"onboarded": 0, "offboarded": 0, "skipped": 0, "failed": 0}
    logger.info(
        "v5 bridge run complete: added=%d removed=%d unchanged=%d anomalies=%d "
        "onboarded=%d offboarded=%d skipped=%d failed=%d",
        len(added),
        len(removed),
        unchanged,
        len(anomalies),
        referential["onboarded"],
        referential["offboarded"],
        referential["skipped"],
        referential["failed"],
    )
    return {
        "added": added,
        "removed": removed,
        "unchanged": unchanged,
        "anomalies": anomalies,
        "referential": referential,
    }


def reconcile(
    tracked: dict[str, int],
    *,
    scanned_at: datetime | None = None,
    watchlist_path: str | os.PathLike | None = None,
    portfolio_path: str | os.PathLike | None = None,
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

    keep = {symbol for symbol, context in tracked.items() if context["days_held"] < HORIZON_DAYS}
    removed = sorted(ours - keep)  # J+63 reached, or gone from the journal
    room = max(cap - len(ours - set(removed)), 0)
    candidates = sorted(keep - present)
    added, excluded = candidates[:room], candidates[room:]

    anomalies: list[str] = []
    referential = None
    if excluded:
        anomaly = f"cap of {cap} bridged tickers reached, excluded: {', '.join(excluded)}"
        logger.warning("v5 bridge: %s", anomaly)
        anomalies.append(anomaly)

    drop = set(removed)
    # Both conditions, always: a symbol is only ever dropped when it carries
    # our own provenance tag. A manual entry sharing the symbol stays.
    kept = [
        t for t in entries
        if not (_symbol(t) in drop and t.get("source") == SOURCE_TAG)
    ]
    # The cohort context ages one notch per trading day, so it is refreshed on the
    # entries we own — otherwise the alert would print a stale "day 27/63". The
    # write stays conditional: refreshed is False when the context is identical,
    # and an unchanged snapshot then leaves the file byte-identical.
    refreshed = False
    for entry in kept:
        context = tracked.get(_symbol(entry))
        if entry.get("source") == SOURCE_TAG and context and entry.get(COHORT_KEY) != context:
            entry[COHORT_KEY] = context
            refreshed = True

    if added or removed or refreshed:
        data["tickers"] = kept
        data["tickers"].extend(
            {
                "symbol": symbol, "added": _today(), "source": SOURCE_TAG,
                COHORT_KEY: tracked[symbol],
            }
            for symbol in added
        )
        # Memorised only on an APPLIED snapshot: a fresh snapshot that changes
        # nothing must leave the file byte-identical (conditional write).
        if scanned_at is not None:
            data[SCANNED_AT_KEY] = scanned_at.isoformat()
        _save(str(path), data)
    if added or removed:
        # The referential follows the cohort (Epic 10 S2). After the write, so a
        # symbol kept by a manual entry sharing the symbol still counts as present.
        referential = sync_referential(
            added,
            removed,
            watchlist_symbols={_symbol(t) for t in data["tickers"]},
            portfolio_path=portfolio_path,
        )

    return _result(added, removed, len(keep & present), anomalies, referential)


def run(
    *,
    api_url: str | None = None,
    snapshot_path: str | os.PathLike | None = None,
    watchlist_path: str | os.PathLike | None = None,
    portfolio_path: str | os.PathLike | None = None,
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
        portfolio_path=portfolio_path,
        cap=cap,
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
