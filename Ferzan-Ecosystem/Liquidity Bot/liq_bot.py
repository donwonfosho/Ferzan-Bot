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
sys.path.append(str(Path(__file__).resolve().parent / "liq"))
try:
    import credentials_db
    from liquidity_commands import start_liquidity, stop_liquidity
    HAS_MM = True
except Exception as _exc:
    HAS_MM = False
    start_liquidity = stop_liquidity = None
    log.warning("testnet MM modules not loaded: %s", _exc)

try:
    import subscription
    import basestonk_mm
    HAS_BSTONK_MM = True
except Exception as _exc:
    HAS_BSTONK_MM = False
    subscription = None
    basestonk_mm = None
    log.warning("basestonk MM modules not loaded: %s", _exc)

MM_PRICE_USD = float(os.getenv("FERZAN_MM_PRICE_USD", "49"))
MM_PLAN_DAYS = int(os.getenv("FERZAN_MM_PLAN_DAYS", "30"))
# One treasury address across the whole ecosystem -- same var Launch Bot
# already pays its platform fees to. FERZAN_TREASURY_EVM stays as a
# fallback only for anyone who already set that name.
MM_TREASURY = (
    os.getenv("PLATFORM_TREASURY_EVM") or os.getenv("TREASURY_EVM") or os.getenv("FERZAN_TREASURY_EVM") or ""
).strip().lower()
MM_RPC = os.getenv("BASE_RPC", "https://mainnet.base.org")
ADMIN_IDS = {
    int(x) for x in re.split(r"[,\s]+", (os.getenv("FERZAN_ADMIN_IDS") or "").strip()) if x.strip().isdigit()
}


def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

CHAT = os.getenv("FERZAN_CHAT_URL") or "https://t.me/Ferzan_Chat"
DS = "https://api.dexscreener.com/latest/dex/tokens/{}"
# Trade Desk's internal_api.py -- same droplet, loopback only. Shared
# secret must match Trade Desk's INTERNAL_API_TOKEN env var exactly.
TRADE_API_URL = (os.getenv("TRADE_API_URL") or "http://127.0.0.1:8011").rstrip("/")
INTERNAL_API_TOKEN = (os.getenv("INTERNAL_API_TOKEN") or "").strip()
EVM = re.compile(r"^0x[a-fA-F0-9]{40}$")
CHAINS = ["solana", "ethereum", "bsc", "base", "hood"]
CHAIN_LABEL = {"solana": "Solana · SOL", "ethereum": "Ethereum · ETH", "bsc": "BNB Chain · BNB", "base": "Base · ETH", "hood": "Robinhood · ETH"}
CHAIN_PREF: dict[int, str] = {}
SET_TOKEN: set[int] = set()
SOL = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Real holder counts. Neither DexScreener nor most chain explorers give this
# away free, so each chain needs its own provider + API key. "hood" has no
# established explorer API at all -- left unsupported rather than faking it.
SOLSCAN_API_KEY = (os.getenv("SOLSCAN_API_KEY") or "").strip()
COVALENT_API_KEY = (os.getenv("COVALENT_API_KEY") or "").strip()
COVALENT_CHAIN_ID = {"ethereum": "1", "bsc": "56", "base": "8453"}


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _holder_count(chain_id: str, ca: str) -> int | None:
    """Real holder count for one token, or None if we can't get one.

    None covers every "can't answer honestly" case on purpose: no key set
    for this chain's provider, the provider errored, or the chain (e.g.
    Robinhood/"hood") has no supported provider at all. Callers must not
    turn None into 0 -- 0 holders and "couldn't check" are different facts.
    """
    try:
        if chain_id == "solana":
            if not SOLSCAN_API_KEY:
                return None
            r = requests.get(
                "https://pro-api.solscan.io/v2.0/token/meta",
                params={"address": ca},
                headers={"token": SOLSCAN_API_KEY},
                timeout=8,
            )
            if r.status_code != 200:
                log.warning("solscan holder lookup failed ca=%s status=%s", ca, r.status_code)
                return None
            data = (r.json() or {}).get("data") or {}
            holders = data.get("holder")
            return int(holders) if holders is not None else None

        covalent_chain = COVALENT_CHAIN_ID.get(chain_id)
        if covalent_chain:
            if not COVALENT_API_KEY:
                return None
            r = requests.get(
                f"https://api.covalenthq.com/v1/{covalent_chain}/tokens/{ca}/token_holders_v2/",
                params={"key": COVALENT_API_KEY, "page-size": 1},
                timeout=8,
            )
            if r.status_code != 200:
                log.warning("covalent holder lookup failed chain=%s ca=%s status=%s", chain_id, ca, r.status_code)
                return None
            pagination = ((r.json() or {}).get("data") or {}).get("pagination") or {}
            total = pagination.get("total_count")
            return int(total) if total is not None else None

        # No provider wired up for this chain (e.g. "hood") -- be honest.
        return None
    except Exception as exc:
        log.warning("holder lookup errored chain=%s ca=%s: %s", chain_id, ca, exc)
        return None


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
            [InlineKeyboardButton("💧 Run MM (paste-a-CA volume)", callback_data="mmw:start")],
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


