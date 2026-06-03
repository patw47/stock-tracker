from __future__ import annotations

import logging

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from agents.warren.ticker_storage import add_ticker, load_list, remove_ticker

logger = logging.getLogger(__name__)

CHOOSE_ACTION = 0
AWAIT_TICKER = 1

_DISPLAY_NAMES: dict[str, str] = {
    "watchlist": "Watchlist",
    "portfolio": "Portfolio",
}

_BOT_COMMANDS: list[BotCommand] = [
    BotCommand("modifywatchlist", "➕/➖ Add or remove tickers from your Watchlist"),
    BotCommand("modifyportfolio", "➕/➖ Add or remove tickers from your Portfolio"),
    BotCommand("cancel", "❌ Cancel current operation"),
]


def _make_handler(list_name: str) -> ConversationHandler:
    """Build a ConversationHandler for add/remove flow on a named ticker list."""
    display = _DISPLAY_NAMES.get(list_name, list_name.capitalize())

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        keyboard = [[
            InlineKeyboardButton("➕ Add", callback_data="add"),
            InlineKeyboardButton("➖ Remove", callback_data="remove"),
        ]]
        context.user_data["list_name"] = list_name
        await update.message.reply_text(
            f"📊 Manage your *{display}* — choose an action:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
        return CHOOSE_ACTION

    async def choose_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        context.user_data["action"] = query.data
        await query.edit_message_text("Please type the ticker symbol ✏️")
        return AWAIT_TICKER

    async def receive_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        ticker = update.message.text.strip().upper()
        name: str = context.user_data.get("list_name", list_name)
        action: str = context.user_data.get("action", "add")
        dname = _DISPLAY_NAMES.get(name, name.capitalize())

        if action == "add":
            updated = add_ticker(name, ticker)
            header = f"✅ *{ticker}* added to your {dname}!"
        else:
            updated, found = remove_ticker(name, ticker)
            if not found:
                await update.message.reply_text(
                    f"⚠️ *{ticker}* was not found in your {dname}.",
                    parse_mode="Markdown",
                )
                return ConversationHandler.END
            header = f"✅ *{ticker}* removed from your {dname}!"

        bullets = "\n".join(f"• {t}" for t in updated) if updated else "_empty_"
        await update.message.reply_text(
            f"{header}\n\n📋 Current {dname}:\n{bullets}",
            parse_mode="Markdown",
        )
        return ConversationHandler.END

    async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text("❌ Operation cancelled.")
        return ConversationHandler.END

    return ConversationHandler(
        entry_points=[CommandHandler(f"modify{list_name}", start)],
        states={
            CHOOSE_ACTION: [CallbackQueryHandler(choose_action, pattern="^(add|remove)$")],
            AWAIT_TICKER: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_ticker)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )


async def _set_commands(app: Application) -> None:
    await app.bot.set_my_commands(_BOT_COMMANDS)


def register_list_handlers(application: Application) -> None:
    """Attach watchlist and portfolio ConversationHandlers to the application."""
    application.add_handler(_make_handler("watchlist"))
    application.add_handler(_make_handler("portfolio"))
    application.post_init = _set_commands
