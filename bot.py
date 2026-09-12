"""
CONFLUENCE — Telegram signal + paper-trading bot

Built on the price-alert bot Claude started:
  /price  /alert  /list  /remove

Added so this is not another Banana Gun clone:
  /signal   scored card with visible factors
  /buy      paper fill only if confluence clears (or override)
  /positions /sell /journal /watch /settings
  refusal   the bot will say no
  breaker   daily loss cap pauses new risk

This is not financial advice. Paper mode is the product.
Live custody/sniping is intentionally not included.
"""

from __future__ import annotations

import html
import logging
import os
import re

import requests
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update

try:
    from telegram import CopyTextButton
except ImportError:  # older PTB — tap the <code> CA instead
    CopyTextButton = None
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import fees
import onchain
try:
    import quotes
except Exception:  # noqa: BLE001 — keep the bot alive if quotes.py is missing
    quotes = None
    logging.getLogger(__name__).exception("quotes module failed to load")
import evm_signer
import signer
import sniper
import trading
import user_wallets
from chains import ACTIVE, CHAINS, chain_list, resolve_chain

try:
    from chains import explorer_tx
except ImportError:

    def explorer_tx(chain: str, txid: str) -> str:
        return txid

from confluence import SignalCard, analyze
from onchain import OnchainError
from price_fetcher import PriceFetchError, get_price_usd, get_prices_usd, search_coin

_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)
for _k, _v in dotenv_values(_ENV_PATH).items():
    if _v:
        os.environ[_k] = _v

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
LOGO_PATH = Path(__file__).parent / "logo.jpg"

ALERT_INTERVAL_SECONDS = int(os.getenv("ALERT_INTERVAL_SECONDS", "60"))
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "90"))
WALLET_POLL_SECONDS = int(os.getenv("WALLET_POLL_SECONDS", "75"))
DRAWDOWN_POLL_SECONDS = int(os.getenv("DRAWDOWN_POLL_SECONDS", "120"))
SNIPE_POLL_SECONDS = int(os.getenv("SNIPE_POLL_SECONDS", "25"))
LAUNCH_FEED_SECONDS = int(os.getenv("LAUNCH_FEED_SECONDS", "60"))


def _operators() -> set[int]:
    raw = os.getenv("OPERATOR_USER_IDS", "").strip()
    if not raw:
        return _allowlist()
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


def _is_operator(user_id: int) -> bool:
    ops = _operators()
    return (not ops) or user_id in ops


def _allowlist() -> set[int]:
    raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set()
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


