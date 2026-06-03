---
name: modifyportfolio
description: Add or remove tickers from the stock-tracker Portfolio (daily briefing).
metadata:
  openclaw:
    emoji: 💼
---

# Skill: modifyportfolio

Manage the **Portfolio** tickers tracked by the daily stock briefing.
The list lives in `/opt/apps/stock-tracker/portfolio.json` and is read every
weekday morning by the n8n workflow.

## When invoked

The user sent `/modifyportfolio` (optionally with arguments).

1. Determine the intent from the message:
   - `add SYMBOL`    → add a ticker
   - `remove SYMBOL` → remove a ticker
   - empty / unclear → ask: "➕ Ajouter ou ➖ Retirer ? Quel symbole (ex. AAPL) ?"
2. Run the management script — it edits the live file atomically and dedupes:

   ```bash
   python3 /opt/apps/stock-tracker/agents/warren/manage_tickers.py portfolio add SYMBOL [--name "Full Name"] [--sector "Secteur"]
   python3 /opt/apps/stock-tracker/agents/warren/manage_tickers.py portfolio remove SYMBOL
   python3 /opt/apps/stock-tracker/agents/warren/manage_tickers.py portfolio list
   ```
   - Always uppercase the symbol.
   - When adding, you may fill `--name` (company name) and `--sector` (short FR label)
     if you know them; otherwise omit.
3. Relay the script output verbatim to the user (it already carries ✅/⚠️ and the updated list).

## Rules
- This skill touches the **portfolio only**. Watchlist → `/modifywatchlist`.
- Never hand-edit the JSON; always use the script (atomic write + dedupe + correct shape).
- Several symbols in one request → call the script once per symbol.
