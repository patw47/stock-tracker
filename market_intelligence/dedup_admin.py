"""Admin CLI for the Sprint 5 dedup state file.

Inspect or re-arm the persistent hysteresis state without ad-hoc JSON edits on
the VPS. Post-incident use: when a latch is polluted (a ticker marked "alerted"
without a Telegram message actually leaving — see the 2026-07-01 incident and the
ADR ``decisions/2026-07-02_dedup-transactionnel.md``), run
``python3 -m market_intelligence.dedup_admin reset --ticker SYMBOL`` before the
evening run so the real alert can fire again.

All writes go through ``save_dedup_state`` (atomic temp+replace) under the same
``_state_lock`` flock as the pipeline, and a corrupt/invalid state file is
refused (non-zero exit) without any write.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from market_intelligence.dedup_hysteresis import (
    _DEFAULT_STATE_PATH,
    _REPO_ROOT,
    DedupStateError,
    TickerDedupState,
    _pending_path_for,
    _state_lock,
    load_dedup_state,
    load_pending_state,
    save_dedup_state,
)

_STATE_PATH_ENV_VAR = "ANOMALY_DEDUP_STATE_PATH"


def _expand_state_path(value: str) -> Path:
    """Expand a path value; a relative path is anchored at the repo root.

    Matches ``dedup_hysteresis`` env resolution so a relative value means the
    same file whatever the working directory — both for ``--state-path`` and
    ``ANOMALY_DEDUP_STATE_PATH`` (avoids silently targeting the wrong file).
    """
    path = Path(value).expanduser()
    return path if path.is_absolute() else _REPO_ROOT / path


def _resolve_state_path(arg: str | None) -> Path:
    """Resolve the state path at call time (arg > env > frozen default)."""
    if arg:
        return _expand_state_path(arg)
    env_value = os.getenv(_STATE_PATH_ENV_VAR)
    if env_value:
        return _expand_state_path(env_value)
    return _DEFAULT_STATE_PATH


def _format_ticker(ticker: str, state: TickerDedupState) -> str:
    status = "latched" if state.latched else "armed"
    signals = ",".join(state.seen_signal_types) or "-"
    return (
        f"{ticker}: {status} "
        f"direction={state.direction or '-'} "
        f"last_alert={state.last_alert_as_of or '-'} "
        f"last_observed={state.last_observed_as_of} "
        f"latched_since={state.latched_since or '-'} "
        f"observations={state.latch_observations} "
        f"signals={signals}"
    )


def _show(path: Path, out) -> int:
    states = load_dedup_state(path)
    if not states:
        print(f"No dedup state (0 tickers) at {path}", file=out)
        return 0
    print(f"Dedup state at {path} ({len(states)} tickers):", file=out)
    for ticker in sorted(states):
        print("  " + _format_ticker(ticker, states[ticker]), file=out)
    return 0


def _reset(path: Path, *, ticker: str | None, reset_all: bool, out) -> int:
    with _state_lock(path):
        states = load_dedup_state(path)
        if reset_all:
            removed = len(states)
            save_dedup_state({}, path)
            print(f"Re-armed all tickers (cleared {removed} entries) at {path}", file=out)
            return 0
        assert ticker is not None
        if ticker not in states:
            print(f"{ticker} not present in dedup state; nothing to reset", file=out)
            return 0
        del states[ticker]
        save_dedup_state(states, path)
        print(f"Re-armed {ticker} (removed latch); {len(states)} tickers remain", file=out)
        return 0


def _commit(path: Path, pending_path: Path, *, run_id: str, out) -> int:
    with _state_lock(path):
        if not pending_path.exists():
            print(
                f"No pending state at {pending_path}; nothing to commit", file=out
            )
            return 0
        pending = load_pending_state(pending_path)
        if pending.run_id != run_id:
            print(
                f"error: pending run_id {pending.run_id!r} does not match "
                f"requested {run_id!r}; refusing to commit",
                file=sys.stderr,
            )
            return 1
        save_dedup_state(pending.tickers, path)
        pending_path.unlink(missing_ok=True)
        print(
            f"Committed run_id={run_id} ({len(pending.tickers)} tickers) to {path}",
            file=out,
        )
        return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m market_intelligence.dedup_admin",
        description="Inspect or re-arm the dedup hysteresis state.",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--state-path",
        default=None,
        help="Path to dedup_state.json (default: ANOMALY_DEDUP_STATE_PATH env or runtime path).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "show", parents=[common], help="Print the dedup state per ticker."
    )

    reset = subparsers.add_parser(
        "reset", parents=[common], help="Re-arm one ticker or all tickers."
    )
    target = reset.add_mutually_exclusive_group(required=True)
    target.add_argument("--ticker", help="Re-arm a single ticker symbol.")
    target.add_argument(
        "--all", action="store_true", dest="reset_all", help="Re-arm every ticker."
    )

    commit = subparsers.add_parser(
        "commit",
        parents=[common],
        help="Promote a pending dedup state to the real state (two-phase).",
    )
    commit.add_argument(
        "--run-id", required=True, help="Run id the pending state must match."
    )
    commit.add_argument(
        "--pending-path",
        default=None,
        help="Pending state path (default: <state-path> with .pending.json).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the dedup admin CLI. Returns a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    path = _resolve_state_path(args.state_path)
    try:
        if args.command == "show":
            return _show(path, sys.stdout)
        if args.command == "reset":
            return _reset(
                path, ticker=args.ticker, reset_all=args.reset_all, out=sys.stdout
            )
        if args.command == "commit":
            pending_path = (
                _expand_state_path(args.pending_path)
                if args.pending_path
                else _pending_path_for(path)
            )
            return _commit(path, pending_path, run_id=args.run_id, out=sys.stdout)
    except DedupStateError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    parser.error("unknown command")  # pragma: no cover - argparse guards this
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