def _fetch_referral_stats(uid: int) -> dict | None:
    """Real numbers from Trade Desk's referral_ledger, over its internal API.

    Returns None on any failure (API down, token mismatch, etc.) so callers
    can fall back to an honest "can't reach it right now" message instead of
    silently showing zeros as if that were the real balance.
    """
    try:
        headers = {"x-ferzan-internal": INTERNAL_API_TOKEN} if INTERNAL_API_TOKEN else {}
        r = requests.get(f"{TRADE_API_URL}/internal/referral-stats/{uid}", headers=headers, timeout=6)
        if r.status_code != 200:
            log.warning("referral-stats lookup failed uid=%s status=%s", uid, r.status_code)
            return None
        return r.json()
    except Exception as exc:
        log.warning("referral-stats lookup errored uid=%s: %s", uid, exc)
        return None


def _earn_text(uid: int) -> str:
    # NOTE: must match the "ref_" prefix Trade Desk's bot.py actually parses
    # on /start (see bot.py's deep-link handling) -- the old "r-{uid}" prefix
    # here didn't match anything, so shared links silently attributed to no one.
    link = f"https://t.me/{TRADE}?start=ref_{uid}"
    stats = _fetch_referral_stats(uid)
    if stats is None:
        return (
            "🎁 <b>Earn with Ferzan</b>\n\n"
            "Share your link. When someone you referred <b>actually swaps on Ferzan Trade</b>, "
            "a cut of the real fee lands for you — not a fake-volume rebate.\n\n"
            f"🔗 <b>Your link</b>\n<code>{link}</code>\n\n"
            "⚠️ Couldn't reach the Trade Desk referral ledger just now, so live numbers "
            "aren't shown here. Try again shortly, or check <code>/ref</code> on "
            f"@{TRADE} directly.\n\n"
            "Payout is on real trades only."
        )
    return (
        "🎁 <b>Earn with Ferzan</b>\n\n"
        "Share your link. When someone you referred <b>actually swaps on Ferzan Trade</b>, "
        "a cut of the real fee lands for you — not a fake-volume rebate.\n\n"
        f"🔗 <b>Your link</b>\n<code>{link}</code>\n\n"
        f"🏅 Tier — {_esc(stats.get('tier'))}\n"
        f"👥 Referrals — {int(stats.get('invites') or 0):,}\n"
        f"📈 Their volume — ${float(stats.get('volume') or 0):,.2f}\n"
        f"💰 Earned (lifetime) — ${float(stats.get('earned') or 0):.4f}\n"
        f"💸 Claimable now — ${float(stats.get('open') or 0):.4f}\n\n"
        "<i>Claim with /claim on Ferzan Trade once claimable ≥ $5.</i>"
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
        "What you get instead: paste a CA and the card shows <b>real</b> DexScreener "
        "24h volume plus the actual buy/sell order count on that pool — not a "
        "unique-wallet number (Dex doesn't expose that), just real order flow, "
        "unpadded.\n\n"
        "📄 Set token → paste CA."
    )