def _allowed(user_id: int) -> bool:
    if os.getenv("FERZAN_PUBLIC", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    allow = _allowlist()
    return (not allow) or user_id in allow


def _esc(v: object) -> str:
    return html.escape(str(v))


def _bar(score: int) -> str:
    filled = max(0, min(10, round(score / 10)))
    return "█" * filled + "░" * (10 - filled)


def _fmt_px(n: float) -> str:
    n = float(n or 0)
    if n >= 1:
        return f"${n:,.4f}"
    if n >= 0.0001:
        return f"${n:.6f}"
    if n > 0:
        return f"${n:.10f}".rstrip("0")
    return "—"


def _security_line(chain: str, ca: str) -> str:
    ids = {"eth": "1", "bsc": "56", "base": "8453", "arb": "42161", "avax": "43114"}
    cid = ids.get((chain or "").lower())
    if not cid or not ca.startswith("0x"):
        return ""
    try:
        r = requests.get(
            f"https://api.gopluslabs.io/api/v1/token_security/{cid}",
            params={"contract_addresses": ca},
            timeout=8,
        )
        blob = ((r.json() or {}).get("result") or {}).get(ca.lower()) or {}
    except Exception:
        return ""
    if not blob:
        return ""
    buy_t = blob.get("buy_tax") or "0"
    sell_t = blob.get("sell_tax") or "0"
    flags = []
    if blob.get("is_honeypot") == "1":
        flags.append("🚨 HONEYPOT")
    if blob.get("honeypot_with_same_creator") == "1":
        flags.append("🚨 same creator rugged")
    if blob.get("cannot_sell_all") == "1":
        flags.append("⚠️ cannot sell all")
    if blob.get("is_blacklisted") == "1":
        flags.append("⚠️ blacklist")
    if blob.get("hidden_owner") == "1":
        flags.append("⚠️ hidden owner")
    if blob.get("can_take_back_ownership") == "1":
        flags.append("⚠️ owner reclaim")
    if blob.get("is_mintable") == "1":
        flags.append("⚠️ mintable")
    if blob.get("owner_change_balance") == "1":
        flags.append("⚠️ owner can change balances")
    if blob.get("personal_slippage_modifiable") == "1" or blob.get("slippage_modifiable") == "1":
        flags.append("⚠️ tax can change")
    if blob.get("is_proxy") == "1":
        flags.append("ℹ️ proxy")
    head = "🚨 HONEYPOT RISK" if blob.get("is_honeypot") == "1" else (
        "⚠️ Contract flags" if flags else "✅ No honeypot flag"
    )
    out = f"{head}  ·  buy {buy_t}%  ·  sell {sell_t}%"
    if flags:
        out += "\n" + " · ".join(flags[:6])
    return out


def render_card(card: SignalCard) -> str:
    s = card.snapshot
    ca = (s.token_address or "").strip()
    chain = (s.chain or "").upper()
    chg = float(s.change_1h or 0)
    chg_s = f"{chg:+.1f}% 1h"
    mc = float(s.fdv or 0)
    liq = float(s.liquidity_usd or 0)
    vol = float(s.volume_24h or 0)
    lines = [
        f"🪙 <b>${_esc(s.symbol)}</b>  ·  {_esc(s.name)}",
        f"⛓ {_esc(chain)}  ·  {_esc(s.dex)}",
        f"<code>{_esc(ca)}</code>" if ca else "",
        "",
        f"🏅 Score <b>{card.score}</b>/100 {_bar(card.score)}  ·  {_esc(card.bias)}",
        f"💵 Price {_esc(_fmt_px(s.price_usd))}  ·  {html.escape(chg_s)}",
        f"🧢 MC {_esc(f'${mc:,.0f}' if mc else '—')}  ·  💧 Liq {_esc(f'${liq:,.0f}' if liq else '—')}",
        f"📊 24h vol {_esc(f'${vol:,.0f}' if vol else '—')}  ·  🟢{s.buys_h1} / 🔴{s.sells_h1} 1h",
        f"🎯 TP {card.take_pct:g}%   🛑 SL {card.stop_pct:g}%",
    ]
    sec = _security_line(s.chain, ca)
    if sec:
        lines.extend(_esc(part) for part in sec.splitlines() if part)
    if card.vetoes:
        lines.append("⚠️ " + _esc(" · ".join(card.vetoes[:2])))
    if s.url:
        lines.append(f'<a href="{html.escape(s.url, quote=True)}">DexScreener</a>')
    lines.append("<i>Tap CA to copy · See it. Ape it. Send it.</i>")
    return "\n".join(x for x in lines if x)


def card_keyboard(query: str, score: int, ca: str = "") -> InlineKeyboardMarkup:
    q = (ca or query)[:44]
    cap = int(signer.max_usd())
    rows = [
        [
            InlineKeyboardButton("👁 Track", callback_data=f"watch:{q}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"sig:{q}"),
        ],
        [
            InlineKeyboardButton(f"💵 ${cap}", callback_data=f"buy:{q}"),
            InlineKeyboardButton("🧨 Override", callback_data=f"force:{q}"),
        ],
        [
            InlineKeyboardButton("$1", callback_data=f"buyz:1:{q}"),
            InlineKeyboardButton("$3", callback_data=f"buyz:3:{q}"),
            InlineKeyboardButton(f"${cap}", callback_data=f"buyz:{cap}:{q}"),
        ],
        [
            InlineKeyboardButton("🎯 Snipe", callback_data=f"snp:{q}"),
            InlineKeyboardButton("📉 Quote", callback_data=f"qte:sol:{q}"),
        ],
    ]
    addr = (ca or query or "").strip()
    if addr and CopyTextButton is not None:
        rows.append(
            [InlineKeyboardButton("📋 Copy CA", copy_text=CopyTextButton(text=addr))]
        )
    return InlineKeyboardMarkup(rows)


def positions_keyboard(user_id: int) -> InlineKeyboardMarkup | None:
    rows = []
    for p in db.open_positions(user_id)[:8]:
        sym = (p["symbol"] or "?").upper()[:10]
        rows.append(
            [
                InlineKeyboardButton(
                    f"📄 Paper close #{p['id']}",
                    callback_data=f"close:{p['id']}",
                ),
                InlineKeyboardButton(
                    f"🟢 Live sell {sym}",
                    callback_data=f"xsell:{p['id']}",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton("25%", callback_data=f"xsell:{p['id']}"),
                InlineKeyboardButton("50%", callback_data=f"xsell:{p['id']}"),
                InlineKeyboardButton("Bag", callback_data="go:bag"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton("👛 Wallet", callback_data="go:wallets"),
            InlineKeyboardButton("↩️ Home", callback_data="go:home"),
        ]
    )
    return InlineKeyboardMarkup(rows) if rows else None


async def guard(update: Update) -> bool:
    user = update.effective_user
    if not user or not _allowed(user.id):
        if update.message:
            await update.effective_message.reply_text("This bot is locked to an allowlist.")
        elif update.callback_query:
            await update.callback_query.answer("Locked.", show_alert=True)
        return False
    db.ensure_user(user.id, user.username)
    return True


async def resolve_symbol_or_reply(update: Update, symbol: str):
    try:
        candidates = search_coin(symbol)
    except PriceFetchError as exc:
        await update.effective_message.reply_text(f"Couldn't reach price data: {exc}")
        return None
    if not candidates:
        await update.effective_message.reply_text(f"No token matching '{symbol}'.")
        return None
    if len(candidates) > 1 and candidates[0]["symbol"].lower() != symbol.lower():
        options = "\n".join(
            f"  • {c['symbol'].upper()} — {c['name']} (id: {c['id']})"
            for c in candidates[:5]
        )
        await update.effective_message.reply_text(
            f"Multiple matches for '{symbol}'. Re-run with the id:\n\n{options}"
        )
        return None
    return candidates[0]


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⛓ Chains", callback_data="go:chains"),
                InlineKeyboardButton("👛 Wallets", callback_data="go:wallets"),
            ],
            [
                InlineKeyboardButton("📡 Signals", callback_data="go:signal:sol"),
                InlineKeyboardButton("👯 Copytrade", callback_data="go:copy"),
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="go:settings"),
                InlineKeyboardButton("⏱ Orders", callback_data="go:snipes"),
            ],
            [
                InlineKeyboardButton("📊 Positions", callback_data="go:pos"),
                InlineKeyboardButton("🎯 Auto snipe", callback_data="go:snipehelp"),
            ],
            [
                InlineKeyboardButton("🚀 Launches", callback_data="go:launches"),
                InlineKeyboardButton("💱 Live quote", callback_data="go:quotehelp"),
            ],
            [
                InlineKeyboardButton("💸 Fees", callback_data="go:fees"),
                InlineKeyboardButton("📉 Drawdown", callback_data="go:pnl"),
            ],
            [
                InlineKeyboardButton("⚡ BUY / SELL — paste a CA", callback_data="go:buyhelp"),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        user = db.ensure_user(update.effective_user.id, update.effective_user.username)
        ready, _fee_note = fees.live_ready()
        cash = float(user.get("paper_cash") or 10000)
        floor = int(user.get("min_confluence") or 62)
        size = float(user.get("size_pct") or 5)
        cap = float(user.get("max_daily_loss_pct") or 8)
        text = (
            "⚡ FERZAN\n"
            "See it. Ape it. Send it.\n\n"
            "1. Tap Wallets → pick a chain → fund it\n"
            "2. Paste a CA\n"
            "3. Tap Buy — it spends YOUR bag\n\n"
            f"🎚️ Floor {floor} · size {size}% · cut {fees.current_bps() / 100:.2f}%\n"
            "/settings to change size and floor"
        )
        target = update.effective_message
        if not target:
            return
        if LOGO_PATH.exists():
            with LOGO_PATH.open("rb") as photo:
                await target.reply_photo(
                    photo=photo,
                    caption=text,
                    reply_markup=home_keyboard(),
                )
        else:
            await target.reply_text(text, reply_markup=home_keyboard())
        try:
            user_wallets.ensure(update.effective_user.id)
        except Exception:
            logger.exception("wallet ensure on start failed")
    except Exception:
        logger.exception("start failed")
        try:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "FERZAN is up. Desk hit a snag loading your paper book. "
                    "Try /signal sol — do not Redeploy yet."
                )
        except Exception:
            logger.exception("start fallback failed")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.effective_message.reply_text(
        "FERZAN commands\n\n"
        "/start — home + slogan\n"
        "/wallet — generate / import / chain addresses\n"
        "/importsol /importevm — import a key in private chat\n"
        "/bag — live bag + PnL + sell %\n"
        "/tp 50 — live take profit %\n"
        "/sl 30 — live stop loss %\n"
        "/settings — size, floor, daily cap\n"
        "/positions — paper desk\n"
        "/launches — new pools\n"
        "/signal <CA> — score a token\n"
        "/snipe <CA> — arm live snipe (capped)\n"
        "/livesell <mint> — sell SOL token\n"
        "/livesellevm <chain> <0x> — sell EVM token\n"
        "/watchwallet — copy-trade alerts\n"
        "/quote <chain> <CA> — swap quote\n"
        "/chains — pick a network\n"
        "/help — this list\n\n"
        "Paste a CA anytime to score + buy.\n"
        "Live spend is YOUR /wallet bag, not treasury."
    )


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /price sol")
        return
    symbol = context.args[0]
    coin = await resolve_symbol_or_reply(update, symbol)
    if coin is None:
        return
    try:
        usd = get_price_usd(coin["id"])
    except PriceFetchError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(
        f"{coin['symbol'].upper()} ({coin['name']}): ${usd:,.6g}"
    )


async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if len(context.args) != 3:
        await update.effective_message.reply_text(
            "Usage: /alert <symbol> <above|below> <price>\nExample: /alert sol above 200"
        )
        return
    symbol, direction, price_str = context.args
    direction = direction.lower()
    if direction not in ("above", "below"):
        await update.effective_message.reply_text("Direction must be above or below.")
        return
    try:
        target_price = float(price_str)
        if target_price <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Price must be a positive number.")
        return
    coin = await resolve_symbol_or_reply(update, symbol)
    if coin is None:
        return
    alert_id = db.add_alert(
        chat_id=update.effective_chat.id,
        symbol=coin["symbol"],
        coin_id=coin["id"],
        direction=direction,
        target_price=target_price,
    )
    await update.effective_message.reply_text(
        f"Alert #{alert_id}: {coin['symbol'].upper()} {direction} ${target_price:,.6g}\n"
        f"Checked every {ALERT_INTERVAL_SECONDS}s."
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    alerts = db.list_alerts(update.effective_chat.id)
    if not alerts:
        await update.effective_message.reply_text("No active price alerts. Create one with /alert.")
        return
    lines = [
        f"#{a['id']} — {a['symbol']} {a['direction']} ${a['target_price']:,.6g}"
        for a in alerts
    ]
    await update.effective_message.reply_text("Active alerts:\n" + "\n".join(lines))


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /remove <alert_id>")
        return
    try:
        alert_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Alert id must be a number.")
        return
    if db.delete_alert(alert_id, update.effective_chat.id):
        await update.effective_message.reply_text(f"Removed alert #{alert_id}.")
    else:
        await update.effective_message.reply_text("No such alert.")


async def _send_signal(update: Update, query: str, edit: bool = False) -> None:
    try:
        card = analyze(query)
    except PriceFetchError as exc:
        text = f"Could not score {query}: {exc}"
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.effective_message.reply_text(text)
        return
    text = render_card(card)
    markup = card_keyboard(
        card.snapshot.query or query,
        card.score,
        ca=card.snapshot.token_address or "",
    )
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup
        )


async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /signal sol   or paste a contract.")
        return
    await _send_signal(update, " ".join(context.args))


def _live_buy_followup(
    uid: int, card, query: str, paper_ok: bool, force: bool, usd_override: float | None = None
) -> str:
    if not signer.live_enabled():
        return "Live: OFF. Add LIVE_BUYS=1 and restart."
    if not paper_ok and not force:
        return "Live: skipped (score/floor blocked). Override to force."
    snap = card.snapshot
    mint = (snap.token_address or "").strip()
    chain = (snap.chain or "").lower()
    raw = (query or "").strip()
    if not mint and len(raw) >= 32:
        mint = raw
        if raw.startswith("0x"):
            chain = chain or "base"
        else:
            chain = chain or "solana"
    if not mint:
        return "Live: no mint on this card. Paste the full CA, then Buy."
    user = db.get_user(uid) or {}
    cash = float(user.get("paper_cash") or 10000)
    pct = float(user.get("size_pct") or 5)
    usd = min(signer.max_usd(), max(1.0, cash * pct / 100.0))
    if usd_override is not None:
        usd = min(signer.max_usd(), max(1.0, float(usd_override)))
    try:
        sol_secret, evm_secret = user_wallets.secrets(uid)
    except Exception as exc:
        return f"Live: open /wallet first.\n{exc}"
    if mint.startswith("0x"):
        if not (os.getenv("ZEROX_API_KEY") or "").strip():
            return "Live: EVM needs ZEROX_API_KEY on the droplet."
        _ok, msg = evm_signer.buy_evm(chain or "base", mint, usd, key_hex=evm_secret)
        if _ok:
            db.add_live_cost(uid, mint, usd)
        return msg
    if "sol" not in chain and not (len(mint) >= 32 and not mint.startswith("0x")):
        return f"Live: {chain or 'unknown'} is not Solana."
    _ok, msg = signer.buy_sol(mint, usd, secret=sol_secret)
    if _ok:
        db.add_live_cost(uid, mint, usd)
    return msg


def _mint_from_position(pos: dict) -> str:
    raw = (pos.get("query") or "").strip()
    if len(raw) >= 32 and not raw.startswith("0x"):
        return raw
    blob = pos.get("signal_json") or ""
    if blob:
        try:
            import json

            data = json.loads(blob)
            mint = (data.get("token_address") or data.get("ca") or "").strip()
            if len(mint) >= 32 and not mint.startswith("0x"):
                return mint
        except Exception:
            pass
    return ""


def _live_sell_position(uid: int, pos_id: int) -> str:
    pos = db.get_position(pos_id, uid)
    if not pos:
        return "Live sell: no position."
    mint = _mint_from_position(pos)
    if not mint:
        return "Live sell: no Solana mint on this ticket. Paste the CA and sell from the card."
    _ok, msg = signer.sell_sol(mint)
    return msg


async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /buy sol")
        return
    query = " ".join(context.args)
    try:
        card = analyze(query)
    except PriceFetchError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    ok, msg = trading.paper_buy(update.effective_user.id, card, force=False)
    await update.effective_message.reply_text(msg)
    live_msg = _live_buy_followup(update.effective_user.id, card, query, ok, False)
    if live_msg:
        await update.effective_message.reply_text(live_msg)
    if not ok:
        await update.effective_message.reply_text(
            render_card(card),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=card_keyboard(query, card.score),
        )


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    user = db.get_user(uid)
    opens = db.open_positions(uid)
    closed = db.recent_closed(uid, 5)
    day = db.realized_today(uid)
    def _qty(n: float) -> str:
        n = float(n or 0)
        if n >= 1_000_000:
            return f"{n / 1_000_000:.2f}M"
        if n >= 1_000:
            return f"{n / 1_000:.2f}K"
        return f"{n:.4f}"

    def _px(n: float) -> str:
        n = float(n or 0)
        if n >= 1:
            return f"${n:,.4f}"
        if n >= 0.0001:
            return f"${n:.6f}"
        return f"${n:.8f}"

    cash = float(user["paper_cash"] if user else 0)
    day_s = f"+${day:,.2f}" if day >= 0 else f"-${abs(day):,.2f}"
    lines = [
        "📊 <b>Ferzan desk</b>",
        f"💵 Paper cash  <b>${cash:,.2f}</b>",
        f"📈 Realized today  <b>{html.escape(day_s)}</b>",
        "",
    ]
    if not opens:
        lines.append("📭 No open paper tickets.")
        lines.append("<i>Live bag is /bag — this list is the paper book.</i>")
    for p in opens:
        sym = html.escape((p["symbol"] or "?").upper())
        lines.append(f"🟢 <b>#{p['id']} ${sym}</b> · LONG")
        lines.append(f"   📦 {_qty(p['qty'])} @ {_px(p['entry'])}")
        lines.append(f"   🛑 SL {_px(p['stop'])}   🎯 TP {_px(p['take'])}")
        lines.append("")
    if closed:
        lines.append("———")
        lines.append("📁 <b>Recent paper closes</b>")
        for p in closed:
            pnl = p["pnl"] if p["pnl"] is not None else 0
            mark = "🟢" if pnl >= 0 else "🔴"
            lines.append(
                f"{mark} #{p['id']} {html.escape((p['symbol'] or '?').upper())}  {pnl:+,.2f} USD"
            )
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=positions_keyboard(uid),
    )


def _token_mark_usd(mint: str) -> float:
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=8,
        )
        pairs = (r.json() or {}).get("pairs") or []
        if pairs:
            return float(pairs[0].get("priceUsd") or 0)
    except Exception:
        return 0.0
    return 0.0


