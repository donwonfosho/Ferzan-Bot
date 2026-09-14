"""
launch_bot.py

The Telegram-facing half of the launch bot. Walks the user through:
  chain selection -> mode selection -> token details (name/symbol/supply)
-> creates a launch_request in the shared DB -> opens the Mini App with
that request's ID, where the actual wallet connection and signing happen.

This process and api.py should run as two separate services (see
DEPLOYMENT notes) -- they share state only through launch_bot_db.py's
SQLite file, not through any direct function calls, so either can be
restarted independently.
"""

import logging
import os

from dotenv import load_dotenv
from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

load_dotenv("/opt/ferzan/.env")
load_dotenv()
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, filters, ContextTypes,
)

import launch_bot_db as db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Your Mini App's public HTTPS URL (see DEPLOYMENT notes -- this must be
# HTTPS, Telegram will refuse a plain-http web_app URL).
MINI_APP_BASE_URL = os.environ.get("MINI_APP_BASE_URL", "https://yourdomain.com/miniapp")
TRADE = (os.environ.get("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")
LIQ = (os.environ.get("FERZAN_LIQ_BOT") or "FerzanLiqBot").lstrip("@")
BUY = (os.environ.get("FERZAN_BUY_BOT") or "Ferzan_Buy_Bot").lstrip("@")
CHAT = os.environ.get("FERZAN_CHAT_URL") or "https://t.me/Ferzan_Chat"

CHAINS = {
    "ethereum": "Ethereum",
    "bsc": "BNB Chain",
    "base": "Base",
    "robinhood": "Robinhood Chain (HOOD)",
    "solana": "Solana",
}

EVM_CHAINS = {"ethereum", "bsc", "base", "robinhood"}

# Conversation states
CHOOSING_CHAIN, CHOOSING_MODE, ENTERING_NAME, ENTERING_SYMBOL, ENTERING_SUPPLY, CONFIRMING = range(6)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🚀 Launch a token", callback_data="go:launch")],
            [InlineKeyboardButton("📜 My launches", callback_data="go:history")],
            [
                InlineKeyboardButton("⚡ Trade", url=f"https://t.me/{TRADE}"),
                InlineKeyboardButton("💧 Liq", url=f"https://t.me/{LIQ}"),
            ],
            [InlineKeyboardButton("🟢 Buy alerts", url=f"https://t.me/{BUY}")],
            [InlineKeyboardButton("💬 Community", url=CHAT)],
        ]
    )
    await update.effective_message.reply_text(
        "🚀 <b>Ferzan Launch</b>\n\n"
        "Create a token from Telegram. You sign in your own wallet — "
        "this bot never holds keys.\n\n"
        "Chains: Solana · ETH · BNB · Base · Hood\n"
        "Modes: plain mint, or bonding curve / pump.fun where live.\n\n"
        "Tap Launch. Review the tx in the wallet before you approve.\n"
        "Platform fee is disclosed in the review screen when that path is live.",
        parse_mode="HTML",
        reply_markup=kb,
    )