def _hold_text() -> str:
    return (
        "👥 <b>Holders</b>\n\n"
        "No batch airdrop to 100 or 1,000 empty wallets.\n"
        "Holder count on the card is pulled live from Solscan (Solana) or "
        "Covalent (Ethereum / BSC / Base) — a real per-wallet count, not a Dex "
        "estimate. Not available yet on Robinhood chain, or if the lookup fails.\n\n"
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
    txns24 = (p.get("txns") or {}).get("h24") or {}
    buys = int(txns24.get("buys") or 0)
    sells = int(txns24.get("sells") or 0)
    holders = _holder_count(p.get("chainId") or "", ca)
    holders_line = f"👥 Holders {holders:,}" if holders is not None else "👥 Holders — not available"
    lines = [
        f"💧 <b>{_esc(base.get('name'))}</b> ${_esc(base.get('symbol'))}",
        f"<code>{_esc(ca)}</code>",
        f"⛓ {_esc(p.get('chainId'))} · {_esc(p.get('dexId'))}",
        f"💵 ${_esc(p.get('priceUsd'))}",
        holders_line,
        f"💧 Liq ${liq:,.0f}",
        f"📈 24h vol ${vol:,.0f}",
        f"🔁 24h orders {buys+sells:,} ({buys:,} buys / {sells:,} sells)",
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
    wiz = MM_WIZ.get(uid) if HAS_BSTONK_MM else None
    if wiz and wiz.get("step") == "ca":
        if EVM.match(text):
            wiz["token"] = text
            wiz["step"] = "round"
            await update.effective_message.reply_text(
                "💧 <b>Run MM — step 2 of 5</b>\n\nHow much per buy/sell round?",
                parse_mode="HTML",
                reply_markup=_mm_picks_kb("round", MM_ROUNDS),
            )
        else:
            await update.effective_message.reply_text("That's not a 0x contract address. Paste the token CA (BaseStonk is EVM-only right now).")
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
    if data.startswith("mmw:"):
        await _mm_wizard_button(update, context, uid, data)
        return
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




async def cmd_rank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.effective_message.reply_text(_rank_text(), parse_mode="HTML", reply_markup=_nav_kb(
        [InlineKeyboardButton("📄 Set token", callback_data="liq:settoken")]
    ))

async def cmd_chart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.effective_message.reply_text(_chart_text(uid), parse_mode="HTML", reply_markup=_chart_kb())

async def cmd_quotes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(_vol_text(), parse_mode="HTML", reply_markup=_nav_kb(
        [InlineKeyboardButton("📄 Set token", callback_data="liq:settoken")]
    ))

async def cmd_holders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(_hold_text(), parse_mode="HTML", reply_markup=_nav_kb(
        [InlineKeyboardButton("📄 Set token", callback_data="liq:settoken")]
    ))

async def cmd_react(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(_react_text(), parse_mode="HTML", reply_markup=_nav_kb())

async def cmd_ref(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.effective_message.reply_text(_earn_text(uid), parse_mode="HTML", reply_markup=_nav_kb(
        [InlineKeyboardButton("⚡ Open Trade", url=f"https://t.me/{TRADE}?start=r-{uid}")]
    ))

async def cmd_wd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(_wd_text(), parse_mode="HTML", reply_markup=_nav_kb(
        [InlineKeyboardButton("⚡ Open Trade wallets", url=f"https://t.me/{TRADE}")]
    ))

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


# --- guided /mm wizard: chain -> CA -> round size -> budget -> duration -> confirm ---

MM_WIZ: dict[int, dict] = {}
MM_ROUNDS = [2, 5, 10]
MM_BUDGETS = [10, 25, 50]
MM_DURATIONS = [15, 30, 60]


def _mm_run_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📊 Status", callback_data="mmw:status"),
                InlineKeyboardButton("⏹ Stop", callback_data="mmw:stopnow"),
            ]
        ]
    )


def _mm_chain_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Base", callback_data="mmw:chain:base")],
            [InlineKeyboardButton("Robinhood Chain", callback_data="mmw:chain:robinhood")],
            [InlineKeyboardButton("✖️ Cancel", callback_data="mmw:cancel")],
        ]
    )


def _mm_picks_kb(prefix: str, values: list, suffix: str = "") -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(f"${v}{suffix}", callback_data=f"mmw:{prefix}:{v}") for v in values]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("✖️ Cancel", callback_data="mmw:cancel")]])


def _mm_duration_kb() -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(f"{m}m", callback_data=f"mmw:dur:{m}") for m in MM_DURATIONS]
    return InlineKeyboardMarkup([row, [InlineKeyboardButton("✖️ Cancel", callback_data="mmw:cancel")]])


