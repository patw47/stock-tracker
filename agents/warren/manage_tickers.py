#!/usr/bin/env python3
"""Modify watchlist/portfolio via Telegram inline buttons (Warren / OpenClaw).

Same pattern as property-cm's social_draft.py: all Telegram I/O goes straight to the
Bot API (sendMessage with inline_keyboard). OpenClaw owns the bot and auto-answers the
callback; an unrecognized button press is forwarded to the agent as a normal incoming
message carrying the callback_data, which the SKILL routes here via --handle-callback.

Edits the ROOT files the n8n "Read Tickers" node reads:
  /opt/apps/stock-tracker/watchlist.json
  /opt/apps/stock-tracker/portfolio.json
Shape: {"tickers": [{"symbol": "NVDA", "added": "2026-06-03", ...}], "updated_at": "..."}

Callback namespace (per list, so each skill owns its prefix):
  watchlist -> wl:   portfolio -> pf:
  <p>:add            open the "add" flow (prompt for symbols, captured as free text)
  <p>:rem            open the "remove" flow (tickers as toggle buttons + Validate)
  <p>:tog:SYMBOL     toggle a symbol in the remove selection
  <p>:val            apply the pending removal
  <p>:x              cancel the current flow
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

REPO = os.environ.get("STOCK_TRACKER_DIR", "/opt/apps/stock-tracker")
FILES = {
    "watchlist": os.path.join(REPO, "watchlist.json"),
    "portfolio": os.path.join(REPO, "portfolio.json"),
}
OPENCLAW_CONFIG = os.environ.get(
    "OPENCLAW_CONFIG_PATH", "/home/warren/.openclaw/openclaw.json"
)
STATE_DB = os.environ.get(
    "TICKER_STATE_DB", "/home/warren/.openclaw/workspace-warren/.ticker-state.db"
)

PREFIX_TO_LIST = {"wl": "watchlist", "pf": "portfolio"}
LIST_TO_PREFIX = {"watchlist": "wl", "portfolio": "pf"}
LABEL = {"watchlist": "Watchlist", "portfolio": "Portfolio"}
EMOJI = {"watchlist": "📋", "portfolio": "💼"}


# ── Telegram (direct Bot API) ───────────────────────────────────────────────

def _load_tg() -> tuple[str, int]:
    with open(OPENCLAW_CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    token = cfg["channels"]["telegram"]["accounts"]["default"]["botToken"]
    chat = int(cfg["commands"]["ownerAllowFrom"][0].split(":")[1])
    return token, chat


def _tg(method: str, payload: dict) -> dict | None:
    body = json.dumps(payload).encode()
    req = Request(
        f"https://api.telegram.org/bot{TOKEN}/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except URLError as e:
        print(f"[TG ERROR] {method}: {e}", file=sys.stderr)
        return None


def send(text: str, keyboard: list | None = None) -> dict | None:
    payload: dict = {"chat_id": CHAT, "text": text, "parse_mode": "HTML"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return _tg("sendMessage", payload)


# ── Ticker files ────────────────────────────────────────────────────────────

def _load(path: str) -> dict:
    if not os.path.exists(path):
        return {"tickers": []}
    data = json.load(open(path, encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tickers"), list):
        raise SystemExit(f"ERROR: {path} is not in {{'tickers':[...]}} shape")
    return data


def _save(path: str, data: dict) -> None:
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _symbols(data: dict) -> list[str]:
    return [str(t.get("symbol", "")).upper() for t in data["tickers"]]


def _summary(data: dict) -> str:
    syms = _symbols(data)
    return ", ".join(syms) if syms else "(empty)"


# ── Pending-flow state (single owner) ───────────────────────────────────────

def _db() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(STATE_DB), exist_ok=True)
    c = sqlite3.connect(STATE_DB)
    c.execute(
        "CREATE TABLE IF NOT EXISTS pending "
        "(k TEXT PRIMARY KEY, list TEXT, mode TEXT, selected TEXT, updated_at TEXT)"
    )
    c.commit()
    return c


def set_pending(list_name: str, mode: str, selected: list | None = None) -> None:
    c = _db()
    c.execute(
        "INSERT OR REPLACE INTO pending (k,list,mode,selected,updated_at) "
        "VALUES ('owner',?,?,?,?)",
        (list_name, mode, json.dumps(selected or []), datetime.now(timezone.utc).isoformat()),
    )
    c.commit()
    c.close()


def get_pending() -> dict | None:
    c = _db()
    row = c.execute("SELECT list,mode,selected FROM pending WHERE k='owner'").fetchone()
    c.close()
    if not row:
        return None
    return {"list": row[0], "mode": row[1], "selected": json.loads(row[2] or "[]")}


def clear_pending() -> None:
    c = _db()
    c.execute("DELETE FROM pending WHERE k='owner'")
    c.commit()
    c.close()


# ── Keyboards ───────────────────────────────────────────────────────────────

def _menu_keyboard(list_name: str) -> list:
    p = LIST_TO_PREFIX[list_name]
    return [[
        {"text": "➕ Add a ticker", "callback_data": f"{p}:add"},
        {"text": "➖ Remove a ticker", "callback_data": f"{p}:rem"},
    ]]


def _remove_keyboard(list_name: str, selected: list) -> list:
    p = LIST_TO_PREFIX[list_name]
    syms = _symbols(_load(FILES[list_name]))
    rows, row = [], []
    for s in syms:
        mark = "✅ " if s in selected else ""
        row.append({"text": f"{mark}{s}", "callback_data": f"{p}:tog:{s}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        {"text": "✅ Validate", "callback_data": f"{p}:val"},
        {"text": "⛔ Cancel", "callback_data": f"{p}:x"},
    ])
    return rows


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_menu(list_name: str) -> None:
    clear_pending()
    send(
        f"{EMOJI[list_name]} <b>{LABEL[list_name]}</b> — what would you like to do?",
        keyboard=_menu_keyboard(list_name),
    )


def _today() -> str:
    return date.today().isoformat()


def _do_add(list_name: str, raw: str) -> None:
    syms = [x.upper() for x in re.split(r"[,\s]+", raw.strip()) if x]
    if not syms:
        send("⚠️ No symbol detected. Send e.g. <code>NVDA, MSFT</code>.")
        return
    data = _load(FILES[list_name])
    existing = set(_symbols(data))
    added, skipped = [], []
    for s in syms:
        if s in existing:
            skipped.append(s)
        else:
            data["tickers"].append({"symbol": s, "added": _today()})
            existing.add(s)
            added.append(s)
    if added:
        _save(FILES[list_name], data)
    clear_pending()
    parts = []
    if added:
        parts.append(f"✅ <b>{', '.join(added)}</b> added to {LABEL[list_name]} ({_today()})")
    if skipped:
        parts.append(f"⚠️ already present: {', '.join(skipped)}")
    parts.append(f"\n{EMOJI[list_name]} {LABEL[list_name]}: {_summary(data)}")
    send("\n".join(parts))


def _do_remove(list_name: str, selected: list) -> None:
    data = _load(FILES[list_name])
    sel = {s.upper() for s in selected}
    before = _symbols(data)
    removed = [s for s in before if s in sel]
    if not removed:
        clear_pending()
        send("⚠️ Nothing selected — nothing removed.")
        return
    data["tickers"] = [t for t in data["tickers"] if str(t.get("symbol", "")).upper() not in sel]
    _save(FILES[list_name], data)
    clear_pending()
    send(
        f"✅ <b>{', '.join(removed)}</b> removed from {LABEL[list_name]} ({_today()})\n"
        f"\n{EMOJI[list_name]} {LABEL[list_name]}: {_summary(data)}"
    )


def cmd_handle_callback(data: str) -> None:
    parts = data.strip().split(":")
    if len(parts) < 2 or parts[0] not in PREFIX_TO_LIST:
        print(f"NOT_A_TICKER_CALLBACK: {data!r}", file=sys.stderr)
        return
    list_name = PREFIX_TO_LIST[parts[0]]
    action = parts[1]

    if action == "add":
        set_pending(list_name, "add")
        send(
            f"➕ Which ticker(s) to add to <b>{LABEL[list_name]}</b>?\n"
            f"Type the symbol(s), e.g. <code>NVDA, MSFT</code>."
        )

    elif action == "rem":
        set_pending(list_name, "remove", [])
        data = _load(FILES[list_name])
        if not _symbols(data):
            clear_pending()
            send(f"{EMOJI[list_name]} {LABEL[list_name]} is empty — nothing to remove.")
            return
        send(
            f"➖ Tap the ticker(s) to remove from <b>{LABEL[list_name]}</b>, then ✅ Validate:",
            keyboard=_remove_keyboard(list_name, []),
        )

    elif action == "tog" and len(parts) >= 3:
        sym = parts[2].upper()
        pend = get_pending() or {"list": list_name, "mode": "remove", "selected": []}
        selected = pend.get("selected", [])
        if sym in selected:
            selected.remove(sym)
        else:
            selected.append(sym)
        set_pending(list_name, "remove", selected)
        chosen = ", ".join(selected) if selected else "(none)"
        send(
            f"Selected: <b>{chosen}</b>\nTap more or press ✅ Validate:",
            keyboard=_remove_keyboard(list_name, selected),
        )

    elif action == "val":
        pend = get_pending()
        selected = pend.get("selected", []) if pend else []
        _do_remove(list_name, selected)

    elif action == "x":
        clear_pending()
        send("⛔ Cancelled. Nothing changed.")

    else:
        print(f"UNHANDLED_CALLBACK: {data!r}", file=sys.stderr)


def cmd_add_text(value: str) -> None:
    pend = get_pending()
    if not pend or pend.get("mode") != "add":
        print("NO_PENDING_ADD")
        return
    _do_add(pend["list"], value)
    print("HANDLED")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Telegram inline-button ticker manager.")
    p.add_argument("--menu", choices=sorted(FILES), help="send the add/remove menu")
    p.add_argument("--handle-callback", action="store_true")
    p.add_argument("--add-text", action="store_true")
    p.add_argument("--get-pending", action="store_true")
    p.add_argument("--show", choices=sorted(FILES), help="print a list (testing)")
    p.add_argument("--data", default="", help="callback_data for --handle-callback")
    p.add_argument("--value", default="", help="free text for --add-text")
    args = p.parse_args()

    if args.show:
        print(f"{args.show}: {_summary(_load(FILES[args.show]))}")
        return 0
    if args.get_pending:
        print(json.dumps(get_pending() or {}, ensure_ascii=False))
        return 0

    # the rest need Telegram config
    global TOKEN, CHAT
    TOKEN, CHAT = _load_tg()

    if args.menu:
        cmd_menu(args.menu)
    elif args.handle_callback:
        cmd_handle_callback(args.data)
    elif args.add_text:
        cmd_add_text(args.value)
    else:
        p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
