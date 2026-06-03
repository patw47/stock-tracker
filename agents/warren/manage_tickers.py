#!/usr/bin/env python3
"""Add/remove/list tickers in the stock-tracker root list files.

These are the exact files the n8n "Read Tickers" Code node reads each morning:
  /opt/apps/stock-tracker/watchlist.json
  /opt/apps/stock-tracker/portfolio.json
Shape: {"tickers": [{"symbol": "AAPL", "name": "...", "sector": "..."}]}

Driven by the OpenClaw "modifywatchlist" / "modifyportfolio" skills (Warren agent).
Deterministic, atomic write, dedupe by symbol (case-insensitive, stored uppercase).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(os.environ.get("STOCK_TRACKER_DIR", "/opt/apps/stock-tracker"))
FILES = {"watchlist": REPO / "watchlist.json", "portfolio": REPO / "portfolio.json"}


def _load(path: Path) -> dict:
    if not path.exists():
        return {"tickers": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tickers"), list):
        raise SystemExit(f"ERROR: {path} is not in {{tickers:[...]}} shape")
    return data


def _save(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _summary(data: dict) -> str:
    tickers = data["tickers"]
    if not tickers:
        return "(empty)"
    return ", ".join(str(t.get("symbol", "?")) for t in tickers)


def main() -> int:
    ap = argparse.ArgumentParser(description="Manage stock-tracker ticker lists.")
    ap.add_argument("list", choices=sorted(FILES))
    ap.add_argument("action", choices=["list", "add", "remove"])
    ap.add_argument("symbol", nargs="?", default="")
    ap.add_argument("--name", default="")
    ap.add_argument("--sector", default="")
    args = ap.parse_args()

    path = FILES[args.list]
    data = _load(path)
    tickers = data["tickers"]

    if args.action == "list":
        print(f"{args.list}: {_summary(data)}")
        return 0

    sym = args.symbol.strip().upper()
    if not sym:
        print("ERROR: a ticker symbol is required for add/remove")
        return 2

    idx = next(
        (i for i, t in enumerate(tickers) if str(t.get("symbol", "")).upper() == sym),
        None,
    )

    if args.action == "add":
        if idx is not None:
            print(f"⚠️ {sym} is already in {args.list}: {_summary(data)}")
            return 0
        entry: dict = {"symbol": sym}
        if args.name.strip():
            entry["name"] = args.name.strip()
        if args.sector.strip():
            entry["sector"] = args.sector.strip()
        tickers.append(entry)
        _save(path, data)
        print(f"✅ {sym} added to {args.list}: {_summary(data)}")
        return 0

    # remove
    if idx is None:
        print(f"⚠️ {sym} is not in {args.list}: {_summary(data)}")
        return 0
    tickers.pop(idx)
    _save(path, data)
    print(f"✅ {sym} removed from {args.list}: {_summary(data)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