async def go_launch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"chain:{key}")]
        for key, label in CHAINS.items()
    ]
    await q.edit_message_text(
        "Which chain do you want to launch on?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return CHOOSING_CHAIN


async def launch_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    buttons = [
        [InlineKeyboardButton(label, callback_data=f"chain:{key}")]
        for key, label in CHAINS.items()
    ]
    await update.message.reply_text(
        "Which chain do you want to launch on?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return CHOOSING_CHAIN


async def chain_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chain = query.data.split(":", 1)[1]
    context.user_data["launch"] = {"chain": chain}

    if chain == "solana":
        modes = [("plain", "Plain token launch"), ("pumpfun", "pump.fun (bonding curve + trading)")]
    else:
        modes = [("plain", "Plain token launch"), ("bonding_curve", "Bonding curve (pump.fun-style, on your own chain)")]

    buttons = [[InlineKeyboardButton(label, callback_data=f"mode:{key}")] for key, label in modes]
    await query.edit_message_text(
        f"Launching on *{CHAINS[chain]}*. Pick a launch type:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return CHOOSING_MODE


async def mode_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    context.user_data["launch"]["mode"] = mode

    await query.edit_message_text("What's the token's name? (e.g. \"My Cool Token\")")
    return ENTERING_NAME


async def name_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["launch"]["name"] = update.message.text.strip()
    await update.message.reply_text("What's the ticker symbol? (e.g. \"MCT\")")
    return ENTERING_SYMBOL


async def symbol_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["launch"]["symbol"] = update.message.text.strip().upper()
    await update.message.reply_text(
        "Total supply? Enter a plain number (e.g. \"1000000000\" for 1 billion). "
        "This is the whole-token amount -- decimal scaling is handled for you."
    )
    return ENTERING_SUPPLY


async def supply_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    if not text.isdigit():
        await update.message.reply_text("That doesn't look like a plain number -- try again.")
        return ENTERING_SUPPLY

    launch = context.user_data["launch"]
    decimals = 18 if launch["chain"] != "solana" else 6  # Solana tokens conventionally use 6, but this is a choice, not a rule
    launch["total_supply_raw"] = str(int(text) * (10 ** decimals))
    launch["decimals"] = decimals

    summary = (
        f"*Review your launch:*\n"
        f"Chain: {CHAINS[launch['chain']]}\n"
        f"Mode: {launch['mode']}\n"
        f"Name: {launch['name']}\n"
        f"Symbol: {launch['symbol']}\n"
        f"Supply: {text}\n\n"
        "Confirm to open the wallet-connect screen and review the exact "
        "transaction before signing anything."
    )
    buttons = [[
        InlineKeyboardButton("✅ Confirm", callback_data="confirm:yes"),
        InlineKeyboardButton("❌ Cancel", callback_data="confirm:no"),
    ]]
    await update.message.reply_text(summary, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")
    return CONFIRMING


async def confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm:no":
        await query.edit_message_text("Cancelled.")
        context.user_data.pop("launch", None)
        return ConversationHandler.END

    launch = context.user_data["launch"]
    req = db.create_launch_request(
        telegram_user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        chain=launch["chain"],
        mode=launch["mode"],
        name=launch["name"],
        symbol=launch["symbol"],
        total_supply=launch["total_supply_raw"],
        decimals=launch["decimals"],
        extra_params=launch.get("extra_params", {}),
    )

    # Route to the chain-appropriate Mini App page -- kept as separate
    # pages (evm.html / solana.html) rather than one universal page that
    # switches wallet adapters mid-session, which avoids a known bug
    # class in multichain wallet-connect libraries.
    page = "evm.html" if launch["chain"] != "solana" else "solana.html"
    mini_app_url = f"{MINI_APP_BASE_URL}/{page}?request_id={req.id}"

    buttons = [[InlineKeyboardButton("🔗 Connect Wallet & Launch", web_app=WebAppInfo(url=mini_app_url))]]
    await query.edit_message_text(
        "Tap below to connect your wallet and review the exact transaction "
        "before signing. Nothing is sent until you approve it in your own wallet.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

    context.user_data.pop("launch", None)
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("launch", None)
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launches = db.get_user_launch_history(update.effective_user.id)
    target = update.effective_message
    if not launches:
        await target.reply_text("No launches yet — try /launch.")
        return
    lines = ["*Your launches:*"]
    for row in launches:
        status_emoji = {"confirmed": "✅", "failed": "❌", "pending": "⏳", "built": "⏳"}.get(row.status, "•")
        lines.append(f"{status_emoji} {row.name} ({row.symbol}) on {row.chain} — {row.status}")
    await target.reply_text("\n".join(lines), parse_mode="Markdown")


async def go_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await history(update, context)


def main():
    token = (
        os.environ.get("LAUNCHBOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    if not token:
        raise SystemExit("Set LAUNCHBOT_TOKEN in /opt/ferzan/.env")

    db.init_db()
    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("launch", launch_start),
            CallbackQueryHandler(go_launch, pattern="^go:launch$"),
        ],
        states={
            CHOOSING_CHAIN: [CallbackQueryHandler(chain_chosen, pattern="^chain:")],
            CHOOSING_MODE: [CallbackQueryHandler(mode_chosen, pattern="^mode:")],
            ENTERING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, name_entered)],
            ENTERING_SYMBOL: [MessageHandler(filters.TEXT & ~filters.COMMAND, symbol_entered)],
            ENTERING_SUPPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, supply_entered)],
            CONFIRMING: [CallbackQueryHandler(confirmed, pattern="^confirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(conv)
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CallbackQueryHandler(go_history, pattern="^go:history$"))

    async def _post(application):
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Ferzan Launch home"),
                BotCommand("launch", "Launch a token"),
                BotCommand("history", "Your launches"),
                BotCommand("cancel", "Cancel launch"),
            ]
        )

    app.post_init = _post
    logger.info("Ferzan Launch starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