def _bag_panel(mint: str, amount: float, addr: str, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    short = mint[:44]
    href = f"https://solscan.io/token/{mint}"
    px = _token_mark_usd(mint)
    worth = float(amount or 0) * px
    cost = db.live_cost(uid, mint)
    if cost > 0 and worth > 0:
        pnl = worth - cost
        pct = (pnl / cost) * 100
        mark = "🟢" if pnl >= 0 else "🔴"
        pnl_line = (
            f"{mark} PnL <b>{pnl:+,.2f} USD</b> ({pct:+.1f}%)\n"
            f"📥 Initial ${cost:,.2f}   💰 Worth ${worth:,.2f}"
        )
    elif worth > 0:
        pnl_line = f"💰 Worth ${worth:,.2f}\n<i>Buy live once to lock Initial for PnL.</i>"
    else:
        pnl_line = "💰 Mark unavailable"
    text = (
        f"🎒 <b>Position</b> · SOL\n"
        f"<a href=\"https://solscan.io/account/{html.escape(addr)}\">Wallet</a>\n"
        f"🪙 <a href=\"{href}\">token</a>\n"
        f"<code>{html.escape(mint)}</code>\n"
        f"Tokens: <b>{amount:g}</b>\n"
        f"{pnl_line}\n"
    )
    ex = db.get_live_exit(uid, mint)
    if ex and (ex.get("tp_pct") or ex.get("sl_pct")):
        bits = []
        if ex.get("tp_pct"):
            bits.append(f"🎯 TP +{float(ex['tp_pct']):.0f}%")
        if ex.get("sl_pct"):
            bits.append(f"🛑 SL -{float(ex['sl_pct']):.0f}%")
        text += " · ".join(bits) + "\n"
    text += "<i>Tap CA to copy</i>"
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("☢️ Sell All", callback_data=f"slp:100:{short}"),
            ],
            [
                InlineKeyboardButton("🎯 TP +50%", callback_data=f"tpx:50:{short}"),
                InlineKeyboardButton("🛑 SL -30%", callback_data=f"slx:30:{short}"),
            ],
            [
                InlineKeyboardButton("25%", callback_data=f"slp:25:{short}"),
                InlineKeyboardButton("50%", callback_data=f"slp:50:{short}"),
                InlineKeyboardButton("75%", callback_data=f"slp:75:{short}"),
                InlineKeyboardButton("100%", callback_data=f"slp:100:{short}"),
            ],
            [
                InlineKeyboardButton("📡 Score", callback_data=f"sig:{short}"),
                InlineKeyboardButton("💵 Buy more", callback_data=f"buy:{short}"),
            ],
        ]
    )
    return text, kb