async def _mm_wizard_start(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int) -> None:
    if not HAS_BSTONK_MM:
        await update.effective_message.reply_text("MM module is not installed on this host.")
        return
    if not (subscription.is_premium(uid) or _is_admin(uid)):
        await update.effective_message.reply_text(
            f"🔒 MM (paste-a-CA volume) is a paid feature — ${MM_PRICE_USD:.0f}/{MM_PLAN_DAYS}d.\n\n"
            f"Send that amount of ETH on Base to:\n<code>{MM_TREASURY or 'set PLATFORM_TREASURY_EVM'}</code>\n"
            "then run /paid followed by the transaction hash.",
            parse_mode="HTML",
        )
        return
    existing = basestonk_mm.status(uid) if HAS_BSTONK_MM else None
    if existing and uid in basestonk_mm._active:
        await update.effective_message.reply_text("You already have an MM session running.", reply_markup=_mm_run_kb())
        return
    MM_WIZ[uid] = {"step": "chain"}
    await update.effective_message.reply_text(
        "💧 <b>Run MM — step 1 of 5</b>\n\nWhich chain is the token on?",
        parse_mode="HTML",
        reply_markup=_mm_chain_kb(),
    )


async def _mm_wizard_button(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, data: str) -> None:
    q = update.callback_query
    parts = data.split(":")  # mmw:<action>[:<value>]
    action = parts[1] if len(parts) > 1 else ""

    if action == "start":
        await _mm_wizard_start(update, context, uid)
        return

    if action == "status":
        row = basestonk_mm.status(uid) if HAS_BSTONK_MM else None
        if not row:
            await q.message.reply_text("No MM session on record yet.")
            return
        state = "running" if row["status"] == "running" and uid in basestonk_mm._active else row["status"]
        await q.message.reply_text(
            f"MM ({row['chain']}, {row['token'][:10]}…): {state}\n"
            f"Trades: {row['trades']} · Spent ~${row['spent_usd']:.2f} of ${row['budget_usd']:.2f} budget",
            reply_markup=_mm_run_kb() if state == "running" else None,
        )
        return

    if action == "stopnow":
        ok = basestonk_mm.stop(uid) if HAS_BSTONK_MM else False
        await q.message.reply_text("Stopping after the current leg…" if ok else "No MM session running.")
        return

    if action == "cancel":
        MM_WIZ.pop(uid, None)
        await q.edit_message_text("Cancelled.")
        return

    wiz = MM_WIZ.get(uid)
    if not wiz:
        await q.edit_message_text("That setup expired — tap 💧 Run MM to start again.")
        return

    if action == "chain":
        wiz["chain"] = parts[2]
        wiz["step"] = "ca"
        await q.edit_message_text(
            "💧 <b>Run MM — step 2 of 5</b>\n\nPaste the token's contract address (CA) now.",
            parse_mode="HTML",
        )
        return

    if action == "round":
        wiz["trade_usd"] = float(parts[2])
        wiz["step"] = "budget"
        await q.edit_message_text(
            "💧 <b>Run MM — step 3 of 5</b>\n\nTotal budget for this session?",
            parse_mode="HTML",
            reply_markup=_mm_picks_kb("budget", MM_BUDGETS),
        )
        return

    if action == "budget":
        wiz["budget_usd"] = float(parts[2])
        wiz["step"] = "duration"
        await q.edit_message_text(
            "💧 <b>Run MM — step 4 of 5</b>\n\nHow long should it run?",
            parse_mode="HTML",
            reply_markup=_mm_duration_kb(),
        )
        return

    if action == "dur":
        wiz["minutes"] = int(parts[2])
        wiz["step"] = "confirm"
        await q.edit_message_text(
            "💧 <b>Run MM — step 5 of 5</b>\n\n"
            f"Chain: {wiz['chain']}\n"
            f"Token: <code>{wiz['token']}</code>\n"
            f"${wiz['trade_usd']:.0f} per round · ${wiz['budget_usd']:.0f} budget · {wiz['minutes']}m\n\n"
            "Confirm to start — this spends from your linked Ferzan wallet.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("▶️ Start MM", callback_data="mmw:confirm")],
                    [InlineKeyboardButton("✖️ Cancel", callback_data="mmw:cancel")],
                ]
            ),
        )
        return

    if action == "confirm":
        err = await _mm_begin(context, uid, wiz["chain"], wiz["token"], wiz["trade_usd"], wiz["budget_usd"], wiz["minutes"])
        MM_WIZ.pop(uid, None)
        if err:
            await q.edit_message_text(err)
            return
        await q.edit_message_text("Starting MM — I'll DM you here as it runs.", reply_markup=_mm_run_kb())
        return


