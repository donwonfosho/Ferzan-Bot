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
LAUNCH_BANNER_FILE_ID = "AgACAgEAAxkBAAICkWqv9toLFcZR-OCvzlhSEVuNwf4MAAIPDWsbZJaARUPhbF_grfmtAQADAgADeQADPQQ"
LAUNCH_BANNER_FILE_ID = "AgACAgEAAxkBAAICkWqv9toLFcZR-OCvzlhSEVuNwf4MAAIPDWsbZJaARUPhbF_grfmtAQADAgADeQADPQQ"
TRADE = (os.environ.get("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")
LIQ = (os.environ.get("FERZAN_LIQ_BOT") or "FerzanLiqBot").lstrip("@")
BUY = (os.environ.get("FERZAN_BUY_BOT") or "Ferzan_Buy_Bot").lstrip("@")
CHAT = os.environ.get("FERZAN_CHAT_URL") or "https://t.me/Ferzan_Chat"

CHAINS = {
    "ethereum": "Ethereum",
    "bsc": "BNB Chain",
    "base": "Base",
    "robinhood": "Robinhood Chain (HOOD)",
    "arc": "Arc",
    "solana": "Solana",
    "tron": "Tron",
    "ton": "TON",
}

EVM_CHAINS = {"ethereum", "bsc", "base", "robinhood", "arc"}

CHOOSING_CHAIN, CHOOSING_MODE, ENTERING_NAME, ENTERING_SYMBOL, ENTERING_SUPPLY, ENTERING_GRAD, ENTERING_VETH, ENTERING_VTOKEN, ENTERING_ALLOCS, ENTERING_DEVBUY, ENTERING_WINDOW, CONFIRMING = range(12)
CURVE_MODES = {"bonding_curve", "meteora"}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if context.args:
        raw = (context.args[0] or "").replace("ref_", "").replace("ref", "")
        if raw.isdigit():
            if db.set_referrer(uid, int(raw)):
                await update.effective_message.reply_text("Referral locked. You launch, they earn a cut of curve fees.")
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
    await update.effective_message.reply_photo(
        photo=LAUNCH_BANNER_FILE_ID,
        caption=(

        "🚀 <b>Ferzan Launch</b>\n\n"
        "Create a token from Telegram. You sign in your own wallet — "
        "this bot never holds keys.\n\n"
        "Live now: ETH · BNB · Base · Hood · Solana\n"
        "Plain mint or EVM bonding curve. Fees go to Ferzan treasury.\n"
        "Next: Arc · Tron · TON · Meteora pool.\n\n"
        "Tap Launch. Review the tx in your wallet before you approve.\n"
        "Platform fee is shown on the review screen."
        ),
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
    try:
        await q.message.delete()
    except Exception:
        pass
    await context.bot.send_message(
        chat_id=q.message.chat_id,
        text="Which chain do you want to launch on?",
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
        modes = [
            ("plain", "🔸 Plain SPL — fixed supply, no fees"),
            ("meteora", "🚀 Meteora pool — bonding curve, fee on every trade"),
        ]
    elif chain == "tron":
        modes = [("plain", "TRC-20 — coming soon (no signable tx yet)")]
    elif chain == "ton":
        modes = [("plain", "TON — fee memo only, jetton minter coming soon")]
    elif chain == "arc":
        modes = [
            ("plain", "Arc plain — held (6-dec USDC gas, verify RPC first)"),
            ("bonding_curve", "Arc curve — held (no V2 router / Uniswap v4)"),
        ]
    else:
        modes = [
            ("plain", "Plain ERC-20 — fixed supply"),
            ("bonding_curve", "Bonding curve — Ferzan fee on every trade"),
        ]

    buttons = [[InlineKeyboardButton(label, callback_data=f"mode:{key}")] for key, label in modes]
    await query.edit_message_text(
        f"*Step 1/9 — Launch type*\n\nLaunching on *{CHAINS[chain]}*. Choose how your token works:\n\n"
        "🔸 *Plain SPL* — you mint a fixed supply once, no trading fee, no bonding curve.\n"
        "🚀 *Meteora pool* — launches on a bonding curve with a built-in trading fee that pays you as it trades.",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return CHOOSING_MODE


async def mode_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split(":", 1)[1]
    context.user_data["launch"]["mode"] = mode

    await query.edit_message_text("*Step 2/9 — Name*\n\nWhat should your token be called?\n"
        "This is the full display name people will see in wallets and explorers.\n"
        "Example: `My Cool Token`",
        parse_mode="Markdown")
    return ENTERING_NAME


async def name_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["launch"]["name"] = update.message.text.strip()
    await update.message.reply_text("*Step 3/9 — Ticker symbol*\n\nWhat's the short ticker for your token?\n"
        "Usually 3–5 capital letters, no spaces.\n"
        "Example: `MCT`",
        parse_mode="Markdown")
    return ENTERING_SYMBOL


async def symbol_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["launch"]["symbol"] = update.message.text.strip().upper()
    await update.message.reply_text(
        "*Step 4/9 — Total supply*\n\nHow many tokens should exist in total?\n"
        "Enter a plain whole number — no commas, no decimals. Most launches use 1,000,000,000 (1 billion).\n"
        "Decimal scaling is handled for you automatically.\n"
        "Example: `1000000000`"
    ,
        parse_mode="Markdown")
    return ENTERING_SUPPLY


async def supply_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().replace(",", "")
    if not text.isdigit():
        await update.message.reply_text("That doesn't look like a plain number -- try again.")
        return ENTERING_SUPPLY

    launch = context.user_data["launch"]
    decimals = {"solana": 6, "ton": 9, "tron": 6}.get(launch["chain"], 18)
    launch["total_supply_raw"] = str(int(text) * (10 ** decimals))
    launch["decimals"] = decimals
    launch["supply_display"] = text
    launch.setdefault("extra_params", {})

    if launch["mode"] in CURVE_MODES:
        unit = "SOL" if launch["chain"] == "solana" else CHAINS[launch["chain"]]
        await update.message.reply_text(
            f"*Step 5/9 — Graduation threshold*\n\n"
            f"How much {unit} should the bonding curve collect before it \"graduates\" to a full liquidity pool?\n"
            "Typical range: 5–85 depending on how big you want the curve phase to be.\n"
            f"Example: `5` (in {unit})  —  or type `default` for a standard threshold"
        ,
        parse_mode="Markdown")
        return ENTERING_GRAD
    await update.message.reply_text(
        "*Step 8/9 — Team allocation*\n\n"
        "Want to set aside a % of supply for team wallets? This mints directly to those addresses at launch.\n"
        "Format: `address:500` where 500 = 5% (out of a 10000 total). Separate multiple with commas.\n"
        "Example: `0xabc...:500, 0xdef...:250`\n\n"
        "Type `skip` for no team allocation."
    ,
        parse_mode="Markdown")
    return ENTERING_ALLOCS


def _to_wei(text: str, decimals: int) -> int:
    raw = text.strip().lower().replace(",", "")
    if raw in {"default", "d", ""}:
        return 0
    if not raw.replace(".", "", 1).isdigit():
        raise ValueError("number")
    if "." in raw:
        whole, frac = raw.split(".", 1)
        frac = (frac + "0" * decimals)[:decimals]
        return int(whole or "0") * (10 ** decimals) + int(frac or "0")
    return int(raw) * (10 ** decimals)


async def _show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    extra = launch.get("extra_params") or {}
    lines = [
        "*Review your launch:*",
        f"Chain: {CHAINS[launch['chain']]}",
        f"Mode: {launch['mode']}",
        f"Name: {launch['name']}",
        f"Symbol: {launch['symbol']}",
        f"Supply: {launch.get('supply_display', launch['total_supply_raw'])}",
    ]
    if extra.get("graduation_eth_threshold"):
        lines.append(f"Graduation: {extra['graduation_eth_threshold']}")
    if extra.get("virtual_eth_reserve"):
        lines.append(f"Virtual reserve: {extra['virtual_eth_reserve']}")
    if extra.get("dev_buy"):
        lines.append(f"Dev buy: {extra['dev_buy']}")
    if extra.get("start_minutes"):
        lines.append(f"Delay min: {extra['start_minutes']}  max buy: {extra.get('max_buy', '0')}")
    if extra.get("allocs"):
        lines.append(f"Allocs: {extra['allocs']}")
    lines.append("")
    lines.append("Confirm to open the wallet screen. You sign. Ferzan never holds the key.")
    buttons = [[
        InlineKeyboardButton("✅ Confirm", callback_data="confirm:yes"),
        InlineKeyboardButton("❌ Cancel", callback_data="confirm:no"),
    ]]
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown",
    )
    return CONFIRMING


async def grad_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    decimals = 9 if launch["chain"] == "solana" else 18
    try:
        val = _to_wei(update.message.text, decimals)
    except ValueError:
        await update.message.reply_text("Number or default.")
        return ENTERING_GRAD
    if val == 0:
        val = (50 * 10 ** 9) if launch["chain"] == "solana" else (5 * 10 ** 18)
    launch.setdefault("extra_params", {})["graduation_eth_threshold"] = str(val)
    await update.message.reply_text("*Step 6/9 — Starting price*\n\n"
        "Virtual native reserve sets the bonding curve's starting price — higher means a higher price at launch.\n"
        "Most launches leave this at default.\n"
        "Example: `1`  —  or type `default`",
        parse_mode="Markdown")
    return ENTERING_VETH


async def veth_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    decimals = 9 if launch["chain"] == "solana" else 18
    try:
        val = _to_wei(update.message.text, decimals)
    except ValueError:
        await update.message.reply_text("Number or default.")
        return ENTERING_VETH
    if val == 0:
        val = (1 * 10 ** 9) if launch["chain"] == "solana" else (1 * 10 ** 18)
    launch.setdefault("extra_params", {})["virtual_eth_reserve"] = str(val)
    await update.message.reply_text("*Step 7/9 — Curve depth*\n\n"
        "Virtual token reserve controls how much supply the curve trades through before graduating.\n"
        "Most launches leave this at default (80% of total supply).\n"
        "Example: `800000000`  —  or type `default`",
        parse_mode="Markdown")
    return ENTERING_VTOKEN


async def vtoken_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    try:
        val = _to_wei(update.message.text, launch["decimals"])
    except ValueError:
        await update.message.reply_text("Number or default.")
        return ENTERING_VTOKEN
    if val == 0:
        val = int(int(launch["total_supply_raw"]) * 80 / 100)
    launch.setdefault("extra_params", {})["virtual_token_reserve"] = str(val)
    await update.message.reply_text(
        "Team wallets? Format `0xabc...:500` (500 = 5%). Multiple comma-separated. Or skip"
    )
    return ENTERING_ALLOCS


async def allocs_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    text = (update.message.text or "").strip().lower()
    extra = launch.setdefault("extra_params", {})
    extra["allocs"] = "" if text in {"skip", "none", "no", "0"} else update.message.text.strip()
    ref = db.get_referrer(update.effective_user.id)
    if ref:
        extra["referrer_id"] = str(ref)
    if launch["mode"] in CURVE_MODES:
        await update.message.reply_text("*Step 9/9 — Dev buy*\n\n"
        "Want to buy some of your own token the instant it launches, before anyone else can?\n"
        "This is optional and denominated in the chain's native currency (e.g. SOL, ETH).\n"
        "Example: `0.05`  —  or `0` to skip",
        parse_mode="Markdown")
        return ENTERING_DEVBUY
    return await _show_confirm(update, context)


async def devbuy_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    raw = (update.message.text or "0").strip().replace(",", "")
    launch.setdefault("extra_params", {})["dev_buy"] = raw
    await update.message.reply_text(
        "*Final step — Trading window*\n\n"
        "Two settings, space-separated:\n"
        "• Delay in minutes before trading opens (gives you time to prep)\n"
        "• Max buy per wallet in native currency (limits early whales)\n"
        "Example: `10 0.2` = opens in 10 minutes, 0.2 max per wallet\n"
        "Type `0 0` for instant open with no cap\n"
        "Example: `10 0.2`  (opens in 10m, max 0.2 native)\n"
        "Or `0 0` for instant / no cap"
    ,
        parse_mode="Markdown")
    return ENTERING_WINDOW


async def window_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    parts = (update.message.text or "0 0").replace(",", "").split()
    mins = parts[0] if parts else "0"
    cap = parts[1] if len(parts) > 1 else "0"
    extra = launch.setdefault("extra_params", {})
    extra["start_minutes"] = mins
    extra["max_buy"] = cap
    return await _show_confirm(update, context)


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
    live = MINI_APP_BASE_URL.startswith("https://") and "yourdomain.com" not in MINI_APP_BASE_URL
    if not live:
        treas = os.environ.get("PLATFORM_TREASURY_EVM") or os.environ.get("TREASURY_EVM") or "(set PLATFORM_TREASURY_EVM)"
        await query.edit_message_text(
            f"✅ Request saved: {req.name} ({req.symbol}) on {req.chain}\n"
            f"ID: `{req.id}`\n\n"
            "Wallet signing is not live yet (no HTTPS Mini App).\n"
            f"When it is live, platform fees go to treasury:\n`{treas}`\n\n"
            "Close the blank Mini App if it opened. Use /launch to file another request.",
            parse_mode="Markdown",
        )
    else:
        page = {
            "solana": "solana.html",
            "tron": "evm.html",
            "ton": "evm.html",
        }.get(launch["chain"], "evm.html")
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


async def refer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = (os.environ.get("LAUNCHBOT_USERNAME") or "FerzanLaunchBot").lstrip("@")
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        f"Your Ferzan launch referral:\n"
        f"https://t.me/{me}?start=ref_{uid}\n\n"
        "Share that link. When they tap Start we store you as their referrer.\n\n"
        "On-chain payout needs your EVM wallet:\n"
        "`/referwallet 0xYourAddress`\n\n"
        "Curve buys can then send 10% of the 1% fee to that address. "
        "The trade bot has to pass it into buy() — until that ships, "
        "the link only tracks who referred whom."
    )


async def referwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args or []).strip()
    if not (raw.startswith("0x") and len(raw) == 42):
        await update.effective_message.reply_text("Usage: /referwallet 0xYourEvmAddress")
        return
    db.set_payout_wallet(update.effective_user.id, raw)
    await update.effective_message.reply_text(f"Payout wallet set:\n`{raw}`", parse_mode="Markdown")


async def lplock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🔒 *LP lock helper (plain launches)*\n\n"
        "Ferzan curve mode already burns LP to `0xdead` on graduation.\n"
        "Plain mode: after you add liquidity on Uniswap / Pancake / Raydium:\n"
        "1. Find the LP token in your wallet\n"
        "2. Send the LP tokens to `0x000000000000000000000000000000000000dead`\n"
        "3. Post the burn tx in your group — buyers can verify\n\n"
        "Do not send the *project* token. Only the LP pair token.",
        parse_mode="Markdown",
    )


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
            ENTERING_GRAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, grad_entered)],
            ENTERING_VETH: [MessageHandler(filters.TEXT & ~filters.COMMAND, veth_entered)],
            ENTERING_VTOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, vtoken_entered)],
            ENTERING_ALLOCS: [MessageHandler(filters.TEXT & ~filters.COMMAND, allocs_entered)],
            ENTERING_DEVBUY: [MessageHandler(filters.TEXT & ~filters.COMMAND, devbuy_entered)],
            ENTERING_WINDOW: [MessageHandler(filters.TEXT & ~filters.COMMAND, window_entered)],
            CONFIRMING: [CallbackQueryHandler(confirmed, pattern="^confirm:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(conv)
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("refer", refer_cmd))
    app.add_handler(CommandHandler("referwallet", referwallet_cmd))
    app.add_handler(CommandHandler("lplock", lplock_cmd))
    app.add_handler(CallbackQueryHandler(go_history, pattern="^go:history$"))

    async def _post(application):
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Ferzan Launch home"),
                BotCommand("launch", "Launch a token"),
                BotCommand("history", "Your launches"),
                BotCommand("refer", "Your referral link"),
                BotCommand("referwallet", "Set referral payout wallet"),
                BotCommand("lplock", "Burn / lock LP helper"),
                BotCommand("cancel", "Cancel launch"),
            ]
        )

    app.post_init = _post
    logger.info("Ferzan Launch starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
