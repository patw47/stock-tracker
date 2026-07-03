---
name: modifyportfolio
description: Modify the stock-tracker Portfolio from Telegram. Triggered by /modifyportfolio, by any incoming message starting with `pf:` or `wl:` (inline-button callback), or by free text right after an "add" prompt. All Telegram I/O (inline buttons) goes through manage_tickers.py. Stateless — each button press returns as a `pf:`/`wl:` message.
metadata:
  openclaw:
    emoji: 💼
    requires:
      bins: ["python3"]
---

# modifyportfolio

Add or remove tickers from the **Portfolio** read every morning by the n8n briefing
(`/opt/apps/stock-tracker/portfolio.json`). All Telegram I/O — the menu, inline buttons,
confirmations — goes through the script (direct Bot API). Stateless: each button press
comes back as an incoming message and is routed here.

> `MT` = `python3 /opt/apps/stock-tracker/agents/warren/manage_tickers.py`

## Routing — DO THIS FIRST for every incoming message

1. **Message is the `/modifyportfolio` command** → open the menu:
   ```bash
   MT --menu portfolio
   ```
   Then reply `NO_REPLY` (the script already sent the menu with buttons).

2. **Message starts with `pf:` or `wl:`** → it is an inline-button callback:
   ```bash
   MT --handle-callback --data "<exact message>"
   ```
   Then reply `NO_REPLY`. The script sends the next step (prompt, toggle keyboard,
   or confirmation). Do not add anything.

3. **Any other free text** → check for a pending "add":
   ```bash
   MT --get-pending
   ```
   - Output `{"mode":"add", ...}` → this text is the ticker(s) to add:
     ```bash
     MT --add-text --value "<exact message>"
     ```
     Then reply `NO_REPLY` (the script confirmed + updated the file).
   - Output `{}` or any other mode → no pending add → handle the message normally.

## Flow (what the buttons do — handled by the script)

1. `/modifyportfolio` → "What would you like to do?" + **[➕ Add a ticker] [➖ Remove a ticker]**.
2. **Add** (`pf:add`) → prompt for symbol(s); the user types e.g. `NVDA, MSFT` → each symbol is
   validated and **onboarded** into the Layer B referentials (registry, `speculative`
   classification, `single_factor_symbols`) with safe defaults, the file is updated with each
   ticker and its `added` date → ✅ confirmation listing what was generated and any manual
   follow-up (sector-factor ETF to choose in a PR). An invalid symbol is refused, nothing written.
3. **Remove** (`pf:rem`) → current tickers shown as toggle buttons + **✅ Validate / ⛔ Cancel**;
   tapping toggles selection (`pf:tog:SYMBOL`), **✅ Validate** (`pf:val`) removes them and
   stamps the file → ✅ confirmation.

## Rules
- Never hand-edit the JSON — always go through `MT` (atomic write, dedupe, date stamp,
  correct `{tickers:[...]}` shape).
- After any `--handle-callback` / `--add-text`, reply `NO_REPLY` — the script owns the
  Telegram messages; do not duplicate them.
- This command is for the **Portfolio**. Watchlist → `/modifywatchlist` (same engine).
- Onboarding uses safe defaults only (`speculative` + `single_factor`); it never guesses a
  sector-factor ETF mapping and never commits/pushes from the VPS — reconciling the modified
  `market_intelligence/data/` files to git stays manual (the reply lists the diff to report).