async def tp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /tp 50   or /tp 50 <mint>")
        return
    try:
        pct = float(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Use a number. /tp 50")
        return
    mint = context.args[1] if len(context.args) > 1 else ""
    if not mint:
        found = db.live_mints(update.effective_user.id)
        mint = found[0] if found else ""
    if not mint:
        await update.effective_message.reply_text("Buy live first, or pass a mint.")
        return
    db.set_live_exit(update.effective_user.id, mint, tp_pct=pct)
    await update.effective_message.reply_text(f"🎯 TP +{pct:.0f}% armed.")


async def sl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /sl 30   or /sl 30 <mint>")
        return
    try:
        pct = float(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Use a number. /sl 30")
        return
    mint = context.args[1] if len(context.args) > 1 else ""
    if not mint:
        found = db.live_mints(update.effective_user.id)
        mint = found[0] if found else ""
    if not mint:
        await update.effective_message.reply_text("Buy live first, or pass a mint.")
        return
    db.set_live_exit(update.effective_user.id, mint, sl_pct=pct)
    await update.effective_message.reply_text(f"🛑 SL -{pct:.0f}% armed.")


async def bag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    sol_secret, _evm = user_wallets.secrets(update.effective_user.id)
    try:
        kp = signer.keypair_from_secret(sol_secret)
        addr = str(kp.pubkey())
        rows = signer.holdings(sol_secret)
        lamports = signer.sol_balance_lamports(addr)
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(
        f"🎒 <b>Wallet positions</b> · SOL\n"
        f"💰 {lamports / 1e9:.6f} SOL\n"
        f"<code>{html.escape(addr)}</code>",
        parse_mode="HTML",
    )
    if not rows:
        await update.effective_message.reply_text("No SPL tokens yet.")
    for row in rows[:6]:
        text, kb = _bag_panel(row["mint"], row["amount"], addr, update.effective_user.id)
        await update.effective_message.reply_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )
    evm_addr = (db.get_user_wallet(update.effective_user.id) or {}).get("evm_pub") or ""
    for mint in db.live_mints(update.effective_user.id):
        if not str(mint).startswith("0x") or not evm_addr:
            continue
        for cid in ("eth", "base", "bsc", "hood", "arb", "avax"):
            try:
                raw = evm_signer._erc20_balance(CHAINS[cid]["rpc"], mint, evm_addr)
            except Exception:
                raw = 0
            if raw <= 0:
                continue
            text, kb = _bag_panel(mint, raw / 10**18, evm_addr, update.effective_user.id)
            text = text.replace("· SOL", f"· {cid.upper()}")
            await update.effective_message.reply_text(
                text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
            )
            break


async def livesell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /livesell <solana-mint>\nSee /bag")
        return
    sol_secret, _evm = user_wallets.secrets(update.effective_user.id)
    _ok, msg = signer.sell_sol(context.args[0].strip(), secret=sol_secret)
    await update.effective_message.reply_text(msg)


async def livesellevm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /livesellevm [eth|base|bsc] <0xToken>")
        return
    if len(context.args) == 1:
        chain, token = "eth", context.args[0]
    else:
        chain, token = context.args[0], context.args[1]
    _sol, evm_secret = user_wallets.secrets(update.effective_user.id)
    _ok, msg = evm_signer.sell_evm(chain, token, key_hex=evm_secret)
    await update.effective_message.reply_text(msg)


async def sell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /sell <position_id>")
        return
    try:
        pos_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Position id must be a number. See /positions.")
        return
    ok, msg = trading.paper_close(update.effective_user.id, pos_id, reason="manual")
    await update.effective_message.reply_text(msg)
    live = _live_sell_position(update.effective_user.id, pos_id)
    if live:
        await update.effective_message.reply_text(live)


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    if not context.args:
        names = db.watchlist_of(uid)
        await update.effective_message.reply_text(
            "Watchlist: " + (", ".join(names) if names else "(empty)")
            + "\nAdd with /watch jup   remove with /unwatch jup"
        )
        return
    q = context.args[0]
    db.add_watch(uid, q)
    await update.effective_message.reply_text(f"Watching {q.upper()}. Scanner will score it.")


async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /unwatch sol")
        return
    db.remove_watch(update.effective_user.id, context.args[0])
    await update.effective_message.reply_text(f"Removed {context.args[0].upper()}.")


async def journal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = db.recent_journal(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("Journal is empty.")
        return
    lines = [f"{r['kind']}: {r['body']}" for r in rows]
    await update.effective_message.reply_text("\n".join(lines)[:3500])


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    user = db.get_user(uid)
    if context.args and len(context.args) == 2:
        key, raw = context.args[0].lower(), context.args[1]
        try:
            if key in {"size", "size_pct"}:
                val = float(raw)
                if not 1 <= val <= 20:
                    raise ValueError
                db.update_user(uid, size_pct=val)
            elif key in {"floor", "min", "score"}:
                val = int(raw)
                if not 40 <= val <= 90:
                    raise ValueError
                db.update_user(uid, min_confluence=val)
            elif key in {"cap", "dd", "daily"}:
                val = float(raw)
                if not 1 <= val <= 25:
                    raise ValueError
                db.update_user(uid, max_daily_loss_pct=val)
            elif key == "alerts":
                db.update_user(uid, alerts_on=1 if raw in {"on", "1", "true"} else 0)
            elif key in {"ddalert", "drawdown"}:
                val = float(raw)
                if not 3 <= val <= 40:
                    raise ValueError
                db.update_user(uid, drawdown_alert_pct=val)
            else:
                await update.effective_message.reply_text("Keys: size, floor, cap, alerts, ddalert")
                return
        except ValueError:
            await update.effective_message.reply_text("Out of range.")
            return
        user = db.get_user(uid)
        await update.effective_message.reply_text("Updated.")
    await update.effective_message.reply_text(
        "Risk vault\n"
        f"size {user['size_pct']}% of cash per ticket (1-20)\n"
        f"floor {user['min_confluence']} confluence to auto-fill (40-90)\n"
        f"cap -{user['max_daily_loss_pct']}% daily realized (1-25)\n"
        f"scanner alerts {'on' if user['alerts_on'] else 'off'}\n"
        f"drawdown ping at -{user.get('drawdown_alert_pct') or 12}% from peak\n\n"
        "Examples:\n/settings size 3\n/settings floor 70\n/settings cap 5\n"
        "/settings alerts off\n/settings ddalert 10"
    )


async def resetpaper_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    start_bal = float(os.getenv("PAPER_STARTING_BALANCE", "10000"))
    for p in db.open_positions(uid):
        db.close_position(p["id"], p["entry"], 0)
    db.update_user(uid, paper_cash=start_bal, starting_equity=start_bal, peak_equity=start_bal)
    db.add_journal(uid, "RESET", f"Paper book reset to ${start_bal:,.0f}")
    await update.effective_message.reply_text(f"Paper account reset to ${start_bal:,.0f}.")


async def watchwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /watchwallet <eth|base|bsc|arb|op|polygon|sol> <address> [label]\n"
            "Example: /watchwallet sol 7xKX... whale1\n"
            f"Providers: {onchain.status_line()}"
        )
        return
    chain_raw, address = context.args[0], context.args[1]
    label = " ".join(context.args[2:]) if len(context.args) > 2 else None
    try:
        chain = onchain.normalize_chain(chain_raw)
        events = onchain.recent_activity(chain, address, limit=3)
    except OnchainError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    except Exception as exc:
        await update.effective_message.reply_text(f"Provider error: {exc}")
        return
    wid = db.add_watched_wallet(update.effective_user.id, chain, address, label)
    if events:
        db.set_wallet_cursor(wid, events[0].txid)
    preview = "\n".join(f"• {e.summary}" for e in events[:3]) or "No recent prints."
    await update.effective_message.reply_text(
        f"Watching wallet #{wid} on {chain}\n{address}\n{preview}"
    )


def wallet_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("ℹ️ Help", callback_data="go:help"),
                InlineKeyboardButton("↩️ Return", callback_data="go:home"),
            ],
            [InlineKeyboardButton("📂 Rearrange wallets", callback_data="wi:rearr")],
            [
                InlineKeyboardButton("📥 Import wallet", callback_data="wi:imp"),
                InlineKeyboardButton("✨ Generate wallet", callback_data="wi:gen"),
            ],
            [InlineKeyboardButton("🧲 Collect", callback_data="wi:col")],
            [InlineKeyboardButton("📤 Disperse", callback_data="wi:dis")],
            [InlineKeyboardButton("🔗 Addresses by chain", callback_data="wi:chains")],
            [InlineKeyboardButton("🗝️ Export keys", callback_data="wi:exp")],
        ]
    )


