"""Ferzan Liq — pool analytics + real LP desk hook. Token: LIQBOT_TOKEN"""
from __future__ import annotations

import html
import logging
import os
import re
from pathlib import Path

import requests
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv("/opt/ferzan/.env")
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s liqbot %(message)s")
log = logging.getLogger("liqbot")

TRADE = (os.getenv("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")

# Optional CEX testnet quoting (sandbox only)
import sys
sys.path.append("/opt/ferzan/app/liq")
sys.path.append(str(Path(__file__).resolve().parent / "liq"))
try:
    import credentials_db
    from liquidity_commands import start_liquidity, stop_liquidity
    HAS_MM = True
except Exception as _exc:
    HAS_MM = False
    start_liquidity = stop_liquidity = None
    log.warning("testnet MM modules not loaded: %s", _exc)

CHAT = os.getenv("FERZAN_CHAT_URL") or "https://t.me/Ferzan_Chat"
DS = "https://api.dexscreener.com/latest/dex/tokens/{}"
EVM = re.compile(r"^0x[a-fA-F0-9]{40}$")
CHAINS = ["solana", "ethereum", "bsc", "base", "hood"]
CHAIN_LABEL = {"solana": "Solana · SOL", "ethereum": "Ethereum · ETH", "bsc": "BNB Chain · BNB", "base": "Base · ETH", "hood": "Robinhood · ETH"}
CHAIN_PREF: dict[int, str] = {}
SET_TOKEN: set[int] = set()
SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _pools(ca: str) -> list[dict]:
    r = requests.get(DS.format(ca), timeout=12)
    pairs = (r.json() or {}).get("pairs") or []
    pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    return pairs


def _impact(liq: float, trade: float) -> float:
    if liq <= 0:
        return float("inf")
    side = liq / 2
    return trade / (side + trade) * 100


def _score(liq: float, vol: float) -> float:
    if liq <= 0:
        return 0.0
    depth = min(100.0, liq / 500_000 * 100)
    turn = min(100.0, (vol / liq) * 100)
    return round(0.65 * depth + 0.35 * turn, 1)


def _menu(uid: int = 0) -> InlineKeyboardMarkup:
    chain = CHAIN_PREF.get(uid, "solana")
    lab = CHAIN_LABEL.get(chain, chain)
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏆 Rank Boost", callback_data="liq:rank")],
            [
                InlineKeyboardButton("📊 Chart Maker", callback_data="liq:chart"),
                InlineKeyboardButton("⚡ Testnet quotes", callback_data="liq:vol"),
            ],
            [
                InlineKeyboardButton("👥 Holders", callback_data="liq:hold"),
                InlineKeyboardButton("😮 Reactions", callback_data="liq:react"),
            ],
            [
                InlineKeyboardButton("🎁 Earn", callback_data="liq:earn"),
                InlineKeyboardButton("🏧 Withdraw", callback_data="liq:wd"),
            ],
            [InlineKeyboardButton(f"⛓ Chain: {lab}", callback_data="liq:chain")],
            [
                InlineKeyboardButton("💬 Support", url=CHAT),
                InlineKeyboardButton("⚡ Ferzan Trade", url=f"https://t.me/{TRADE}"),
            ],
        ]
    )

def _chain_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(CHAIN_LABEL[c], callback_data=f"liq:setchain:{c}")] for c in CHAINS]
    rows.append([InlineKeyboardButton("« Back", callback_data="liq:menu")])
    return InlineKeyboardMarkup(rows)


def _start_text() -> str:
    return (

        "💧 <b>Ferzan Liq</b> · ⛓ play desk\n\n"
        "Welcome. Multi-chain pool tools for tokens you already trade.\n\n"
        "📊 <b>Chart Maker</b> — live DexScreener card from a CA\n"
        "⚡ <b>Testnet quotes</b> — CEX sandbox maker (/setkeys)\n"
        "🏆 <b>Rank</b> — real unique flow from the chart, not bought prints\n"
        "👥 <b>Holders</b> — real Dex holder/liq numbers\n"
        "😮 <b>Reactions</b> — off (no farms)\n"
        "🎁 <b>Earn</b> — cut of real Ferzan Trade fees\n"
        "🏧 <b>Withdraw</b> — Trade wallets hold funds, not this bot\n\n"
        "Paste a CA any time. Switch chain from the list. "
        "Bottom right opens <b>Ferzan Trade</b> to buy."
    )

def _chart_text(uid: int = 0) -> str:
    chain = CHAIN_LABEL.get(CHAIN_PREF.get(uid, "solana"), "Solana · SOL")
    return (
        "📊 <b>Chart Maker</b>\n\n"
        "<b>What it shows</b>\n"
        "👥 <b>Holders</b> — count from the live Dex card\n"
        "📈 <b>Volume</b> — real 24h volume on that pool\n"
        "💧 <b>Liquidity</b> — pool TVL, not a painted book\n"
        "🎯 <b>Score</b> — depth + turnover 0–100\n\n"
        "<b>Play desk</b>\n"
        "This screen does not run a 5-wallet uptrend. "
        "No aged-wallet pool, no scheduled buy/sell to draw candles.\n\n"
        "<b>How to use</b>\n"
        "1. Tap <b>Set token</b>\n"
        "2. Paste the CA\n"
        "3. Get the Ferzan card (price, liq, vol, impact)\n\n"
        f"⛓ Active chain: {chain}\n"
        "Buy from the card opens <b>Ferzan Trade</b>."
    )