async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    tag = " (admin)" if _is_admin(uid) else ""
    await update.effective_message.reply_text(f"Your Telegram user id: {uid}{tag}")


async def grantmm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not HAS_BSTONK_MM:
        return
    if not _is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Admins only.")
        return
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Usage: /grantmm <user_id> [days]")
        return
    try:
        target = int(args[0])
        days = int(args[1]) if len(args) > 1 else MM_PLAN_DAYS
    except ValueError:
        await update.effective_message.reply_text("user_id and days must be numbers.")
        return
    expiry = subscription.grant_premium(target, days)
    await update.effective_message.reply_text(f"Granted MM to {target} until {expiry.strftime('%Y-%m-%d')}.")


async def mm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not HAS_BSTONK_MM:
        await update.effective_message.reply_text("MM module is not installed on this host.")
        return
    uid = update.effective_user.id
    if not (subscription.is_premium(uid) or _is_admin(uid)):
        await update.effective_message.reply_text(
            f"🔒 MM (paste-a-CA volume) is a paid feature — ${MM_PRICE_USD:.0f}/{MM_PLAN_DAYS}d.\n\n"
            f"Send that amount of ETH on Base to:\n<code>{MM_TREASURY or 'set PLATFORM_TREASURY_EVM'}</code>\n"
            "then run /paid followed by the transaction hash.",
            parse_mode="HTML",
        )
        return
    args = context.args or []
    if not args:
        # no args typed -- walk them through it with buttons instead of dumping raw usage
        await _mm_wizard_start(update, context, uid)
        return
    if len(args) < 5:
        await update.effective_message.reply_text(
            "Usage: /mm <CA> <base|robinhood> <usd_per_round> <budget_usd> <minutes>\n"
            "Example: /mm 0xabc... base 5 50 60\n"
            "Or just send /mm with no arguments for a guided, button-driven setup."
        )
        return
    token, chain, usd_s, budget_s, min_s = args[0], args[1], args[2], args[3], args[4]
    try:
        trade_usd, budget_usd, minutes = float(usd_s), float(budget_s), int(min_s)
    except ValueError:
        await update.effective_message.reply_text("usd_per_round, budget_usd must be numbers; minutes an integer.")
        return

    err = await _mm_begin(context, uid, chain, token, trade_usd, budget_usd, minutes)
    if err:
        await update.effective_message.reply_text(err)
        return
    await update.effective_message.reply_text(
        "Starting MM — I'll DM you here as it runs.", reply_markup=_mm_run_kb()
    )


async def _mm_begin(context: ContextTypes.DEFAULT_TYPE, uid: int, chain: str, token: str, trade_usd: float, budget_usd: float, minutes: int) -> str | None:
    async def _notify(text: str):
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            pass

    return basestonk_mm.start(uid, chain, token, trade_usd, budget_usd, minutes, _notify)


async def mmstop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not HAS_BSTONK_MM:
        return
    ok = basestonk_mm.stop(update.effective_user.id)
    await update.effective_message.reply_text("Stopping after the current leg…" if ok else "No MM session running.")


async def mmstatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not HAS_BSTONK_MM:
        return
    row = basestonk_mm.status(update.effective_user.id)
    if not row:
        await update.effective_message.reply_text("No MM session on record yet.")
        return
    state = "running" if row["status"] == "running" and update.effective_user.id in basestonk_mm._active else row["status"]
    await update.effective_message.reply_text(
        f"Last MM session ({row['chain']}, {row['token'][:10]}…): {state}\n"
        f"Trades: {row['trades']} · Spent ~${row['spent_usd']:.2f} of ${row['budget_usd']:.2f} budget"
    )