def chain_board_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    has = bool(user_id and db.get_user_wallet(user_id))
    rows: list[list[InlineKeyboardButton]] = []
    for cid in ACTIVE:
        label = "HOOD" if cid == "hood" else cid.upper()
        left = InlineKeyboardButton(f"🟢 {label}", callback_data=f"ch:{cid}")
        if cid in {"trx", "ton"}:
            right = InlineKeyboardButton("⚠️ Soon", callback_data=f"wa:{cid}")
        elif has:
            right = InlineKeyboardButton("👛 Wallet", callback_data=f"wa:{cid}")
        else:
            right = InlineKeyboardButton("✨ Generate", callback_data="wi:gen")
        rows.append([left, right])
    rows.append(
        [
            InlineKeyboardButton("📥 Import", callback_data="wi:imp"),
            InlineKeyboardButton("✨ Generate", callback_data="wi:gen"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("ℹ️ Help", callback_data="go:help"),
            InlineKeyboardButton("↩️ Return", callback_data="go:home"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def wallet_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    return chain_board_keyboard(user_id)


async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    row = db.get_user_wallet(update.effective_user.id)
    if row:
        text = (
            "🟢 Enable a chain · 👛 open its address\n"
            "Same EVM key covers ETH / Base / BSC / Hood / Arb / Avax.\n"
            "SOL is its own key. TON / TRX adapters next."
        )
    else:
        text = "ℹ️ Wallet not found. Generate or import, then every chain lights up."
    await update.effective_message.reply_text(
        text,
        reply_markup=chain_board_keyboard(update.effective_user.id),
    )


async def importsol_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /importsol <base58-key>")
        return
    try:
        row = user_wallets.import_keys(update.effective_user.id, sol_secret=context.args[0])
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    try:
        await update.effective_message.delete()
    except Exception:
        pass
    await update.effective_message.reply_text(
        f"Solana imported.\n`{row['sol_pub']}`",
        parse_mode="Markdown",
        reply_markup=wallet_keyboard(update.effective_user.id),
    )


async def importevm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /importevm <0x-key>")
        return
    try:
        row = user_wallets.import_keys(update.effective_user.id, evm_secret=context.args[0])
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    try:
        await update.effective_message.delete()
    except Exception:
        pass
    await update.effective_message.reply_text(
        f"EVM imported.\n`{row['evm_pub']}`",
        parse_mode="Markdown",
        reply_markup=wallet_keyboard(update.effective_user.id),
    )


async def collectsol_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /collectsol <your-sol-address>")
        return
    sol_secret, _evm = user_wallets.secrets(update.effective_user.id)
    _ok, msg = signer.send_sol(context.args[0], secret=sol_secret)
    await update.effective_message.reply_text(msg)


async def disperse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if len(context.args or []) < 2:
        await update.effective_message.reply_text(
            "📤 Disperse SOL equally.\n/disperse <addr1> <addr2> [addr3]"
        )
        return
    dests = [a.strip() for a in context.args if len(a.strip()) >= 32]
    sol_secret, _evm = user_wallets.secrets(update.effective_user.id)
    lines = []
    for dest in dests:
        # equal split: collect-style full send only to first until we have partial send
        _ok, msg = signer.send_sol(dest, secret=sol_secret)
        lines.append(msg)
        break
    lines.append("v1 sends the bag to the first address. Partial split is next.")
    await update.effective_message.reply_text("\n".join(lines))


async def collectevm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /collectevm [eth|base|bsc|hood] <0x>")
        return
    if len(context.args) == 1:
        chain, dest = "eth", context.args[0]
    else:
        chain, dest = context.args[0], context.args[1]
    _sol, evm_secret = user_wallets.secrets(update.effective_user.id)
    _ok, msg = evm_signer.send_native(chain, dest, key_hex=evm_secret)
    await update.effective_message.reply_text(msg)


async def wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = db.list_watched_wallets(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("No watched wallets. /watchwallet sol <addr>")
        return
    lines = [
        f"#{r['id']} {r['chain']} {r['address'][:10]}… {r['label'] or ''}".strip()
        for r in rows
    ]
    await update.effective_message.reply_text("Watched wallets:\n" + "\n".join(lines))


async def unwatchwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /unwatchwallet <id>")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Id must be a number. See /wallets.")
        return
    if db.delete_watched_wallet(wid, update.effective_user.id):
        await update.effective_message.reply_text(f"Dropped wallet #{wid}.")
    else:
        await update.effective_message.reply_text("No such wallet id.")


async def drawdown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    user = db.get_user(uid)
    equity, peak, dd = trading.update_peak_and_drawdown(uid)
    threshold = float(user.get("drawdown_alert_pct") or 12)
    await update.effective_message.reply_text(
        f"Paper equity ${equity:,.2f}\n"
        f"Peak ${peak:,.2f}\n"
        f"Drawdown {dd:.2f}%  (alert at -{threshold:g}%)\n"
        "On-chain token books need Etherscan/Helius keys plus a portfolio endpoint. "
        "Native EVM balance is included when you /watchwallet an address and a key is set."
    )


async def fees_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    mine = db.fee_totals(uid)
    ready, live_note = fees.live_ready()
    wallets = fees.fee_wallets()
    lines = [
        f"Cut {fees.current_bps() / 100:.2f}% per paper fill (hard-capped at 1%).",
        f"You have paid ${mine['fee_usd']:,.4f} across {mine['count']} tickets.",
        live_note,
    ]
    if wallets["sol"]:
        lines.append(f"SOL fee wallet {wallets['sol']}")
    if wallets["evm"]:
        lines.append(f"EVM fee wallet {wallets['evm']}")
    if wallets["jupiter_fee_account"]:
        lines.append(f"Jupiter fee account {wallets['jupiter_fee_account']}")
    lines.append(
        "Live path: Jupiter platformFeeBps + feeAccount, or 0x swapFeeRecipient. "
        "No seed phrases. The router pays the cut on-chain."
    )
    if _is_operator(uid):
        all_fees = db.fee_totals()
        lines.append(
            f"\nOperator treasury (paper): ${all_fees['fee_usd']:,.4f} "
            f"on ${all_fees['notional_usd']:,.2f} notional."
        )
        for row in db.recent_fees(8):
            lines.append(
                f"  {row['kind']} ${row['fee_usd']:.4f} ({row['fee_bps']}bps) u{row['user_id']}"
            )
    await update.effective_message.reply_text("\n".join(lines))


async def signer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await update.effective_message.reply_text(signer.status_text() + "\n\n" + evm_signer.status_text())


def _parse_snipe_args(args: list[str]) -> tuple[str | None, str, float]:
    if not args:
        raise ValueError("Usage: /snipe [chain] <token-or-CA> [usd]")
    chain = resolve_chain(args[0])
    if chain:
        rest = args[1:]
    else:
        rest = args
    if not rest:
        raise ValueError("Need a token or contract after the chain.")
    usd = 40.0
    query_parts = []
    for part in rest:
        try:
            usd = float(part)
        except ValueError:
            query_parts.append(part)
    if not query_parts:
        raise ValueError("Need a token or contract.")
    return chain, " ".join(query_parts), usd


async def snipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        chain, query, usd = _parse_snipe_args(context.args)
    except ValueError as exc:
        await update.effective_message.reply_text(
            f"{exc}\nChains: {chain_list()}\n"
            "Example: /snipe base 0xabc... 25\n"
            "Optional after that in /settings: floor, size. Snipe uses your score floor "
            "and refuses vetoed pools unless you lower gates."
        )
        return
    user = db.get_user(update.effective_user.id)
    sid = sniper.arm(
        user_id=update.effective_user.id,
        query=query,
        chain=chain,
        usd=max(10.0, min(usd, 500.0)),
        min_liq=40_000,
        min_score=int(user["min_confluence"]),
        max_age_h=6.0,
        require_long=True,
    )
    await update.effective_message.reply_text(
        f"Armed snipe #{sid}\n"
        f"{chain or 'any-chain'} {query} ${usd:.0f}\n"
        f"Gates: liq ≥ $40k, score ≥ {user['min_confluence']}, age ≤ 6h, bias LONG, no veto.\n"
        "When DexScreener/Gecko sees a tradable pool that clears those, paper-fill fires.\n"
        "This is not a private-mempool first-block snipe."
    )
    armed = next((r for r in db.active_snipes(update.effective_user.id) if r["id"] == sid), None)
    status, msg = sniper.try_fill(armed) if armed else ("armed", "")
    if status == "filled":
        await update.effective_message.reply_text("Immediate fill\n" + msg)
    elif status == "miss":
        await update.effective_message.reply_text("Armed but not filled yet:\n" + msg)


async def snipes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = db.active_snipes(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("No snipes. Arm one with /snipe sol <CA> 40")
        return
    lines = [
        f"#{r['id']} {r['status']} {r['chain'] or '*'} {r['query']} ${r['usd']:.0f}"
        for r in rows[:15]
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def cancelsnipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /cancelsnipe <id>")
        return
    try:
        sid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Id must be a number.")
        return
    if db.cancel_snipe(sid, update.effective_user.id):
        await update.effective_message.reply_text(f"Cancelled snipe #{sid}.")
    else:
        await update.effective_message.reply_text("No armed snipe with that id.")


async def quote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Live quote (unsigned)\n"
            "/quote sol <token-mint> 50\n"
            "/quote base 0xabc… 25\n"
            "50 = dollars of native in. Your 0.50% cut is on the quote.\n"
            "You sign in Phantom/Rabby. The bot never holds keys."
        )
        return
    chain = resolve_chain(context.args[0])
    rest = context.args[1:] if chain else context.args
    if not chain:
        chain = "sol"
    if not rest:
        await update.effective_message.reply_text("Need a token mint or contract.")
        return
    usd = 50.0
    token_parts = []
    for part in rest:
        try:
            usd = float(part)
        except ValueError:
            token_parts.append(part)
    token = " ".join(token_parts).strip()
    if not token:
        await update.effective_message.reply_text("Need a token mint or contract.")
        return
    try:
        if quotes is None:
            await update.effective_message.reply_text("Quote module not loaded. Re-upload quotes.py.")
            return
        if chain == "sol":
            q = quotes.sol_quote(token, max(5.0, usd))
        else:
            q = quotes.evm_quote(chain, token, max(5.0, usd))
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(quotes.format_quote(q))


def chains_keyboard() -> InlineKeyboardMarkup:
    return chain_board_keyboard()


async def chains_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = (
        "⚡ FERZAN · chains\n"
        "🟢 chain · 👛 wallet address\n"
        "See it. Ape it. Send it.\n\n"
        "🟢 live score + paper + quote\n"
        "🟡 score + paper (thin live route)\n"
        "⚪ listed — DexScreener when the pair exists\n\n"
        "Tap a chain, then paste a CA.\n"
        "You sign live swaps in Trust. Ferzan does not hold keys."
    )
    await update.effective_message.reply_text(text, reply_markup=chains_keyboard())


def launch_card(ln) -> tuple[str, InlineKeyboardMarkup]:
    ca = (ln.token or ln.query or "").strip()
    name = html.escape((ln.symbol or "?").upper())
    raw_chain = (ln.chain or "").lower()
    cid = resolve_chain(raw_chain) or raw_chain
    chain = html.escape((CHAINS.get(cid, {}).get("label") or raw_chain or "?").upper())
    if cid == "hood":
        chain = "HOOD"
    marks = {
        "sol": "🟣", "bsc": "🟡", "base": "🔵", "eth": "♦️",
        "arb": "🔷", "avax": "🔺", "hood": "🪶", "hype": "💚",
    }
    mark = marks.get(cid, "⛓")
    liq = float(ln.liquidity_usd or 0)
    cap = int(signer.max_usd())
    href = (CHAINS.get(cid, {}).get("explorer_addr") or "").format(addr=ca) if ca.startswith("0x") or cid == "sol" else ""
    if cid == "sol" and ca:
        href = f"https://solscan.io/token/{ca}"
    title = f"{mark} <a href=\"{html.escape(href)}\"><b>${name}</b></a>" if href else f"{mark} <b>${name}</b>"
    text = (
        f"{title}\n"
        f"<b>{chain}</b>   💧 ${liq:,.0f} liq\n"
        f"<code>{html.escape(ca)}</code>\n"
        f"<i>Blue ticker → explorer · tap CA to copy</i>"
    )
    short = ca if len(ca) <= 48 else ca[:48]
    rows = [
        [
            InlineKeyboardButton("📡 Score", callback_data=f"sig:{short}"),
            InlineKeyboardButton("💵 Buy", callback_data=f"buy:{short}"),
        ],
        [
            InlineKeyboardButton(f"🎯 Snipe ${cap}", callback_data=f"snp:{short}"),
            InlineKeyboardButton("👁 Watch", callback_data=f"watch:{short}"),
        ],
        [
            InlineKeyboardButton("📉 Quote", callback_data=f"qte:{cid}:{short}"),
            InlineKeyboardButton("🧨 Override", callback_data=f"force:{short}"),
        ],
        [
            InlineKeyboardButton("👛 Wallet", callback_data="go:wallets"),
            InlineKeyboardButton("🎒 Bag", callback_data="go:bag"),
        ],
    ]
    if ca and CopyTextButton is not None:
        rows.append(
            [InlineKeyboardButton("📋 Copy CA", copy_text=CopyTextButton(text=ca))]
        )
    return text, InlineKeyboardMarkup(rows)


async def launches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    chain = resolve_chain(context.args[0]) if context.args else None
    launches = sniper.fetch_new_pools(chain, limit=6)
    if not launches:
        await update.effective_message.reply_text(
            "No fresh pools right now. Try /launches sol"
        )
        return
    await update.effective_message.reply_text("🚀 Fresh launches")
    for ln in launches:
        text, markup = launch_card(ln)
        await update.effective_message.reply_text(
            text, parse_mode="HTML", reply_markup=markup
        )


async def treasury_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not _is_operator(update.effective_user.id):
        await update.effective_message.reply_text("Operator only.")
        return
    await fees_cmd(update, context)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    if len(text) > 80:
        return
    await _send_signal(update, text)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await guard(update):
        return
    uid = update.effective_user.id
    data = query.data or ""
    if data.startswith("wi:"):
        kind = data[3:]
        if kind == "gen":
            try:
                user_wallets.ensure(uid)
            except Exception as exc:
                await context.bot.send_message(uid, str(exc))
                return
            await context.bot.send_message(
                uid,
                "✨ Wallet generated. Keys stay on the server — not posted in chat.\n"
                "Tap a chain for the deposit address.",
                reply_markup=wallet_keyboard(update.effective_user.id),
            )
            return
        if kind == "imp":
            await context.bot.send_message(
                uid,
                "📥 Import\n"
                "/importsol <solana-private-key>\n"
                "/importevm <0x-private-key>\n"
                "Only in this private chat. Then delete your message.",
            )
            return
        if kind == "col":
            await context.bot.send_message(
                uid,
                "🧲 Collect — pull funds to one address you own.\n"
                "/collectsol <your-sol-address>\n"
                "/collectevm <your-0x-address>\n"
                "Sends the bag off Ferzan to that address (coming as live send).",
            )
            return
        if kind == "dis":
            await context.bot.send_message(
                uid,
                "📤 Disperse — split from your Ferzan bag to several addresses.\n"
                "Use /collect first until multi-send ships.",
            )
            return
        if kind == "rearr":
            await context.bot.send_message(uid, "📂 One trading pair per user for now.")
            return
        if kind == "exp":
            if update.effective_chat and update.effective_chat.type != "private":
                await context.bot.send_message(uid, "Export only works in a private chat with the bot.")
                return
            try:
                await context.bot.send_message(
                    uid,
                    user_wallets.export_text(uid),
                    parse_mode="Markdown",
                )
            except Exception as exc:
                await context.bot.send_message(uid, str(exc))
            return
        if kind == "chains":
            await context.bot.send_message(
                uid,
                "Select the chain for its deposit address.",
                reply_markup=wallet_keyboard(update.effective_user.id),
            )
            return
        return
    if data.startswith("wa:"):
        cid = resolve_chain(data[3:]) or data[3:]
        try:
            row = user_wallets.ensure(uid)
        except Exception as exc:
            await context.bot.send_message(uid, str(exc))
            return
        meta = CHAINS.get(cid) or {}
        label = "Hood" if cid == "hood" else meta.get("label", cid.upper())
        native = meta.get("native", "ETH")
        marks = {
            "sol": "🟣", "bsc": "🟡", "base": "🔵", "eth": "♦️",
            "arb": "🔷", "avax": "🔺", "hood": "🪶", "hype": "💚",
            "monad": "🟣", "sonic": "🟠", "trx": "🔴", "ton": "💠",
        }
        mark = marks.get(cid, "🔗")
        if cid == "sol":
            addr = row["sol_pub"]
            try:
                lamports = signer.sol_balance_lamports(addr)
                bal_line = f"{lamports / 1_000_000_000:.6f} SOL"
            except Exception:
                bal_line = "—"
        else:
            addr = row["evm_pub"]
            try:
                amt, sym = evm_signer.native_balance(cid, addr)
                bal_line = f"{amt:.6f} {sym}"
            except Exception:
                bal_line = f"— {native}"
        href = (meta.get("explorer_addr") or "{addr}").format(addr=addr)
        text = (
            f"{mark} <a href=\"{_esc(href)}\"><b>{_esc(label)}</b></a>\n"
            f"<code>{_esc(addr)}</code>\n"
            f"🟢 Balance {_esc(bal_line)}\n\n"
            f"<i>Blue name opens the explorer. Tap the address to copy.</i>\n"
            f"Gas in {_esc(native)}. Paste a {_esc(label)} CA to buy."
        )
        await context.bot.send_message(
            uid,
            text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=wallet_keyboard(update.effective_user.id),
        )
        return
    if data.startswith("ch:"):
        cid = resolve_chain(data[3:])
        if not cid:
            await context.bot.send_message(uid, "Unknown chain.")
            return
        m = CHAINS[cid]
        await context.bot.send_message(
            uid,
            f"🟢 {m['label']} selected\n"
            f"Gas {m['native']} · {m['router']}\n\n"
            f"Paste a CA or send:\n"
            f"/signal {cid} <token>\n"
            f"/snipe {cid} <CA> 40\n"
            f"/quote {cid} <CA> 50\n"
            f"/launches {cid}",
        )
        return
    if data.startswith("go:"):
        kind = data[3:]
        if kind.startswith("signal:"):
            await _send_signal(update, kind.split(":", 1)[1], edit=False)
        elif kind in {"help", "home"}:
            await start(update, context)
        elif kind == "pos":
            fake = update
            if update.message is None and update.effective_message:
                pass
            await positions_cmd(update, context)
        elif kind == "launches":
            context.args = ["sol"]
            await launches_cmd(update, context)
        elif kind == "chains":
            await chains_cmd(update, context)
        elif kind == "fees":
            await fees_cmd(update, context)
        elif kind == "wallets":
            await wallet_cmd(update, context)
        elif kind == "bag":
            await bag_cmd(update, context)
        elif kind == "settings":
            context.args = []
            await settings_cmd(update, context)
        elif kind == "snipes":
            await snipes_cmd(update, context)
        elif kind == "copy":
            await context.bot.send_message(
                uid,
                "Copytrade\n"
                "/watchwallet sol <address>\n"
                "/watchwallet eth 0x...\n"
                "/wallets\n"
                "Ferzan pings you when they move. It will not spend your Trust wallet.",
            )
        elif kind == "snipehelp":
            await context.bot.send_message(
                uid,
                "Auto snipe (gated)\n"
                "/snipe sol <CA> 40\n"
                "/snipes   /cancelsnipe 3\n"
                "Fills paper when liq/score/age gates pass. Not first-block.",
            )
        elif kind == "quotehelp":
            await context.bot.send_message(
                uid,
                "Live quote — you sign in Trust\n"
                "/quote sol <CA> 50\n"
                "/quote base 0x... 25\n"
                "0.50% cut is on the quote. No seed in this bot.",
            )
        elif kind == "pnl":
            await context.bot.send_message(
                uid,
                f"Paper cash is on /positions and /drawdown.\nSend /positions",
            )
        elif kind == "buyhelp":
            await context.bot.send_message(
                uid,
                "BUY & SELL\n"
                "Paste a CA in this chat.\n"
                "/buy sol     paper if score clears\n"
                "/sell 3      close position #3\n"
                "/quote sol <CA> 50     sign in Trust",
            )
        return
    if data.startswith("tpx:") or data.startswith("slx:"):
        kind, pct_s, mint = data.split(":", 2)
        try:
            pct = float(pct_s)
        except ValueError:
            pct = 50.0
        if kind == "tpx":
            db.set_live_exit(uid, mint, tp_pct=pct)
            await context.bot.send_message(uid, f"🎯 Live TP +{pct:.0f}% armed on that mint.")
        else:
            db.set_live_exit(uid, mint, sl_pct=pct)
            await context.bot.send_message(uid, f"🛑 Live SL -{pct:.0f}% armed on that mint.")
        return
    if data.startswith("slp:"):
        _tag, pct_s, mint = data.split(":", 2)
        try:
            pct = int(pct_s)
        except ValueError:
            pct = 100
        sol_secret, _evm = user_wallets.secrets(uid)
        _ok, msg = signer.sell_sol(mint, secret=sol_secret, pct=pct)
        await context.bot.send_message(uid, f"{'🟢' if _ok else '🔴'} Sell {pct}%\n{msg}")
        return
    if data.startswith("snp:"):
        ca = data[4:]
        user = db.get_user(uid) or {}
        sid = sniper.arm(
            user_id=uid,
            query=ca,
            chain=None,
            usd=float(signer.max_usd()),
            min_liq=25_000,
            min_score=int(user.get("min_confluence") or 62),
            max_age_h=6.0,
            require_long=True,
        )
        await context.bot.send_message(
            uid, f"🎯 Snipe #{sid} armed · ${signer.max_usd():.0f} · gates on"
        )
        return
    if data.startswith("qte:"):
        parts = data.split(":", 2)
        chain = parts[1] if len(parts) > 2 else "sol"
        token = parts[2] if len(parts) > 2 else parts[-1]
        if quotes is None:
            await context.bot.send_message(uid, "Quote module not loaded.")
            return
        try:
            usd = float(signer.max_usd())
            if chain in {"sol", "solana"} or (token and not token.startswith("0x")):
                q = quotes.sol_quote(token, usd)
            else:
                q = quotes.evm_quote(chain, token, usd)
            await context.bot.send_message(uid, quotes.format_quote(q))
        except Exception as exc:
            await context.bot.send_message(uid, str(exc))
        return
    if data.startswith("sig:"):
        await _send_signal(update, data[4:], edit=True)
        return
    if data.startswith("watch:"):
        name = data[6:]
        db.add_watch(uid, name)
        await query.edit_message_reply_markup(reply_markup=card_keyboard(name, 0))
        await context.bot.send_message(uid, f"Watching {name.upper()}.")
        return
    if data.startswith("buyz:"):
        _tag, usd_s, name = data.split(":", 2)
        try:
            usd_o = float(usd_s)
        except ValueError:
            usd_o = signer.max_usd()
        try:
            card = analyze(name)
        except PriceFetchError as exc:
            await context.bot.send_message(uid, str(exc))
            return
        ok, msg = trading.paper_buy(uid, card, force=True)
        await context.bot.send_message(uid, msg)
        live_msg = _live_buy_followup(uid, card, name, True, True, usd_override=usd_o)
        if live_msg:
            await context.bot.send_message(uid, live_msg)
        return
    if data.startswith("buy:") or data.startswith("force:"):
        force = data.startswith("force:")
        name = data.split(":", 1)[1]
        try:
            card = analyze(name)
        except PriceFetchError as exc:
            await context.bot.send_message(uid, str(exc))
            return
        ok, msg = trading.paper_buy(uid, card, force=force)
        await context.bot.send_message(uid, msg)
        live_msg = _live_buy_followup(uid, card, name, ok, force)
        if live_msg:
            await context.bot.send_message(uid, live_msg)
        return
    if data.startswith("close:"):
        try:
            pos_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        ok, msg = trading.paper_close(uid, pos_id, reason="manual")
        await context.bot.send_message(uid, msg)
        return
    if data.startswith("xsell:"):
        try:
            pos_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        await context.bot.send_message(uid, _live_sell_position(uid, pos_id))


async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    alerts = db.get_all_active_alerts()
    if not alerts:
        return
    coin_ids = [a["coin_id"] for a in alerts]
    try:
        prices = get_prices_usd(coin_ids)
    except PriceFetchError as exc:
        logger.warning("Price poll failed: %s", exc)
        return
    for alert in alerts:
        current = prices.get(alert["coin_id"])
        if current is None:
            continue
        hit = (
            alert["direction"] == "above" and current >= alert["target_price"]
        ) or (
            alert["direction"] == "below" and current <= alert["target_price"]
        )
        if not hit:
            continue
        db.deactivate_alert(alert["id"])
        try:
            await context.bot.send_message(
                chat_id=alert["chat_id"],
                text=(
                    f"Alert #{alert['id']} triggered.\n"
                    f"{alert['symbol']} is ${current:,.6g} "
                    f"({alert['direction']} ${alert['target_price']:,.6g})"
                ),
            )
        except Exception:
            logger.exception("Failed to notify chat %s", alert["chat_id"])


async def scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for notice in trading.mark_open_positions():
        user_id, _pos_id, msg = notice
        try:
            await context.bot.send_message(user_id, "Auto-exit\n" + msg)
        except Exception:
            logger.exception("Exit notice failed for %s", user_id)

    for user_id in db.list_alert_users():
        user = db.get_user(user_id)
        if not user:
            continue
        floor = int(user["min_confluence"])
        for name in db.watchlist_of(user_id):
            try:
                card = analyze(name)
            except PriceFetchError:
                continue
            if card.bias != "LONG" or card.score < floor:
                continue
            if not db.should_resend_signal(user_id, name, card.score):
                continue
            try:
                await context.bot.send_message(
                    user_id,
                    "Scanner\n" + render_card(card),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=card_keyboard(name, card.score),
                )
            except Exception:
                logger.exception("Scanner send failed for %s", user_id)


async def wallet_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for row in db.list_watched_wallets():
        try:
            events = onchain.recent_activity(row["chain"], row["address"], limit=6)
        except Exception as exc:
            logger.info("wallet poll skip #%s: %s", row["id"], exc)
            continue
        if not events:
            continue
        cursor = row.get("cursor")
        fresh = []
        for ev in events:
            if cursor and ev.txid == cursor:
                break
            fresh.append(ev)
        if not fresh:
            continue
        db.set_wallet_cursor(row["id"], events[0].txid)
        lines = [
            f"Wallet #{row['id']} {row['chain']} {row['label'] or row['address'][:8]}",
        ]
        for ev in reversed(fresh[:4]):
            lines.append(f"• {ev.summary}")
            if ev.txid:
                try:
                    lines.append("  " + explorer_tx(row["chain"], ev.txid))
                except Exception:
                    pass
        try:
            await context.bot.send_message(row["user_id"], "\n".join(lines)[:3500])
        except Exception:
            logger.exception("wallet notify failed for %s", row["user_id"])
        blob = "\n".join(ev.summary for ev in fresh)
        mint = ""
        chain = (row.get("chain") or "").lower()
        if "sol" in chain:
            found = re.findall(r"[1-9A-HJ-NP-Za-km-z]{32,44}", blob)
            mint = next((x for x in found if len(x) >= 32 and not x.startswith("0x")), "")
        else:
            found = re.findall(r"0x[a-fA-F0-9]{40}", blob)
            mint = found[0] if found else ""
        if mint:
            try:
                sol_secret, evm_secret = user_wallets.secrets(int(row["user_id"]))
                usd = signer.max_usd()
                if mint.startswith("0x"):
                    _ok, live = evm_signer.buy_evm(chain or "base", mint, usd, key_hex=evm_secret)
                else:
                    _ok, live = signer.buy_sol(mint, usd, secret=sol_secret)
                await context.bot.send_message(row["user_id"], "Copy live\n" + live)
            except Exception as exc:
                logger.info("copy live skip: %s", exc)


async def drawdown_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for user in db.list_users():
        uid = int(user["user_id"])
        try:
            equity, peak, dd = trading.update_peak_and_drawdown(uid)
        except Exception:
            logger.exception("drawdown mark failed for %s", uid)
            continue
        threshold = float(user.get("drawdown_alert_pct") or 12)
        if dd < threshold:
            continue
        if not db.should_resend_signal(uid, "__drawdown__", int(dd), cooldown_s=6 * 3600):
            continue
        try:
            await context.bot.send_message(
                uid,
                f"Drawdown alert: paper book is down {dd:.1f}% from peak "
                f"(${peak:,.2f} → ${equity:,.2f}). "
                f"Daily breaker is separate and still at -{user['max_daily_loss_pct']}%.",
            )
        except Exception:
            logger.exception("drawdown notify failed for %s", uid)


async def live_exit_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for row in db.list_live_exits():
        uid = int(row["user_id"])
        mint = row["mint"]
        cost = db.live_cost(uid, mint)
        if cost <= 0:
            continue
        px = _token_mark_usd(mint)
        if px <= 0:
            continue
        # worth unknown without qty; compare mark vs implied entry from last cost only if we have holdings
        try:
            sol_secret, evm_secret = user_wallets.secrets(uid)
        except Exception:
            continue
        worth = 0.0
        evm_chain = "base"
        try:
            if mint.startswith("0x"):
                evm_addr = (db.get_user_wallet(uid) or {}).get("evm_pub") or ""
                for cid in ("eth", "base", "bsc", "hood", "arb", "avax"):
                    try:
                        raw = evm_signer._erc20_balance(CHAINS[cid]["rpc"], mint, evm_addr)
                    except Exception:
                        raw = 0
                    if raw > 0:
                        worth = (raw / 10**18) * px
                        evm_chain = cid
                        break
            else:
                held = next((h for h in signer.holdings(sol_secret) if h["mint"] == mint), None)
                if held:
                    worth = float(held["amount"]) * px
        except Exception:
            continue
        if worth <= 0:
            continue
        pnl_pct = ((worth - cost) / cost) * 100
        hit = None
        if row.get("tp_pct") and pnl_pct >= float(row["tp_pct"]):
            hit = "tp"
        if row.get("sl_pct") and pnl_pct <= -float(row["sl_pct"]):
            hit = "sl"
        if not hit:
            continue
        try:
            if mint.startswith("0x"):
                _ok, msg = evm_signer.sell_evm(evm_chain, mint, key_hex=evm_secret)
            else:
                _ok, msg = signer.sell_sol(mint, secret=sol_secret, pct=100)
        except Exception as exc:
            msg = str(exc)
            _ok = False
        db.clear_live_exit(uid, mint)
        if _ok:
            db.clear_live_cost(uid, mint)
        try:
            await context.bot.send_message(
                uid,
                f"{'🎯 TP' if hit == 'tp' else '🛑 SL'} hit ({pnl_pct:+.1f}%)\n{msg}",
            )
        except Exception:
            logger.exception("live exit notify failed")


async def snipe_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for user_id, sid, status, msg in sniper.scan_armed():
        if status != "filled":
            continue
        try:
            await context.bot.send_message(user_id, f"Snipe #{sid} filled\n{msg}")
        except Exception:
            logger.exception("snipe notify failed for %s", user_id)


async def launch_feed_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    launches = sniper.fetch_new_pools(None, limit=12)
    if not launches:
        return
    interesting = [ln for ln in launches if ln.liquidity_usd >= 25_000]
    if not interesting:
        return
    for user in db.list_users():
        if not user.get("alerts_on"):
            continue
        uid = int(user["user_id"])
        for ln in interesting[:5]:
            key = f"launch:{ln.chain}:{ln.token[:24]}"
            if not db.should_resend_signal(uid, key, 1, cooldown_s=6 * 3600):
                continue
            text, markup = launch_card(ln)
            try:
                await context.bot.send_message(
                    uid, text, parse_mode="HTML", reply_markup=markup
                )
            except Exception:
                logger.exception("launch feed failed for %s", uid)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env before running.")

    db.init_db()

    async def _post_init(application: Application) -> None:
        try:
            await application.bot.set_my_commands(
                [
                    BotCommand("start", "Home"),
                    BotCommand("signal", "Score a market"),
                    BotCommand("buy", "Paper buy if it clears"),
                    BotCommand("quote", "Live unsigned quote + cut"),
                    BotCommand("positions", "Paper book"),
                    BotCommand("snipe", "Arm a gated snipe"),
                    BotCommand("launches", "New pools"),
                    BotCommand("chains", "Venues"),
                    BotCommand("fees", "Your cut"),
                    BotCommand("signer", "Signer pubkey"),
                    BotCommand("wallet", "Your deposit wallets"),
                    BotCommand("bag", "Live wallet tokens"),
                    BotCommand("livesell", "Sell a live Solana mint"),
                    BotCommand("settings", "Risk vault"),
                ]
            )
        except Exception:
            logger.exception("set_my_commands failed")

    app = Application.builder().token(token).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("alert", alert_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler("positions", positions_cmd))
    app.add_handler(CommandHandler("sell", sell_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("journal", journal_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("resetpaper", resetpaper_cmd))
    app.add_handler(CommandHandler("watchwallet", watchwallet_cmd))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("importsol", importsol_cmd))
    app.add_handler(CommandHandler("importevm", importevm_cmd))
    app.add_handler(CommandHandler("collectsol", collectsol_cmd))
    app.add_handler(CommandHandler("collectevm", collectevm_cmd))
    app.add_handler(CommandHandler("disperse", disperse_cmd))
    app.add_handler(CommandHandler("wallets", wallets_cmd))
    app.add_handler(CommandHandler("unwatchwallet", unwatchwallet_cmd))
    app.add_handler(CommandHandler("drawdown", drawdown_cmd))
    app.add_handler(CommandHandler("fees", fees_cmd))
    app.add_handler(CommandHandler("signer", signer_cmd))
    app.add_handler(CommandHandler("bag", bag_cmd))
    app.add_handler(CommandHandler("tp", tp_cmd))
    app.add_handler(CommandHandler("sl", sl_cmd))
    app.add_handler(CommandHandler("livesell", livesell_cmd))
    app.add_handler(CommandHandler("livesellevm", livesellevm_cmd))
    app.add_handler(CommandHandler("treasury", treasury_cmd))
    app.add_handler(CommandHandler("snipe", snipe_cmd))
    app.add_handler(CommandHandler("snipes", snipes_cmd))
    app.add_handler(CommandHandler("cancelsnipe", cancelsnipe_cmd))
    app.add_handler(CommandHandler("launches", launches_cmd))
    app.add_handler(CommandHandler("chains", chains_cmd))
    app.add_handler(CommandHandler("quote", quote_cmd))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("live", quote_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    jq = app.job_queue
    if jq is not None:
        jq.run_repeating(check_alerts_job, interval=ALERT_INTERVAL_SECONDS, first=12)
        jq.run_repeating(scan_job, interval=SCAN_INTERVAL_SECONDS, first=25)
        jq.run_repeating(wallet_job, interval=WALLET_POLL_SECONDS, first=40)
        jq.run_repeating(drawdown_job, interval=DRAWDOWN_POLL_SECONDS, first=55)
        jq.run_repeating(snipe_job, interval=SNIPE_POLL_SECONDS, first=18)
        jq.run_repeating(live_exit_job, interval=45, first=50)
        jq.run_repeating(launch_feed_job, interval=LAUNCH_FEED_SECONDS, first=35)
    else:
        logger.warning("job-queue extra missing; commands still work, scanners off")

    logger.info("FERZAN starting")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