def _chart_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📄 Set token", callback_data="liq:settoken")],
            [InlineKeyboardButton("⬅️ Back", callback_data="liq:menu")],
        ]
    )


def _nav_kb(*extra):
    rows = list(extra)
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data="liq:refresh"),
        InlineKeyboardButton("⬅️ Back", callback_data="liq:menu"),
    ])
    return InlineKeyboardMarkup(rows)


def _earn_text(uid: int) -> str:
    link = f"https://t.me/{TRADE}?start=r-{uid}"
    return (
        "🎁 <b>Earn with Ferzan</b>\n\n"
        "Share your link. When someone you referred <b>actually swaps on Ferzan Trade</b>, "
        "a cut of the real fee can land for you — not a fake-volume rebate.\n\n"
        f"🔗 <b>Your link</b>\n<code>{link}</code>\n\n"
        "👥 Referrals — tracked when Trade referral desk is live\n"
        "💰 Earned — 0 until a referred live swap pays a fee\n\n"
        "<i>Nothing yet — share the link. Payout is on real trades only.</i>"
    )


def _wd_text() -> str:
    return (
        "🏧 <b>Withdraw</b>\n\n"
        "Ferzan Liq does <b>not</b> hold a deposit wallet.\n"
        "Balances live in <b>Ferzan Trade</b> wallets you already funded.\n\n"
        "Open Trade → Wallets → send out.\n"
        "Network fee is the only amount the chain keeps.\n\n"
        f"⚡ @{TRADE}"
    )


def _rank_text() -> str:
    return (
        "🏆 <b>Rank</b>\n\n"
        "Odin-style unique-buyer farms are <b>off</b>.\n"
        "We do not spin a fresh wallet per tiny buy to juice maker count.\n\n"
        "What you get instead: paste a CA and read <b>real</b> DexScreener "
        "volume / makers on that pool.\n\n"
        "📄 Set token → paste CA."
    )


def _hold_text() -> str:
    return (
        "👥 <b>Holders</b>\n\n"
        "No batch airdrop to 100 or 1,000 empty wallets.\n"
        "Holder count on the card is whatever DexScreener / the explorer shows.\n\n"
        "📄 Set token → paste CA."
    )


def _vol_text() -> str:
    return (
        "⚡ <b>Testnet quotes</b>\n\n"
        "Not Fast Volume. We will not buy and sell in one bundle to print 2× volume.\n\n"
        "Play path (CEX sandbox only):\n"
        "/setkeys exchange SYMBOL KEY SECRET\n"
        "/start_liquidity\n"
        "/stop_liquidity\n\n"
        "For a live pool, Set token and read 24h volume that already happened."
    )


def _react_text() -> str:
    return (
        "😮 <b>Reactions</b>\n\n"
        "DexScreener emoji farms are <b>off</b>.\n"
        "No packages, no Start Boost, no session-token clicks.\n\n"
        "If the pair is busy, the chart already shows it."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(_start_text(), parse_mode="HTML", reply_markup=_menu(update.effective_user.id if update.effective_user else 0))


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def token_card(update: Update, ca: str) -> None:
    try:
        pools = _pools(ca)
    except Exception as exc:
        await update.effective_message.reply_text(f"Lookup failed: {exc}")
        return
    if not pools:
        await update.effective_message.reply_text("No DexScreener pool for that CA.")
        return
    p = pools[0]
    base = p.get("baseToken") or {}
    liq = float((p.get("liquidity") or {}).get("usd") or 0)
    vol = float((p.get("volume") or {}).get("h24") or 0)
    lines = [
        f"💧 <b>{_esc(base.get('name'))}</b> ${_esc(base.get('symbol'))}",
        f"<code>{_esc(ca)}</code>",
        f"⛓ {_esc(p.get('chainId'))} · {_esc(p.get('dexId'))}",
        f"💵 ${_esc(p.get('priceUsd'))}",
        f"💧 Liq ${liq:,.0f}",
        f"📈 24h vol ${vol:,.0f}",
        f"🎯 Score {_score(liq, vol)}/100",
        "",
        "Impact (ballpark AMM):",
    ]
    for size in (100, 1000, 10000):
        lines.append(f"  ${size:,} → ~{_impact(liq, size):.2f}%")
    if len(pools) > 1:
        lines.append(f"\n+{len(pools)-1} other pool(s)")
    url = p.get("url") or f"https://dexscreener.com/{p.get('chainId')}/{ca}"
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📈 DexScreener", url=url)],
            [InlineKeyboardButton("⚡ Buy on Ferzan", url=f"https://t.me/{TRADE}?start={ca}")],
            [InlineKeyboardButton("« Menu", callback_data="liq:menu")],
        ]
    )
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    uid = update.effective_user.id if update.effective_user else 0
    if uid in SET_TOKEN:
        SET_TOKEN.discard(uid)
        if EVM.match(text) or SOL.match(text):
            await token_card(update, text)
        else:
            await update.effective_message.reply_text("That is not a CA. Tap Set token and paste the address.")
        return
    if EVM.match(text) or SOL.match(text):
        await token_card(update, text)