async def paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not HAS_BSTONK_MM:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /paid <tx hash>")
        return
    if not MM_TREASURY:
        await update.effective_message.reply_text("Payments aren't configured yet — ask an admin to set PLATFORM_TREASURY_EVM.")
        return
    txh = context.args[0].strip()
    uid = update.effective_user.id
    subscription.init_db()
    with subscription._get_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS redeemed_tx (tx_hash TEXT PRIMARY KEY, user_id INTEGER)")
        used = conn.execute("SELECT 1 FROM redeemed_tx WHERE tx_hash=?", (txh,)).fetchone()
    if used:
        await update.effective_message.reply_text("That transaction was already redeemed.")
        return
    try:
        r = requests.post(
            MM_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [txh]},
            timeout=15,
        )
        receipt = (r.json() or {}).get("result")
        r2 = requests.post(
            MM_RPC,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionByHash", "params": [txh]},
            timeout=15,
        )
        txinfo = (r2.json() or {}).get("result")
    except Exception as exc:
        await update.effective_message.reply_text(f"Couldn't reach Base RPC: {exc}")
        return
    if not receipt or receipt.get("status") not in ("0x1", 1):
        await update.effective_message.reply_text("That transaction isn't confirmed (or failed). Wait a minute and try again.")
        return
    if not txinfo or str(txinfo.get("to") or "").lower() != MM_TREASURY:
        await update.effective_message.reply_text("That transaction doesn't pay the Ferzan treasury address.")
        return
    value_wei = int(txinfo.get("value") or "0x0", 16)
    px = 3000.0
    try:
        pr = requests.get(
            "https://api.coingecko.com/api/v3/simple/price", params={"ids": "ethereum", "vs_currencies": "usd"}, timeout=10
        )
        px = float((pr.json() or {}).get("ethereum", {}).get("usd") or px)
    except Exception:
        pass
    paid_usd = (value_wei / 1e18) * px
    if paid_usd < MM_PRICE_USD * 0.9:
        await update.effective_message.reply_text(f"That payment (~${paid_usd:.2f}) is short of the ${MM_PRICE_USD:.0f} price.")
        return
    with subscription._get_conn() as conn:
        conn.execute("INSERT INTO redeemed_tx (tx_hash, user_id) VALUES (?,?)", (txh, uid))
    expiry = subscription.grant_premium(uid, MM_PLAN_DAYS)
    await update.effective_message.reply_text(f"✅ MM unlocked until {expiry.strftime('%Y-%m-%d')}. Run /mm to start.")


def main() -> None:
    token = (os.getenv("LIQBOT_TOKEN") or os.getenv("FERZAN_LIQ_TOKEN") or "").strip()
    if not token:
        raise SystemExit("Set LIQBOT_TOKEN in /opt/ferzan/.env")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("token", token_cmd))

    app.add_handler(CommandHandler("rank", cmd_rank))
    app.add_handler(CommandHandler("chart_maker", cmd_chart))
    app.add_handler(CommandHandler("quotes", cmd_quotes))
    app.add_handler(CommandHandler("fast_volume", cmd_quotes))
    app.add_handler(CommandHandler("holders", cmd_holders))
    app.add_handler(CommandHandler("reactions", cmd_react))
    app.add_handler(CommandHandler("referral", cmd_ref))
    app.add_handler(CommandHandler("withdraw", cmd_wd))

    if HAS_MM:
        credentials_db.init_db()
        app.add_handler(CommandHandler("setkeys", setkeys))
        app.add_handler(CommandHandler("start_liquidity", start_liquidity))
        app.add_handler(CommandHandler("stop_liquidity", stop_liquidity))
    if HAS_BSTONK_MM:
        subscription.init_db()
        app.add_handler(CommandHandler("mm", mm_cmd))
        app.add_handler(CommandHandler("mmstop", mmstop_cmd))
        app.add_handler(CommandHandler("mmstatus", mmstatus_cmd))
        app.add_handler(CommandHandler("paid", paid_cmd))
        app.add_handler(CommandHandler("whoami", whoami_cmd))
        app.add_handler(CommandHandler("grantmm", grantmm_cmd))
    app.add_handler(CallbackQueryHandler(buttons, pattern=r"^liq:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    async def _post(app):
        await app.bot.set_my_commands(
            [
                BotCommand("start", "Main menu"),
                BotCommand("rank", "Rank"),
                BotCommand("chart_maker", "Chart Maker"),
                BotCommand("quotes", "Testnet quotes"),
                BotCommand("holders", "Holders"),
                BotCommand("reactions", "Reactions"),
                BotCommand("referral", "Earn with Ferzan"),
                BotCommand("withdraw", "Withdraw"),
                BotCommand("token", "Lookup a CA"),
                BotCommand("mm", "Run volume MM on a CA (paid)"),
                BotCommand("mmstop", "Stop your MM session"),
                BotCommand("mmstatus", "Check your MM session"),
                BotCommand("paid", "Redeem a payment tx"),
                BotCommand("whoami", "Show your Telegram user id"),
                BotCommand("help", "Help"),
            ]
        )
    app.post_init = _post
    log.info("Ferzan Liq running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