async def token_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /token 0x… or /token SOL_MINT")
        return
    await token_card(update, context.args[0].strip())


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    uid = q.from_user.id
    if data == "liq:chain":
        await q.edit_message_text(
            "⛓ <b>Choose a chain</b>\n\n"
            "Which network do you want to work on? Lookups follow this chain.\n"
            "You can switch any time.",
            parse_mode="HTML",
            reply_markup=_chain_kb(),
        )
        return
    if data.startswith("liq:setchain:"):
        CHAIN_PREF[uid] = data.split(":")[-1]
        await q.edit_message_text(
            _start_text(),
            parse_mode="HTML",
            reply_markup=_menu(uid),
        )
        return
    if data == "liq:refresh":
        await q.edit_message_text(_start_text(), parse_mode="HTML", reply_markup=_menu(uid))
        return
    if data == "liq:vol":
        await q.edit_message_text(_vol_text(), parse_mode="HTML", reply_markup=_nav_kb(
            [InlineKeyboardButton("📄 Set token", callback_data="liq:settoken")]
        ))
        return
    if data == "liq:rank":
        await q.edit_message_text(_rank_text(), parse_mode="HTML", reply_markup=_nav_kb(
            [InlineKeyboardButton("📄 Set token", callback_data="liq:settoken")]
        ))
        return
    if data == "liq:hold":
        await q.edit_message_text(_hold_text(), parse_mode="HTML", reply_markup=_nav_kb(
            [InlineKeyboardButton("📄 Set token", callback_data="liq:settoken")]
        ))
        return
    if data == "liq:react":
        await q.edit_message_text(_react_text(), parse_mode="HTML", reply_markup=_nav_kb())
        return
    if data == "liq:menu":
        await q.edit_message_text(_start_text(), parse_mode="HTML", reply_markup=_menu(uid))
        return
    if data == "liq:chart":
        await q.edit_message_text(_chart_text(uid), parse_mode="HTML", reply_markup=_chart_kb())
        return
    if data == "liq:settoken":
        SET_TOKEN.add(uid)
        await q.message.reply_text("Send the contract address (CA) now.")
        return
    if data in ("liq:score", "liq:impact"):
        await q.message.reply_text("Paste a contract address (CA) in this chat.")
        return
    if data == "liq:earn":
        await q.edit_message_text(_earn_text(uid), parse_mode="HTML", reply_markup=_nav_kb(
            [InlineKeyboardButton("⚡ Open Trade", url=f"https://t.me/{TRADE}?start=r-{uid}")]
        ))
        return
    if data == "liq:wd":
        await q.edit_message_text(_wd_text(), parse_mode="HTML", reply_markup=_nav_kb(
            [InlineKeyboardButton("⚡ Open Trade wallets", url=f"https://t.me/{TRADE}")]
        ))
        return



async def setkeys(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type != "private":
        await update.effective_message.reply_text("Send /setkeys in a private chat with this bot.")
        return
    if not HAS_MM:
        await update.effective_message.reply_text("Testnet maker modules are not installed on this host.")
        return
    if len(context.args) < 4:
        await update.effective_message.reply_text(
            "Usage (DM only):\n/setkeys <exchange> <symbol> <api_key> <api_secret>\n"
            "Example: /setkeys binance BTC/USDT KEY SECRET\n"
            "Key must be TRADE only, no withdraw. Loop is testnet/sandbox."
        )
        return
    credentials_db.init_db()
    credentials_db.store_credentials(
        update.effective_user.id, context.args[0], context.args[1], context.args[2], context.args[3]
    )
    await update.effective_message.reply_text("Keys stored encrypted. /start_liquidity to quote on testnet.")


def main() -> None:
    token = (os.getenv("LIQBOT_TOKEN") or os.getenv("FERZAN_LIQ_TOKEN") or "").strip()
    if not token:
        raise SystemExit("Set LIQBOT_TOKEN in /opt/ferzan/.env")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("token", token_cmd))
    if HAS_MM:
        credentials_db.init_db()
        app.add_handler(CommandHandler("setkeys", setkeys))
        app.add_handler(CommandHandler("start_liquidity", start_liquidity))
        app.add_handler(CommandHandler("stop_liquidity", stop_liquidity))
    app.add_handler(CallbackQueryHandler(buttons, pattern=r"^liq:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    async def _post(app):
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Start the bot"),
                BotCommand("help", "Help and desk"),
                BotCommand("token", "Lookup a CA"),
                BotCommand("setkeys", "Testnet API keys (DM)"),
                BotCommand("start_liquidity", "Start testnet quotes"),
                BotCommand("stop_liquidity", "Stop testnet quotes"),
            ]
        )
    app.post_init = _post
    log.info("Ferzan Liq running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
