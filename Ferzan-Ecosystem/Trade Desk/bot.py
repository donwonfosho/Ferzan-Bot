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

import asyncio
import html
import logging
import os
import re
import time

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
BANNER_PATH = Path(__file__).parent / "trade-desk.jpg"

ADMIN_IDS = {
    int(x) for x in re.split(r"[,\s]+", (os.getenv("FERZAN_ADMIN_IDS") or "").strip()) if x.strip().isdigit()
}


def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS
PROMO_PATH = Path(os.getenv("FERZAN_PROMO_GIF", str(Path(__file__).parent / "promo.gif")))

ALERT_INTERVAL_SECONDS = int(os.getenv("ALERT_INTERVAL_SECONDS", "60"))
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "90"))
WALLET_POLL_SECONDS = int(os.getenv("WALLET_POLL_SECONDS", "75"))
DRAWDOWN_POLL_SECONDS = int(os.getenv("DRAWDOWN_POLL_SECONDS", "120"))
SNIPE_POLL_SECONDS = int(os.getenv("SNIPE_POLL_SECONDS", "25"))
LAUNCH_FEED_SECONDS = int(os.getenv("LAUNCH_FEED_SECONDS", "45"))


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


def _pair_age(iso: str) -> str:
    raw = (iso or "").strip()
    if not raw:
        return ""
    try:
        from datetime import datetime, timezone

        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        mins = max(0, int((datetime.now(timezone.utc) - ts).total_seconds() // 60))
        if mins < 60:
            return f"{mins}m old"
        if mins < 1440:
            return f"{mins // 60}h old"
        return f"{mins // 1440}d old"
    except Exception:
        return raw[:16]


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


def _age_ms(ms: int | None) -> str:
    if not ms:
        return ""
    sec = max(0, int(time.time() - float(ms) / 1000.0))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60}s"
    if sec < 86400:
        return f"{sec // 3600}h {(sec % 3600) // 60}m"
    return f"{sec // 86400}d"


def _pump_meta(ca: str) -> dict:
    out = {"curve": "", "liq": 0.0, "mc": 0.0, "tg": "", "tw": "", "web": ""}
    if not ca or not str(ca).lower().endswith("pump"):
        return out
    try:
        r = requests.get(f"https://frontend-api-v3.pump.fun/coins/{ca}", timeout=6)
        data = r.json() if r.content else {}
        if not isinstance(data, dict):
            return out
        out["mc"] = float(data.get("usd_market_cap") or data.get("market_cap") or 0)
        raw = float(data.get("real_sol_reserves") or data.get("virtual_sol_reserves") or 0)
        sol = raw / (1e9 if raw > 1000 else 1)
        # curve vault ≈ SOL in the curve; USD ~ SOL * 2 * px is what desks quote as liq
        px = 0.0
        if out["mc"] and sol:
            # implied SOL USD from cap / tokens is noisy; use reserve * 200 fallback
            px = 180.0
        out["liq"] = sol * px * 2 if sol else 0.0
        if data.get("complete"):
            out["curve"] = "📈 Bonding curve 100% · graduated"
        elif sol:
            out["curve"] = f"📈 Bonding curve {min(100.0, sol / 85.0 * 100.0):.0f}%"
        out["tg"] = str(data.get("telegram") or "")
        out["tw"] = str(data.get("twitter") or "")
        web = str(data.get("website") or "")
        if web and ("x.com" in web.lower() or "twitter.com" in web.lower()):
            out["tw"] = out["tw"] or web
        else:
            out["web"] = web
    except Exception:
        pass
    return out


def _pump_curve(ca: str) -> str:
    return _pump_meta(ca).get("curve") or ""


def _erc20_amt(rpc: str, token: str, owner: str) -> float:
    try:
        raw = evm_signer._erc20_balance(rpc, token, owner)
        dec_body = evm_signer._rpc(rpc, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
        dec_hex = dec_body.get("result") or "0x12"
        decimals = int(dec_hex, 16) if str(dec_hex).startswith("0x") else int(dec_hex)
        decimals = max(0, min(36, decimals or 18))
        return raw / (10 ** decimals)
    except Exception:
        return 0.0


def _card_wallet(uid: int | None, ca: str, chain: str) -> str:
    if not uid:
        return "<blockquote>💰 <b>Balance</b>\nFund /wallet</blockquote>"
    cid = resolve_chain(chain)
    if not cid and ca:
        try:
            meta = _token_meta(ca)
            cid = resolve_chain(meta.get("chain") or "")
        except Exception:
            cid = None
    cid = cid or ("sol" if ca and not str(ca).startswith("0x") else "eth")
    label = "Hood" if cid == "hood" else (CHAINS.get(cid) or {}).get("label") or cid.upper()
    native_sym = (CHAINS.get(cid) or {}).get("native") or "?"
    tok = 0.0
    native = 0.0
    ticker = ""
    try:
        ticker = ((_token_meta(ca).get("symbol") or "") if ca else "").upper()
    except Exception:
        ticker = ""
    try:
        sol_secret, evm_secret = user_wallets.secrets(uid)
        if cid == "sol":
            kp = signer.keypair_from_secret(sol_secret)
            native = signer.sol_balance_lamports(str(kp.pubkey())) / 1e9
            native_sym = "SOL"
            if ca:
                for row in signer.holdings(sol_secret):
                    if row.get("mint") == ca:
                        tok = float(row.get("amount") or 0)
                        break
        else:
            from eth_account import Account
            addr = Account.from_key(evm_secret).address
            native, native_sym = evm_signer.native_balance(cid, addr)
            rpc = (CHAINS.get(cid) or {}).get("rpc") or ""
            if ca and rpc and str(ca).startswith("0x"):
                tok = _erc20_amt(rpc, ca, addr)
    except Exception:
        return (
            "<blockquote>"
            f"💰 <b>{html.escape(label)}</b>\nFund /wallet"
            "</blockquote>"
        )
    bag = ticker or "token"
    return (
        "<blockquote>"
        f"💰 <b>{html.escape(label)}</b>  {native:.4f} {html.escape(native_sym)}\n"
        f"🪙 {html.escape(bag)} {tok:.4g}"
        "</blockquote>"
    )


def render_card(card: SignalCard, uid: int | None = None) -> str:
    s = card.snapshot
    ca = (s.token_address or "").strip()
    chain = (s.chain or "").upper()
    dex = (s.dex or "").replace("pumpswap", "Pump.fun").replace("pumpfun", "Pump.fun")
    mc = float(s.fdv or 0)
    liq = float(s.liquidity_usd or 0)
    pump_meta = _pump_meta(ca) if ca else {}
    if (not liq) and pump_meta.get("liq"):
        liq = float(pump_meta["liq"])
    if (not mc) and pump_meta.get("mc"):
        mc = float(pump_meta["mc"])
    vol = float(s.volume_24h or 0)
    liq_pct = f" ({liq / mc * 100:.1f}%)" if mc > 0 and liq > 0 else ""
    scan = (CHAINS.get(resolve_chain(s.chain) or "", {}).get("explorer_addr") or "").format(addr=ca) if ca else ""
    if (s.chain or "").lower() in {"solana", "sol"} and ca:
        scan = f"https://solscan.io/token/{ca}"
    ds = s.url or (f"https://dexscreener.com/{s.chain}/{ca}" if ca else "")
    age = _age_ms(getattr(s, "pair_created_ms", None))
    info = ((s.extras or {}).get("info") or {}) if hasattr(s, "extras") else {}
    tw = ""
    if isinstance(info, dict):
        tw = (info.get("twitter") or "") if isinstance(info.get("twitter"), str) else ""
        socials = info.get("socials") or []
        if not tw and isinstance(socials, list):
            for row in socials:
                if isinstance(row, dict) and "twitter" in str(row.get("type") or "").lower():
                    tw = row.get("url") or ""
    pump = "pump" in (ca or "").lower() or "pump" in (dex or "").lower()
    tw = tw or (pump_meta.get("tw") if isinstance(pump_meta, dict) else "") or ""
    tg = (pump_meta.get("tg") if isinstance(pump_meta, dict) else "") or ""
    if isinstance(info, dict):
        for row in info.get("socials") or []:
            if not isinstance(row, dict):
                continue
            url = row.get("url") or ""
            typ = str(row.get("type") or "").lower()
            if "telegram" in typ and url:
                tg = tg or url
    venue = "🧪 Pump.fun" if pump else f"📡 {_esc(dex or chain)}"
    social = []
    if tg:
        social.append(f'➡️ <a href="{html.escape(str(tg), quote=True)}">Telegram</a>')
    social.append("👤 DEV")
    if tw:
        social.append(f'<a href="{html.escape(str(tw), quote=True)}">X</a>')
    curve = (pump_meta.get("curve") if isinstance(pump_meta, dict) else "") or _pump_curve(ca)
    cid = resolve_chain(s.chain) or ("sol" if ca and not str(ca).startswith("0x") else "eth")
    ds_net = {"sol": "solana", "eth": "ethereum", "bsc": "bsc", "base": "base", "arb": "arbitrum"}.get(cid, "solana")
    dt_net = {"sol": "solana", "eth": "ether", "bsc": "bnb", "base": "base", "arb": "arbitrum"}.get(cid)
    ds = ds or (f"https://dexscreener.com/{ds_net}/{ca}" if ca else "")
    dt = f"https://www.dextools.io/app/en/{dt_net}/pair-explorer/{ca}" if ca and dt_net else ""
    pump_url = f"https://pump.fun/coin/{ca}" if pump and ca else ""
    links = []
    if pump_url:
        links.append(f'🏆 <a href="{html.escape(pump_url, quote=True)}">Pump</a>')
    if ds:
        links.append(f'<a href="{html.escape(ds, quote=True)}">DexScreener</a>')
    if dt:
        links.append(f'<a href="{html.escape(dt, quote=True)}">DexTools</a>')
    if scan:
        links.append(f'<a href="{html.escape(scan, quote=True)}">Scan</a>')
    lines = [
        f"⚡ <b>{_esc(s.name)}</b>  ${_esc(str(s.symbol).lstrip('$'))}  ·  {_esc(chain)}",
        f"<code>{_esc(ca)}</code>" if ca else "",
        " · ".join([x for x in [venue, (f"age {html.escape(age)}" if age else ""), curve] if x]),
        " · ".join(social) if social else "",
        (
            f"🧢 {_esc(f'${mc:,.0f}' if mc else '—')}"
            f"  💵 {_esc(_fmt_px(s.price_usd))}"
            f"  💧 {_esc(f'${liq:,.0f}' if liq else '—')}{_esc(liq_pct)}"
        ),
        _card_wallet(uid, ca, s.chain or ""),
        f"📊 1h {s.buys_h1}/{s.sells_h1}  ·  24h {_esc(f'${vol:,.0f}' if vol else '—')}  ·  {card.score}/100 {_esc(card.bias)}",
        " · ".join(links),
    ]
    return "\n".join(lines)


def card_keyboard(
    query: str, score: int, ca: str = "", chain: str = "", uid: int | None = None
) -> InlineKeyboardMarkup:
    q = (ca or query)[:44]
    cid = resolve_chain(chain) or ("sol" if q and not str(q).startswith("0x") else "eth")
    unit = {
        "sol": "SOL", "bsc": "BNB", "eth": "ETH", "base": "ETH",
        "arb": "ETH", "avax": "AVAX", "pol": "POL", "hood": "ETH",
    }.get(cid, "ETH")
    rows = [
        [InlineKeyboardButton("🎯 Snipe now", callback_data=f"snp:{q}")],
        [
            InlineKeyboardButton("📍 Track", callback_data=f"watch:{q}"),
            InlineKeyboardButton(f"🔄 {unit}", callback_data=f"sig:{q}"),
        ],
        [InlineKeyboardButton("↔️ Go to sell", callback_data=f"slc:{q}")],
        [
            InlineKeyboardButton("💳 Multi buy | 1", callback_data="go:wallets"),
            InlineKeyboardButton("🟢 Multi", callback_data="go:wallets"),
        ],
        [InlineKeyboardButton(f"🟢 {unit}", callback_data=f"sig:{q}")],
        [
            InlineKeyboardButton(f"0.01 {unit}", callback_data=f"bnv:0.01:{q}"),
            InlineKeyboardButton(f"0.05 {unit}", callback_data=f"bnv:0.05:{q}"),
            InlineKeyboardButton(f"0.1 {unit}", callback_data=f"bnv:0.1:{q}"),
        ],
        [
            InlineKeyboardButton(f"0.2 {unit}", callback_data=f"bnv:0.2:{q}"),
            InlineKeyboardButton(f"0.5 {unit}", callback_data=f"bnv:0.5:{q}"),
            InlineKeyboardButton(f"1 {unit}", callback_data=f"bnv:1:{q}"),
        ],
        [
            InlineKeyboardButton("$5", callback_data=f"buyz:5:{q}"),
            InlineKeyboardButton("$25", callback_data=f"buyz:25:{q}"),
            InlineKeyboardButton("$50", callback_data=f"buyz:50:{q}"),
        ],
        [
            InlineKeyboardButton(f"✏️ Buy X {unit}", callback_data=f"buyx:{q}"),
            InlineKeyboardButton("✏️ Buy X tokens", callback_data=f"buyx:{q}"),
        ],
        [
            InlineKeyboardButton(
                f"🎚 Slip {int((db.get_chain_trade(uid, cid)['buy_slip'] if uid else 10))}%"
                if uid
                else "🎚 Slippage",
                callback_data=f"xslip:{cid}",
            ),
            InlineKeyboardButton(
                f"⛽ Gas {float((db.get_chain_trade(uid, cid).get('gas') if uid else 0) or 0):.3f} {unit}"
                if uid
                else "⛽ Gas",
                callback_data=f"xgas:{cid}",
            ),
        ],
        [
            InlineKeyboardButton("🎯 Snipe", callback_data=f"snp:{q}"),
            InlineKeyboardButton("⏳ Buy limit", callback_data=f"blm:{q}"),
        ],
    ]
    addr = (ca or query or "").strip()
    ds_net = {
        "sol": "solana", "eth": "ethereum", "bsc": "bsc", "base": "base",
        "arb": "arbitrum", "avax": "avalanche", "pol": "polygon",
    }.get(cid, "solana")
    dt_net = {
        "sol": "solana", "eth": "ether", "bsc": "bnb", "base": "base",
        "arb": "arbitrum", "avax": "avalanche", "pol": "polygon",
    }.get(cid)
    links = []
    if addr:
        if "pump" in addr.lower() or cid == "sol":
            if "pump" in addr.lower():
                links.append(InlineKeyboardButton("🧪 Pump", url=f"https://pump.fun/coin/{addr}"))
        links.append(InlineKeyboardButton("📈 DS", url=f"https://dexscreener.com/{ds_net}/{addr}"))
        if dt_net:
            links.append(InlineKeyboardButton("🛠 DexTools", url=f"https://www.dextools.io/app/en/{dt_net}/pair-explorer/{addr}"))
    if links:
        rows.append(links[:3])
    scan_url = (CHAINS.get(cid, {}).get("explorer_addr") or "").format(addr=addr) if addr else ""
    if cid == "sol" and addr:
        scan_url = f"https://solscan.io/token/{addr}"
    extra = []
    if scan_url:
        extra.append(InlineKeyboardButton("🔎 Scan", url=scan_url))
    if addr and CopyTextButton is not None:
        extra.append(InlineKeyboardButton("📋 Copy CA", copy_text=CopyTextButton(text=addr)))
    elif addr:
        extra.append(InlineKeyboardButton("📋 CA", callback_data=f"sig:{addr[:44]}"))
    if extra:
        rows.append(extra)
    return InlineKeyboardMarkup(rows)


def sell_keyboard(
    query: str, ca: str = "", chain: str = "", uid: int | None = None, token_amt: float = 0.0
) -> InlineKeyboardMarkup:
    q = (ca or query)[:44]
    cid = resolve_chain(chain) or ("sol" if q and not str(q).startswith("0x") else "eth")
    unit = {
        "sol": "SOL", "bsc": "BNB", "eth": "ETH", "base": "ETH",
        "arb": "ETH", "avax": "AVAX", "pol": "POL", "hood": "ETH",
    }.get(cid, "ETH")
    rows = [
        [
            InlineKeyboardButton("📍 Track", callback_data=f"watch:{q}"),
            InlineKeyboardButton(f"🔄 {unit}", callback_data=f"sig:{q}"),
        ],
        [InlineKeyboardButton("↔️ Go to buy", callback_data=f"sig:{q}")],
        [
            InlineKeyboardButton("💳 Multi sell | 1", callback_data="go:wallets"),
            InlineKeyboardButton("🟢 Multi", callback_data="go:wallets"),
        ],
    ]
    if token_amt <= 0:
        rows.append([InlineKeyboardButton("⚠️ No balance detected ⚠️", callback_data=f"slc:{q}")])
    else:
        rows.append(
            [
                InlineKeyboardButton("Sell 25%", callback_data=f"slp:25:{q}"),
                InlineKeyboardButton("Sell 50%", callback_data=f"slp:50:{q}"),
                InlineKeyboardButton("Sell 100%", callback_data=f"slp:100:{q}"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                f"🎚 Slip {int((db.get_chain_trade(uid, cid)['sell_slip'] if uid else 10))}%"
                if uid
                else "🎚 Slippage",
                callback_data=f"xslip:{cid}",
            ),
            InlineKeyboardButton(
                f"⛽ Gas {float((db.get_chain_trade(uid, cid).get('gas') if uid else 0) or 0):.3f} {unit}"
                if uid
                else "⛽ Gas",
                callback_data=f"xgas:{cid}",
            ),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("🎯 Snipe", callback_data=f"snp:{q}"),
            InlineKeyboardButton("⏳ Sell limit", callback_data=f"blm:{q}"),
        ]
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
    chat = (os.getenv("FERZAN_CHAT_URL") or "https://t.me/Ferzan_Chat").strip()
    xurl = (os.getenv("FERZAN_X_URL") or "https://x.com/ferzaneco").strip()
    rows = [
        [
            InlineKeyboardButton("⛓ Chains", callback_data="go:chains"),
            InlineKeyboardButton("👛 Wallets", callback_data="go:wallets"),
            InlineKeyboardButton("⚙️ Desk", callback_data="go:settings"),
        ],
        [
            InlineKeyboardButton("📊 Bag", callback_data="go:bag"),
            InlineKeyboardButton("📡 Signals", callback_data="go:feeds"),
            InlineKeyboardButton("🎯 Snipe", callback_data="go:snipehelp"),
        ],
        [
            InlineKeyboardButton("⏱ Limits", callback_data="go:snipes"),
            InlineKeyboardButton("👯 Copy", callback_data="go:copy"),
            InlineKeyboardButton("🌉 Bridge", callback_data="go:bridge"),
        ],
        [
            InlineKeyboardButton("🚀 Launch", callback_data="go:launches"),
            InlineKeyboardButton("💸 Cut", callback_data="go:fees"),
            InlineKeyboardButton("💬 Chat", url=chat),
        ],
        [InlineKeyboardButton("⚡ PASTE CA", callback_data="go:buyhelp")],
        [InlineKeyboardButton("𝕏 @FerzanEco", url=xurl)],
    ]
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    extra = (context.args or [None])[0]
    if extra and extra.startswith("ref"):
        try:
            rid = int(str(extra).replace("ref_", "").replace("ref", ""))
            me = update.effective_user.id
            if rid and rid != me:
                db.ensure_user(me, update.effective_user.username)
                db.update_user(me, referred_by=rid, discount_until=int(time.time()) + 30 * 86400)
        except (TypeError, ValueError):
            pass
    if extra and extra.startswith("sig_"):
        await _send_signal(update, extra[4:], edit=False)
        return
    if extra and extra.startswith("buy_"):
        await _send_signal(update, extra[4:], edit=False)
        if update.effective_message:
            await update.effective_message.reply_text(
                "Buy desk. Tap 0.01 / 0.05 / $ on the card. Spends YOUR Ferzan wallet."
            )
        return
    try:
        user = db.ensure_user(update.effective_user.id, update.effective_user.username)
        ready, _fee_note = fees.live_ready()
        buy_usd = float(user.get("buy_usd") or 25)
        bslip = float(user.get("buy_slip_pct") or 10)
        text = (
            "⚡ Welcome to Ferzan — the one-stop desk.\n"
            "👀 See it.  🦍 Ape it.  🚀 Send it.\n\n"
            "⛓ Chains: enable the venues you trade.\n"
            "👛 Wallets: your Ferzan desk addresses.\n"
            "⚙️ Desk: slip, size, gas.\n"
            "📊 Bag: open bags and sell %.\n"
            "📡 Signals: chain rooms.\n"
            "🎯 Snipe: arm a first-block buy.\n"
            "⏱ Limits: buy / sell limits.\n"
            "👯 Copy: watch a wallet.\n"
            "🌉 Bridge: SOL · ETH · BASE · BSC inside Ferzan.\n\n"
            "⚡ Paste a token CA to trade now.\n"
            "Chain follows the CA. Session and wallet stay put.\n\n"
            f'<a href="{html.escape(os.getenv("FERZAN_HUB_URL") or "https://t.me/Ferzan_Trade_Ecosystem", quote=True)}">Hub</a> · '
            f'<a href="{html.escape(os.getenv("FERZAN_CHAT_URL") or "https://t.me/Ferzan_Chat", quote=True)}">Chat</a> · '
            f'<a href="{html.escape(os.getenv("FERZAN_X_URL") or "https://x.com/ferzaneco", quote=True)}">X</a>'
        )
        target = update.effective_message
        if not target:
            return
        banner = BANNER_PATH if BANNER_PATH.exists() else LOGO_PATH
        if banner.exists():
            with banner.open("rb") as photo:
                await target.reply_photo(
                    photo=photo,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=home_keyboard(),
                )
        else:
            await target.reply_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=home_keyboard(),
            )
        try:
            user_wallets.ensure(update.effective_user.id)
        except Exception:
            logger.exception("wallet ensure on start failed")
    except Exception:
        logger.exception("start failed")
        try:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "FERZAN is up. Desk hit a snag. Try /wallet — do not Redeploy yet."
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
        "/buylimit <CA> <price> 3 — buy when mark hits\n"
        "/limits — list buy limits\n"
        "/settings — buy size, floor, protection\n"
        "/feeds — on/off launch alerts per chain\n"
        "/positions — live bag\n"
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
        "Pump.fun: paste the mint from DexScreener (usually ends in pump).\n"
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
    uid = update.effective_user.id if update.effective_user else None
    text = render_card(card, uid)
    markup = card_keyboard(
        card.snapshot.query or query,
        card.score,
        ca=card.snapshot.token_address or "",
        chain=card.snapshot.chain or "",
        uid=uid,
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


def _rug_block(uid: int, card, mint: str) -> str:
    s = card.snapshot
    if db.flag_on(uid, "rug_buy", 1):
        liq = float(s.liquidity_usd or 0)
        dex = str(s.dex or "").lower()
        pump = "pump" in dex or "pump" in str(mint).lower()
        if liq <= 0 and not pump:
            return "🛡 Rug guard ON: no DEX liquidity. Live buy blocked."
        if liq and liq < 15_000 and not pump:
            return "🛡 Rug guard ON: liquidity under $15k. Live buy blocked."
    if db.flag_on(uid, "honeypot", 1) and mint.startswith("0x"):
        sec = _security_line(s.chain, mint).lower()
        if "honeypot" in sec:
            return "🛡 Honeypot guard ON: live buy blocked."
        if "cannot sell all" in sec or "owner can change" in sec:
            return "🛡 Honeypot guard ON: sell looks trapped. Live buy blocked."
    return ""


def _token_liq_usd(mint: str) -> float:
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=8,
        )
        pairs = (r.json() or {}).get("pairs") or []
        if not pairs:
            return 0.0
        return max(float((p.get("liquidity") or {}).get("usd") or 0) for p in pairs)
    except Exception:
        return -1.0


def _slip_bps(uid: int, side: str = "buy", chain: str | None = None) -> int:
    pct = None
    if chain:
        row = db.get_chain_trade(uid, resolve_chain(chain) or chain)
        pct = row.get("buy_slip" if side == "buy" else "sell_slip")
    if pct is None:
        user = db.get_user(uid) or {}
        raw = user.get("buy_slip_pct" if side == "buy" else "sell_slip_pct") or 10
        try:
            pct = float(raw)
        except (TypeError, ValueError):
            pct = 10.0
    return int(max(10, min(9900, float(pct) * 100)))


def _default_buy_usd(uid: int) -> float:
    user = db.get_user(uid) or {}
    try:
        usd = float(user.get("buy_usd") or 25)
    except (TypeError, ValueError):
        usd = 25.0
    return min(signer.max_usd(), max(1.0, usd))


def _live_buy_followup(
    uid: int, card, query: str, paper_ok: bool, force: bool, usd_override: float | None = None
) -> str:
    if not signer.live_enabled():
        return "Live buys are off. LIVE_BUYS=0 on the server."
    if db.flag_on(uid, "score_gate", 0) and not force:
        floor = int((db.get_user(uid) or {}).get("min_confluence") or 0)
        if getattr(card, "score", 100) < floor:
            return f"Blocked by your score floor ({card.score} < {floor}). /settings floor or tap Override."
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
    blocked = _rug_block(uid, card, mint)
    if blocked:
        return blocked
    usd = _default_buy_usd(uid)
    if usd_override is not None:
        usd = min(signer.max_usd(), max(1.0, float(usd_override)))
    try:
        sol_secret, evm_secret = user_wallets.secrets(uid)
    except Exception as exc:
        return f"Live: open /wallet first.\n{exc}"
    if chain in {"trx", "tron"} or (mint.startswith("T") and 30 <= len(mint) <= 36):
        import tron_signer

        _ok, msg = tron_signer.buy_tron(mint, usd, key_hex=evm_secret)
        if _ok:
            db.add_live_cost(uid, mint, usd)
            extra = db.credit_desk_share(uid, usd)
            if extra:
                msg = f"{msg}\n{extra}"
        return msg
    if chain in {"ton"} or mint.startswith(("EQ", "UQ", "kQ")):
        import ton_signer

        _ok, msg = ton_signer.buy_ton(mint, usd, secret=sol_secret)
        if _ok:
            db.add_live_cost(uid, mint, usd)
            extra = db.credit_desk_share(uid, usd)
            if extra:
                msg = f"{msg}\n{extra}"
        return msg
    if mint.startswith("0x"):
        if not (os.getenv("ZEROX_API_KEY") or "").strip():
            return "Live: EVM needs ZEROX_API_KEY on the droplet."
        _ok, msg = evm_signer.buy_evm(
            chain or "base", mint, usd, key_hex=evm_secret, slip_bps=_slip_bps(uid, "buy"), user_id=uid
        )
        if _ok:
            db.add_live_cost(uid, mint, usd)
            db.set_lp_mark(uid, mint, float(card.snapshot.liquidity_usd or 0))
            extra = db.credit_desk_share(uid, usd)
            if extra:
                msg = f"{msg}\n{extra}"
        return msg
    if "sol" not in chain and not (len(mint) >= 32 and not mint.startswith("0x")):
        return f"Live: {chain or 'unknown'} is not Solana."
    _ok, msg = signer.buy_sol(mint, usd, secret=sol_secret, slip_bps=_slip_bps(uid, "buy"))
    if _ok:
        db.add_live_cost(uid, mint, usd)
        db.set_lp_mark(uid, mint, float(card.snapshot.liquidity_usd or 0))
        extra = db.credit_desk_share(uid, usd)
        if extra:
            msg = f"{msg}\n{extra}"
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
    _ok, msg = signer.sell_sol(mint, slip_bps=_slip_bps(uid, "sell"))
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
    live_msg = _live_buy_followup(update.effective_user.id, card, query, True, False)
    await update.effective_message.reply_text(live_msg or "Buy sent.")


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await bag_cmd(update, context)
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


def _token_meta(mint: str) -> dict:
    out = {"px": 0.0, "symbol": "", "name": "", "chain": ""}
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{mint}",
            timeout=8,
        )
        pairs = (r.json() or {}).get("pairs") or []
        if not pairs:
            return out
        p = pairs[0]
        base = p.get("baseToken") or {}
        out["px"] = float(p.get("priceUsd") or 0)
        out["symbol"] = str(base.get("symbol") or "").strip()
        out["name"] = str(base.get("name") or "").strip()
        out["chain"] = str(p.get("chainId") or "").strip()
    except Exception:
        return out
    return out


def _token_mark_usd(mint: str) -> float:
    return float(_token_meta(mint).get("px") or 0)


def _bag_panel(mint: str, amount: float, addr: str, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    short = mint[:44]
    meta = _token_meta(mint)
    px = float(meta.get("px") or 0)
    symbol = (meta.get("symbol") or "").upper()
    name = meta.get("name") or ""
    chain = (meta.get("chain") or "").lower()
    if mint.startswith("0x"):
        if chain in ("base",):
            href = f"https://basescan.org/token/{mint}"
            venue = "BASE"
        elif chain in ("bsc", "bnb"):
            href = f"https://bscscan.com/token/{mint}"
            venue = "BNB"
        elif chain in ("arbitrum", "arb"):
            href = f"https://arbiscan.io/token/{mint}"
            venue = "ARB"
        else:
            href = f"https://etherscan.io/token/{mint}"
            venue = chain.upper() or "EVM"
    else:
        href = f"https://solscan.io/token/{mint}"
        venue = "SOL"
    title = symbol or name or "TOKEN"
    if name and symbol and name.upper() != symbol:
        title = f"{html.escape(name)} (${html.escape(symbol)})"
    else:
        title = html.escape(title)
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
        f"🎒 <b>Position</b> · {html.escape(venue)}\n"
        f"<b>{title}</b>\n"
        f"<a href=\"{href}\">Chart / scan</a>\n"
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


async def tpladder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    args = list(context.args or [])
    if not args:
        await update.effective_message.reply_text(
            "Usage: /tpladder 50:25 100:25 200:50 [mint]\n"
            "Each rung is <gain%>:<sell% of remaining bag>. Sells that % of your "
            "*current* holding when the gain is hit, then keeps watching the rest.\n"
            "/tpladder off <mint>  clears the ladder for that token.\n"
            "This stacks with /sl and /trail — they still close whatever's left.",
        )
        return
    mint = ""
    if args and (args[-1].startswith("0x") or len(args[-1]) >= 32) and ":" not in args[-1]:
        mint = args.pop()
    if not mint:
        found = db.live_mints(update.effective_user.id)
        mint = found[0] if found else ""
    if not mint:
        await update.effective_message.reply_text("Buy live first, or pass a mint.")
        return
    if args and args[0].lower() == "off":
        db.clear_tp_ladder(update.effective_user.id, mint)
        await update.effective_message.reply_text("🎯 TP ladder cleared for that token.")
        return
    rungs: list[tuple[float, float]] = []
    for tok in args:
        if ":" not in tok:
            await update.effective_message.reply_text(f"Bad rung '{tok}'. Use gain:sell, e.g. 50:25")
            return
        gain_s, sell_s = tok.split(":", 1)
        try:
            gain, sell = float(gain_s), float(sell_s)
        except ValueError:
            await update.effective_message.reply_text(f"Bad rung '{tok}'. Use gain:sell, e.g. 50:25")
            return
        if gain <= 0 or not (0 < sell <= 100):
            await update.effective_message.reply_text("Gain must be > 0, sell must be 1-100.")
            return
        rungs.append((gain, sell))
    rungs.sort(key=lambda r: r[0])
    total_sell = sum(r[1] for r in rungs)
    if total_sell > 100.0001:
        await update.effective_message.reply_text(
            f"Rungs sell {total_sell:.0f}% of the bag total (as it shrinks after each rung, "
            "that's fine as long as no single rung is > 100%) — armed anyway."
        )
    db.set_tp_ladder(update.effective_user.id, mint, rungs)
    # Ladder rungs are only checked by the exit job for mints it's already
    # tracking (i.e. rows in live_exits) -- make sure this one is, even if
    # the user never ran /tp, /sl, or /trail on it.
    if not db.get_live_exit(update.effective_user.id, mint):
        db.set_live_exit(update.effective_user.id, mint)
    lines = "\n".join(f"  +{g:.0f}% → sell {s:.0f}%" for g, s in rungs)
    await update.effective_message.reply_text(f"🎯 TP ladder armed:\n{lines}")


async def trail_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /trail 20   or /trail 20 <mint>")
        return
    try:
        pct = float(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Use a number. /trail 20")
        return
    mint = context.args[1] if len(context.args) > 1 else ""
    if not mint:
        found = db.live_mints(update.effective_user.id)
        mint = found[0] if found else ""
    if not mint:
        await update.effective_message.reply_text("Buy live first, or pass a mint.")
        return
    db.set_live_exit(update.effective_user.id, mint, trail_pct=pct)
    await update.effective_message.reply_text(
        f"📉 Trailing stop {pct:.0f}% under peak PnL. It only ratchets up."
    )


async def stake_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    user = db.get_user(uid) or {}
    held = float(user.get("stake_units") or 0)
    mint = (os.getenv("FERZAN_TOKEN_MINT") or "").strip()
    if not context.args:
        await update.effective_message.reply_text(
            f"Stake ledger: {held:,.2f} units\n"
            f"Token mint: `{mint or 'not set — FERZAN_TOKEN_MINT'}`\n\n"
            "This is a rebate ledger until the Ferzan token is live.\n"
            "/stake 1000  records units for a fee cut.\n"
            "100 → −5 bps · 1,000 → −10 · 10,000 → −15 (floor 0.10%).",
            parse_mode="Markdown",
        )
        return
    try:
        units = float(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Usage: /stake 1000")
        return
    db.set_stake_units(uid, units)
    await update.effective_message.reply_text(
        f"Stake set to {units:,.2f}. Your next quotes use the rebate tier."
    )


async def lpguard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    user = db.get_user(uid) or {}
    if len(context.args or []) >= 2:
        try:
            drop = float(context.args[0])
            floor = float(context.args[1])
        except ValueError:
            await update.effective_message.reply_text("Usage: /lpguard 50 500")
            return
        db.update_user(uid, lp_drop_pct=drop, lp_floor_usd=floor)
        user = db.get_user(uid) or {}
    await update.effective_message.reply_text(
        f"LP yank: sell if liquidity falls {float(user.get('lp_drop_pct') or 50):.0f}% "
        f"and marked LP was at least ${float(user.get('lp_floor_usd') or 500):.0f}.\n"
        "Change with /lpguard 50 500"
    )


async def buylimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args or len(context.args) < 2:
        await update.effective_message.reply_text(
            "Buy when price hits target.\n"
            "/buylimit <CA> <price>\n"
            "/buylimit <CA> <price> 3   (spend $3)\n"
            "Example: /buylimit 0xabc... 0.00001 3"
        )
        return
    mint = context.args[0].strip()
    try:
        target = float(context.args[1])
        usd = float(context.args[2]) if len(context.args) > 2 else float(signer.max_usd())
    except ValueError:
        await update.effective_message.reply_text("Price and size must be numbers.")
        return
    usd = min(signer.max_usd(), max(1.0, usd))
    chain = "sol" if not mint.startswith("0x") else "bsc"
    lid = db.add_buy_limit(update.effective_user.id, mint, chain, usd, target)
    await update.effective_message.reply_text(
        f"⏳ Buy limit #{lid}\n{_fmt_px(target)} · ${usd:.0f}\n`{mint}`",
        parse_mode="Markdown",
    )


async def limits_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = db.list_buy_limits(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("No limits. /buylimit <CA> <price> 3")
        return
    lines = []
    for r in rows[:12]:
        now = _token_mark_usd(r["mint"])
        lines.append(
            f"#{r['id']} {r['status']}  tgt {_fmt_px(r['target_px'])}  "
            f"now {_fmt_px(now)}  ${r['usd']:.0f}"
        )
    await update.effective_message.reply_text("Buy limits\n" + "\n".join(lines))


async def cancellimit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /cancellimit <id>")
        return
    db.cancel_buy_limit(update.effective_user.id, int(context.args[0]))
    await update.effective_message.reply_text("Cancelled.")


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
    uid = update.effective_user.id
    total_worth = 0.0
    total_cost = 0.0
    priced_positions = 0
    for row in rows[:6]:
        px = _token_mark_usd(row["mint"])
        worth = float(row["amount"] or 0) * px
        cost = db.live_cost(uid, row["mint"])
        if worth > 0:
            total_worth += worth
            if cost > 0:
                total_cost += cost
                priced_positions += 1
    evm_addr_for_totals = (db.get_user_wallet(uid) or {}).get("evm_pub") or ""
    evm_worth_seen: set[str] = set()
    if evm_addr_for_totals:
        for mint in db.live_mints(uid):
            if not str(mint).startswith("0x") or mint in evm_worth_seen:
                continue
            for cid in ("eth", "base", "bsc", "hood", "arb", "avax"):
                try:
                    raw = evm_signer._erc20_balance(CHAINS[cid]["rpc"], mint, evm_addr_for_totals)
                except Exception:
                    raw = 0
                if raw <= 0:
                    continue
                evm_worth_seen.add(mint)
                px = _token_mark_usd(mint)
                worth = (raw / 10**18) * px
                cost = db.live_cost(uid, mint)
                if worth > 0:
                    total_worth += worth
                    if cost > 0:
                        total_cost += cost
                        priced_positions += 1
                break
    summary = f"🎒 <b>Wallet positions</b> · SOL\n💰 {lamports / 1e9:.6f} SOL\n<code>{html.escape(addr)}</code>"
    if priced_positions > 0:
        total_pnl = total_worth - total_cost
        total_pct = (total_pnl / total_cost) * 100 if total_cost > 0 else 0.0
        mark = "🟢" if total_pnl >= 0 else "🔴"
        summary += (
            f"\n\n{mark} <b>Portfolio PnL {total_pnl:+,.2f} USD ({total_pct:+.1f}%)</b>\n"
            f"📥 Cost ${total_cost:,.2f}   💰 Worth ${total_worth:,.2f}"
        )
        if priced_positions < len(rows[:6]) + len(evm_worth_seen):
            summary += "\n<i>Only counts positions bought live through Ferzan.</i>"
    await update.effective_message.reply_text(summary, parse_mode="HTML")
    if not rows:
        await update.effective_message.reply_text("No SPL tokens yet.")
    for row in rows[:6]:
        text, kb = _bag_panel(row["mint"], row["amount"], addr, uid)
        await update.effective_message.reply_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )
    evm_addr = (db.get_user_wallet(uid) or {}).get("evm_pub") or ""
    for mint in db.live_mints(uid):
        if not str(mint).startswith("0x") or not evm_addr:
            continue
        for cid in ("eth", "base", "bsc", "hood", "arb", "avax"):
            try:
                raw = evm_signer._erc20_balance(CHAINS[cid]["rpc"], mint, evm_addr)
            except Exception:
                raw = 0
            if raw <= 0:
                continue
            text, kb = _bag_panel(mint, raw / 10**18, evm_addr, uid)
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
    _ok, msg = signer.sell_sol(
        context.args[0].strip(),
        secret=sol_secret,
        slip_bps=_slip_bps(update.effective_user.id, "sell"),
    )
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
            if key in {"size", "size_pct", "buy"}:
                val = float(raw)
                if key == "buy" or val > 20:
                    if not 1 <= val <= 5000:
                        raise ValueError
                    db.update_user(uid, buy_usd=val)
                else:
                    if not 1 <= val <= 50:
                        raise ValueError
                    db.update_user(uid, size_pct=val)
            elif key in {"buyslip", "slip"}:
                val = float(raw)
                if not 0.1 <= val <= 99:
                    raise ValueError
                db.update_user(uid, buy_slip_pct=val)
            elif key == "sellslip":
                val = float(raw)
                if not 0.1 <= val <= 99:
                    raise ValueError
                db.update_user(uid, sell_slip_pct=val)
            elif key == "autobuy":
                val = float(raw)
                if not 0 <= val <= 5000:
                    raise ValueError
                db.update_user(uid, auto_buy_usd=val)
                db.set_flag(uid, "auto_buy", val > 0)
            elif key in {"floor", "min", "score"}:
                val = int(raw)
                if not 0 <= val <= 90:
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
                await update.effective_message.reply_text(
                    "Keys: buy, buyslip, sellslip, autobuy, floor, cap, alerts"
                )
                return
        except ValueError:
            await update.effective_message.reply_text("Out of range.")
            return
        user = db.get_user(uid)
        await update.effective_message.reply_text("Updated.")
    rug = db.flag_on(uid, "rug_buy", 1)
    honey = db.flag_on(uid, "honeypot", 1)
    lpw = db.flag_on(uid, "lp_watch", 0)
    gate = db.flag_on(uid, "score_gate", 0)
    auto = db.flag_on(uid, "auto_buy", 0)
    mev = db.flag_on(uid, "anti_mev", 1)
    copy_live = db.flag_on(uid, "copy_live", 0)
    buy_usd = float(user.get("buy_usd") or 25)
    bslip = float(user.get("buy_slip_pct") or 10)
    sslip = float(user.get("sell_slip_pct") or 10)
    abuy = float(user.get("auto_buy_usd") or 0)
    await update.effective_message.reply_text(
        "⚙️ Ferzan desk\n"
        f"💵 Default buy  ${buy_usd:.0f}   ( /settings buy 25 )\n"
        f"📉 Buy slip {bslip:.0f}%   Sell slip {sslip:.0f}%\n"
        f"   /settings buyslip 10   /settings sellslip 10\n"
        f"⚡️ Auto-buy paste  {'ON $'+str(int(abuy)) if auto and abuy else 'OFF'}\n"
        f"   /settings autobuy 25   (0 = off)\n"
        f"{'🟢' if mev else '🔴'} Anti-MEV\n"
        f"🎯 Score floor  {user['min_confluence']}   ( /settings floor 0 )\n\n"
        "🛡 Protection — you turn these on or off\n"
        f"{'🟢' if gate else '🔴'} Block buy if score under floor\n"
        f"{'🟢' if rug else '🔴'} Block buys if liq is thin / gone\n"
        f"{'🟢' if honey else '🔴'} Block buys if honeypot / unsellable\n"
        f"{'🟢' if lpw else '🔴'} Auto-sell if LP is yanked after you're in\n"
        f"DM alerts {'on' if user.get('alerts_on') else 'off'}",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(
                    f"{'🟢' if user.get('alerts_on') else '🔴'} DM launch alerts",
                    callback_data="flg:alerts",
                )],
                [InlineKeyboardButton(
                    f"{'🟢' if auto else '🔴'} Auto-buy pasted CA",
                    callback_data="flg:auto_buy",
                )],
                [InlineKeyboardButton(
                    f"{'🟢' if mev else '🔴'} Anti-MEV",
                    callback_data="flg:anti_mev",
                )],
                [InlineKeyboardButton(
                    f"{'🟢' if copy_live else '🔴'} Live copy-mirror",
                    callback_data="flg:copy_live",
                )],
                [InlineKeyboardButton(
                    f"{'🟢' if gate else '🔴'} Score floor gate",
                    callback_data="flg:score_gate",
                )],
                [InlineKeyboardButton(
                    f"{'🟢' if rug else '🔴'} Rug buy-block",
                    callback_data="flg:rug_buy",
                )],
                [InlineKeyboardButton(
                    f"{'🟢' if honey else '🔴'} Honeypot block",
                    callback_data="flg:honeypot",
                )],
                [InlineKeyboardButton(
                    f"{'🟢' if lpw else '🔴'} LP yank auto-sell",
                    callback_data="flg:lp_watch",
                )],
                [InlineKeyboardButton("📡 Per-chain feeds", callback_data="go:feeds")],
            ]
        ),
    )


FEED_CHAINS = (
    "sol", "bsc", "base", "eth", "arb", "avax", "hood", "hype",
    "sonic", "monad", "pol", "pulse", "ink", "ton", "op", "linea", "trx",
)


def _feeds_keyboard(uid: int) -> InlineKeyboardMarkup:
    rows = []
    pair = []
    for cid in FEED_CHAINS:
        on = db.flag_on(uid, f"feed_{cid}", 1)
        label = "HOOD" if cid == "hood" else cid.upper()
        pair.append(
            InlineKeyboardButton(
                f"{'🟢' if on else '🔴'} {label}",
                callback_data=f"fd:{cid}",
            )
        )
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return InlineKeyboardMarkup(rows)


async def setfeed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    is_chan = bool(update.channel_post) or (chat and chat.type == "channel")
    if not is_chan and not await guard(update):
        return
    if not chat or chat.type not in {"channel", "group", "supergroup"}:
        await update.effective_message.reply_text(
            "Add Ferzan as admin in a channel, then send /setfeed in that channel.\n"
            "Or FERZAN_FEED_CHAT=-100xxxxxxxxxx on the droplet."
        )
        return
    raw = (context.args[0] if context.args else "*").lower()
    chain = "*" if raw in {"*", "all", "any"} else (resolve_chain(raw) or raw)
    db.add_feed_chat(chat.id, chat.title or "", chain=chain)
    label = "ALL CHAINS" if chain == "*" else chain.upper()
    await update.effective_message.reply_text(
        f"📡 This chat is the {label} feed.\n{chat.id}\n"
        "/setfeed bsc · /setfeed eth · /setfeed sol · /setfeed base"
    )


async def sponsor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args or len(context.args) < 4:
        await update.effective_message.reply_text(
            "Paid slot (you collect off-Telegram).\n"
            "/sponsor ad bsc Title https://t.me/x 24\n"
            "/sponsor trend eth $TICKER https://t.me/x 12"
        )
        return
    kind, chain, title = context.args[0], context.args[1], context.args[2]
    url = context.args[3]
    hours = float(context.args[4]) if len(context.args) > 4 else 24
    if kind not in {"ad", "trend"}:
        await update.effective_message.reply_text("kind: ad or trend")
        return
    sid = db.add_sponsored(chain, kind, title, url, hours)
    await update.effective_message.reply_text(f"Slot #{sid} {kind} {chain} {hours:g}h")


async def unsetfeed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    chat = update.effective_chat
    if chat:
        db.drop_feed_chat(chat.id)
    await update.effective_message.reply_text("Feed off in this chat.")


async def feeds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        "📡 Launch feed by chain\n"
        "🟢 on · 🔴 off\n"
        "Master switch is still /settings alerts.",
        reply_markup=_feeds_keyboard(uid),
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


async def smartmoney_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    wallets = db.list_curated_wallets()
    if not wallets:
        await update.effective_message.reply_text(
            "🐋 Smart money\n\nNo curated wallets yet — check back soon."
        )
        return
    uid = update.effective_user.id
    already = {
        (w.get("chain"), (w.get("address") or "").lower())
        for w in db.list_watched_wallets(uid)
    }
    lines = ["🐋 <b>Smart money — curated wallets</b>", "Tap Follow to get DM pings when one moves.\n"]
    rows = []
    for w in wallets:
        tag = " · following" if (w["chain"], w["address"].lower()) in already else ""
        note = f" — {html.escape(w['note'])}" if w.get("note") else ""
        lines.append(f"👁 <b>{html.escape(w['label'])}</b> ({w['chain'].upper()}){note}{tag}")
        lines.append(f"<code>{html.escape(w['address'])}</code>\n")
        if (w["chain"], w["address"].lower()) not in already:
            rows.append([InlineKeyboardButton(f"👁 Follow {w['label']}", callback_data=f"sw:follow:{w['id']}")])
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows) if rows else None,
    )


async def addsmartwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    if not _is_admin(uid):
        await update.effective_message.reply_text("Admins only.")
        return
    if len(context.args) < 3:
        await update.effective_message.reply_text(
            "Usage: /addsmartwallet <chain> <address> <label...>\n"
            "Example: /addsmartwallet sol 7xKX... \"early pump.fun sniper\""
        )
        return
    chain_raw, address = context.args[0], context.args[1]
    label = " ".join(context.args[2:])
    try:
        chain = onchain.normalize_chain(chain_raw)
    except OnchainError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    ok = db.add_curated_wallet(chain, address, label, "", uid)
    if not ok:
        await update.effective_message.reply_text("Already on the curated list for that chain.")
        return
    await update.effective_message.reply_text(f"🐋 Added {label} ({chain}) to smart money.")


async def removesmartwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not _is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Admins only.")
        return
    if not context.args:
        wallets = db.list_curated_wallets()
        listing = "\n".join(f"#{w['id']} {w['label']} ({w['chain']})" for w in wallets) or "None yet."
        await update.effective_message.reply_text(f"Usage: /removesmartwallet <id>\n\n{listing}")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Usage: /removesmartwallet <id>")
        return
    ok = db.remove_curated_wallet(wid)
    await update.effective_message.reply_text("Removed." if ok else "No wallet with that id.")


def _onramp_url(code: str, address: str) -> str:
    pk = (os.getenv("MOONPAY_PK") or os.getenv("MOONPAY_KEY") or "").strip()
    sk = (os.getenv("MOONPAY_SK") or "").strip()
    from urllib.parse import quote, urlencode
    params = {
        "currencyCode": code,
        "walletAddress": address,
        "baseCurrencyCode": "usd",
        "enabledPaymentMethods": "apple_pay,google_pay,credit_debit_card",
        "showWalletAddressForm": "true",
    }
    if pk:
        params["apiKey"] = pk
    q = urlencode(params)
    url = "https://buy.moonpay.com/?" + q
    if pk and sk:
        import base64
        import hashlib
        import hmac
        sig = base64.b64encode(
            hmac.new(sk.encode(), ("?" + q).encode(), hashlib.sha256).digest()
        ).decode()
        url += "&signature=" + quote(sig)
    return url


def _offramp_url(code: str) -> str:
    pk = (os.getenv("MOONPAY_PK") or os.getenv("MOONPAY_KEY") or "").strip()
    from urllib.parse import urlencode
    params = {"baseCurrencyCode": code, "quoteCurrencyCode": "usd"}
    if pk:
        params["apiKey"] = pk
    return "https://sell.moonpay.com/?" + urlencode(params)


def buy_fiat_keyboard(uid: int) -> InlineKeyboardMarkup:
    row = db.get_user_wallet(uid) or {}
    sol = row.get("sol_pub") or ""
    evm = row.get("evm_pub") or ""
    buttons = []
    if sol:
        buttons.append(
            [InlineKeyboardButton("🍎 Buy SOL · Apple Pay", url=_onramp_url("sol", sol))]
        )
    if evm:
        buttons.append(
            [InlineKeyboardButton("🍎 Buy ETH", url=_onramp_url("eth", evm))]
        )
        buttons.append(
            [
                InlineKeyboardButton("🟦 Buy ETH on Base", url=_onramp_url("eth_base", evm)),
                InlineKeyboardButton("💵 USDC on Base", url=_onramp_url("usdc_base", evm)),
            ]
        )
        buttons.append(
            [InlineKeyboardButton("🟡 Buy BNB", url=_onramp_url("bnb", evm))]
        )
    buttons.append(
        [
            InlineKeyboardButton("🏦 Cash out SOL", url=_offramp_url("sol")),
            InlineKeyboardButton("🏦 Cash out ETH", url=_offramp_url("eth")),
        ]
    )
    buttons.append(
        [InlineKeyboardButton("🏦 Cash out Base ETH", url=_offramp_url("eth_base"))]
    )
    buttons.append([InlineKeyboardButton("↩️ Wallets", callback_data="go:wallets")])
    return InlineKeyboardMarkup(buttons)


async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    row = db.get_user_wallet(uid)
    if not row:
        await update.effective_message.reply_text("Generate a wallet first: /wallet")
        return
    text = (
        "🍎 <b>Buy gas / 🏦 Cash out</b>\n\n"
        "⚠️ <b>READ THIS BEFORE YOU PAY</b>\n"
        "MoonPay opens in the browser. Ferzan cannot lock their destination yet.\n"
        "On the MoonPay screen, set <b>Receive / wallet</b> to the address below.\n"
        "If it says “MoonPay wallet”, change it or cancel. Funds sent there are not in Ferzan.\n\n"
        "🟣 SOL (Solana only)\n"
        f"<code>{html.escape(row.get('sol_pub') or '')}</code>\n\n"
        "🔷 EVM — same 0x on ETH, Base, and BNB. Pick the <b>network</b> to match the button.\n"
        f"<code>{html.escape(row.get('evm_pub') or '')}</code>\n\n"
        "Cash out: MoonPay shows a deposit address. Send FROM Ferzan on that same chain.\n"
        "Fees and KYC are MoonPay’s. Apple Pay is on their page, not inside Telegram."
    )
    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=buy_fiat_keyboard(uid)
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
            [InlineKeyboardButton("🍎 Buy SOL / ETH / Base", callback_data="go:buy")],
            [InlineKeyboardButton("🔗 Addresses by chain", callback_data="wi:chains")],
            [InlineKeyboardButton("🗝️ Export keys", callback_data="wi:exp")],
        ]
    )


def chain_board_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    has = bool(user_id and db.get_user_wallet(user_id))
    labels = {
        "sol": "SOL", "bsc": "BSC", "base": "BASE", "eth": "ETH",
        "monad": "MONAD", "sonic": "SONIC", "avax": "AVAX", "arb": "ARB",
        "hype": "HYPE", "hood": "HOOD", "pol": "POL", "pulse": "PULSE",
        "ink": "INK", "op": "OP", "linea": "LINEA", "arc": "ARC",
        "stable": "STABLE", "trx": "TRX", "ton": "TON",
    }
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for cid in ACTIVE:
        tap = f"wa:{cid}" if has else "wi:gen"
        row.append(InlineKeyboardButton(labels.get(cid, cid.upper()), callback_data=tap))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("🍎 Buy gas", callback_data="go:buy")])
    rows.append(
        [
            InlineKeyboardButton("📥 Import", callback_data="wi:imp"),
            InlineKeyboardButton("✨ Generate", callback_data="wi:gen"),
        ]
    )
    rows.append([InlineKeyboardButton("↩️ Return", callback_data="go:home")])
    return InlineKeyboardMarkup(rows)


def wallet_keyboard(user_id: int | None = None) -> InlineKeyboardMarkup:
    return chain_board_keyboard(user_id)


async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    row = db.get_user_wallet(update.effective_user.id)
    if row:
        text = "Tap a chain for the deposit address and balance."
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
    if len(dests) < 2:
        await update.effective_message.reply_text("Need at least two Solana addresses.")
        return
    sol_secret, _evm = user_wallets.secrets(update.effective_user.id)
    try:
        kp = signer.keypair_from_secret(sol_secret)
        bag = signer.sol_balance_lamports(str(kp.pubkey()))
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    fee_each = 5000
    spendable = bag - fee_each * len(dests)
    if spendable <= 0:
        await update.effective_message.reply_text("Not enough SOL to disperse.")
        return
    chunk = spendable // len(dests)
    lines = []
    for dest in dests:
        _ok, msg = signer.send_sol(dest, secret=sol_secret, lamports=chunk)
        lines.append(msg)
    await update.effective_message.reply_text("📤 Disperse\n" + "\n".join(lines))


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


_PROMO_TS: dict[int, float] = {}


async def send_launch(bot, chat_id: int, text: str, markup, promo: bool = True) -> None:
    clip = PROMO_PATH if promo and PROMO_PATH.exists() else None
    want_gif = (
        bool(clip)
        and os.getenv("FERZAN_PROMO_ON_SIGNALS", "0") == "1"
        and (time.time() - _PROMO_TS.get(int(chat_id), 0) > 3600)
    )
    try:
        if want_gif:
            with clip.open("rb") as gif:
                await bot.send_animation(
                    chat_id,
                    animation=gif,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=markup,
                )
            _PROMO_TS[int(chat_id)] = time.time()
            return
        await bot.send_message(
            chat_id,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        name = type(exc).__name__
        wait = float(getattr(exc, "retry_after", 0) or 0)
        if wait > 0:
            await asyncio.sleep(min(wait, 20))
        if name in {"RetryAfter", "TimedOut", "NetworkError", "TelegramError"} or wait:
            try:
                await bot.send_message(
                    chat_id,
                    text,
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
                return
            except Exception:
                logger.exception("signal send retry failed for %s", chat_id)
                return
        logger.exception("signal send failed for %s", chat_id)


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
    mc = float(getattr(ln, "fdv_usd", 0) or 0)
    px = float(getattr(ln, "price_usd", 0) or 0)
    if ca and (mc <= 0 or px <= 0):
        try:
            rr = requests.get(
                f"https://api.dexscreener.com/latest/dex/tokens/{ca}",
                timeout=4,
            )
            pairs = (rr.json() or {}).get("pairs") or []
            if pairs:
                p = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd") or 0))
                if mc <= 0:
                    mc = float(p.get("fdv") or p.get("marketCap") or 0)
                if px <= 0:
                    px = float(p.get("priceUsd") or 0)
                if liq <= 0:
                    liq = float((p.get("liquidity") or {}).get("usd") or 0)
        except Exception:
            pass
    cap = int(signer.max_usd())
    href = (CHAINS.get(cid, {}).get("explorer_addr") or "").format(addr=ca) if ca.startswith("0x") or cid == "sol" else ""
    if cid == "sol" and ca:
        href = f"https://solscan.io/token/{ca}"
    title = f"{mark} <b>${name}</b>"
    links = []
    if href:
        links.append(f'<a href="{html.escape(href)}">Scan</a>')
    text = (
        f"{title}\n"
        f"CA\n<code>{html.escape(ca)}</code>\n"
        f"💧 {_esc(cid.upper() if cid else chain)}  ·  ⛓ {chain}\n"
        f"🧢 MC {_esc(f'${mc:,.0f}' if mc else '—')}   💧 Liq ${_esc(f'{liq:,.0f}')}\n"
        + (f"💵 {_esc(_fmt_px(px))}\n" if px else "")
        + (f"⏱ {_esc(_pair_age(getattr(ln, 'created_at', '') or ''))}\n" if getattr(ln, "created_at", None) else "")
        + ("🔥 DexScreener hot\n" if getattr(ln, "source", "") == "dexscreener-boost" else "")
        + (
            f"🚀 {float(getattr(ln, 'chg_1h', 0) or 0):+.1f}% 1h\n"
            if abs(float(getattr(ln, "chg_1h", 0) or 0)) >= 1
            else ""
        )
        + (
            (
                f"{'▲' if float(ln.pulse_chg) >= 0 else '▼'} "
                f"{float(ln.pulse_chg):+.2f}% since last pulse"
                f" ({int(getattr(ln, 'pulse_mins', 0) or 0)}m)\n"
            )
            if getattr(ln, "pulse_chg", None) is not None
            else ""
        )
        + (" · ".join(links) + "\n" if links else "")
        + "<i>Tap CA to copy · Buy opens the Ferzan bot</i>"
    )
    ads = db.list_sponsored(cid or "*", "ad")
    trends = db.list_sponsored(cid or "*", "trend")
    if trends:
        text += f"\n🔥 Trending <b>{html.escape(trends[0]['title'])}</b>"
    if ads:
        text += f"\n📣 {html.escape(ads[0]['title'])}"
    elif os.getenv("FERZAN_AD_TITLE", "").strip():
        text += f"\n📣 {html.escape(os.getenv('FERZAN_AD_TITLE', ''))}"
    short = ca if len(ca) <= 48 else ca[:48]
    bot_user = (os.getenv("FERZAN_BOT_USERNAME") or "").lstrip("@")
    desk = f"https://t.me/{bot_user}?start=sig_{short}" if bot_user and short else ""
    buy_link = f"https://t.me/{bot_user}?start=buy_{short}" if bot_user and short else ""
    rows = [
        [
            InlineKeyboardButton("📡 Score", url=desk) if desk else InlineKeyboardButton("📡 Score", callback_data=f"sig:{short}"),
            InlineKeyboardButton("💵 Buy", url=buy_link) if buy_link else InlineKeyboardButton("💵 Buy", callback_data=f"buy:{short}"),
        ],
    ]
    if href:
        rows.append([InlineKeyboardButton("🔎 Scan", url=href)])
    ds_net = {
        "sol": "solana", "eth": "ethereum", "bsc": "bsc", "base": "base",
        "arb": "arbitrum", "avax": "avalanche", "hood": "robinhood",
    }.get(cid or "", "solana")
    dt_net = {
        "sol": "solana", "eth": "ether", "bsc": "bnb", "base": "base",
        "arb": "arbitrum", "avax": "avalanche",
    }.get(cid or "")
    charts = []
    if ca:
        charts.append(InlineKeyboardButton("📈 DexScreener", url=f"https://dexscreener.com/{ds_net}/{ca}"))
    if ca and dt_net:
        charts.append(InlineKeyboardButton("🛠 DexTools", url=f"https://www.dextools.io/app/en/{dt_net}/pair-explorer/{ca}"))
    if charts:
        rows.append(charts)
    chat_url = (os.getenv("FERZAN_CHAT_URL") or "").strip()
    extra_row = []
    if desk:
        extra_row.append(InlineKeyboardButton("🤖 Ferzan bot", url=desk))
    if chat_url:
        extra_row.append(InlineKeyboardButton("💬 Main chat", url=chat_url))
    if extra_row:
        rows.append(extra_row)
    ad_url = (os.getenv("FERZAN_AD_URL") or "").strip()
    if ads:
        rows.append([InlineKeyboardButton(f"📣 {ads[0]['title'][:28]}", url=ads[0]["url"])])
    elif ad_url:
        rows.append([InlineKeyboardButton("📣 Partner", url=ad_url)])
    if trends:
        rows.append([InlineKeyboardButton(f"🔥 {trends[0]['title'][:28]}", url=trends[0]["url"])])
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
        await send_launch(context.bot, update.effective_chat.id, text, markup)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rpc = (os.getenv("SOLANA_RPC_URL") or os.getenv("HELIUS_API_KEY") or "").strip()
    rpc_on = "Helius/RPC set" if rpc else "public RPC"
    binds = len(db.list_feed_binds())
    live = "ON" if signer.live_enabled() else "OFF"
    prio = os.getenv("PRIORITY_FEE_LAMPORTS", "1000000")
    jito = "ON" if os.getenv("JITO_ENABLED", "").strip() in {"1", "true", "yes"} else "OFF"
    await update.effective_message.reply_text(
        "🩺 Ferzan Trade Bot\n"
        f"Live buys {live}\n"
        "Live swap: SOL + EVM list + TRON SunSwap\n"
        "TON: STON.fi quote live; send after pip install pytoniq\n"
        "Signals only: Pulse\n"
        f"SOL send {rpc_on}\n"
        f"Tip {prio} lamports · Jito {jito}\n"
        f"Feed binds {binds}\n"
        f"Cut {fees.current_bps() / 100:.2f}%\n"
        "If rooms go quiet: systemctl status ferzan"
    )


async def ref_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    bot = os.getenv("FERZAN_BOT_USERNAME", "Ferzan_Trade_Bot").lstrip("@")
    link = f"https://t.me/{bot}?start=ref_{uid}"
    user = db.get_user(uid) or {}
    parent = user.get("referred_by") or "none"
    st = db.referral_stats(uid)
    await update.effective_message.reply_text(
        "🤝 FERZAN DESK SHARE\n"
        "Not a cashback gimmick. You earn a slice of OUR cut when they trade.\n"
        "They get Ape Pass — first 30 days on your link.\n\n"
        f"Tier  <b>{st['tier']}</b>\n"
        f"Invites  {st['invites']}\n"
        f"Their volume  ${st['volume']:,.0f}\n"
        f"Your share  ${st['earned']:.4f}\n"
        f"Claimable  ${st['open']:.4f}\n\n"
        f"Scout 30% of our cut · Captain 35% at $50k · Desk 40% at $250k\n\n"
        f"Link\n{link}\n"
        f"Referred by: {parent}\n"
        "/claim when claimable ≥ $5 — paid from treasury to your /wallet.",
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def claim_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    st = db.referral_stats(uid)
    if st["open"] < 5:
        await update.effective_message.reply_text(
            f"Claimable ${st['open']:.4f}. Minimum $5. Keep sharing /ref."
        )
        return
    amt = db.request_referral_claim(uid)
    await update.effective_message.reply_text(
        f"🧾 Claim locked ${amt:.4f}.\n"
        "Operator pays this from FEE_WALLET to your Ferzan SOL address.\n"
        "Not instant on-chain in this build — the ticket is in the ledger."
    )


async def treasury_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not _is_operator(update.effective_user.id):
        await update.effective_message.reply_text("Operator only.")
        return
    wallets = fees.fee_wallets()
    await update.effective_message.reply_text(
        "🏦 Treasury\n"
        f"SOL  {wallets.get('sol') or 'FEE_WALLET_SOL not set'}\n"
        f"EVM  {wallets.get('evm') or 'FEE_WALLET_EVM not set'}\n"
        f"Jup  {wallets.get('jupiter_fee_account') or 'JUPITER_FEE_ACCOUNT not set'}\n"
        f"Cut  {fees.current_bps() / 100:.2f}%"
    )
    await fees_cmd(update, context)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    pending = (context.user_data or {}).get("buyx")
    if pending:
        context.user_data["buyx"] = None
        try:
            amt = float(text.replace(",", ""))
        except ValueError:
            await update.effective_message.reply_text("Send a number. Example: 0.05")
            return
        try:
            card = analyze(pending)
        except Exception as exc:
            await update.effective_message.reply_text(str(exc))
            return
        cid = resolve_chain(card.snapshot.chain) or "sol"
        gecko = {"sol": "solana", "bsc": "binancecoin", "avax": "avalanche-2"}.get(cid, "ethereum")
        try:
            px = get_price_usd(gecko)
        except Exception:
            px = 0
        usd_o = amt * px if px > 0 else _default_buy_usd(update.effective_user.id)
        live_msg = _live_buy_followup(
            update.effective_user.id, card, pending, True, True, usd_override=usd_o
        )
        await update.effective_message.reply_text(f"{amt:g} native ≈ ${usd_o:.2f}\n{live_msg}")
        return
    if len(text) > 80:
        return
    await _send_signal(update, text)
    uid = update.effective_user.id
    if update.effective_chat and update.effective_chat.type != "private":
        return
    if not db.flag_on(uid, "auto_buy", 0):
        return
    user = db.get_user(uid) or {}
    usd = float(user.get("auto_buy_usd") or 0)
    if usd <= 0:
        return
    looks = (text.startswith("0x") and len(text) == 42) or (len(text) >= 32 and " " not in text)
    if not looks:
        return
    try:
        card = analyze(text)
    except Exception:
        return
    live_msg = _live_buy_followup(uid, card, text, True, True, usd_override=usd)
    await update.effective_message.reply_text(f"⚡️ Auto-buy ${usd:.0f}\n{live_msg}")


BRIDGE = {
    "sol": {"name": "Solana", "id": 792703809, "slug": "solana", "unit": "SOL", "zero": "11111111111111111111111111111111"},
    "eth": {"name": "Ethereum", "id": 1, "slug": "ethereum", "unit": "ETH", "zero": "0x0000000000000000000000000000000000000000"},
    "base": {"name": "Base", "id": 8453, "slug": "base", "unit": "ETH", "zero": "0x0000000000000000000000000000000000000000"},
    "bsc": {"name": "BNB", "id": 56, "slug": "bsc", "unit": "BNB", "zero": "0x0000000000000000000000000000000000000000"},
    "arb": {"name": "Arbitrum", "id": 42161, "slug": "arbitrum", "unit": "ETH", "zero": "0x0000000000000000000000000000000000000000"},
    "pol": {"name": "Polygon", "id": 137, "slug": "polygon", "unit": "POL", "zero": "0x0000000000000000000000000000000000000000"},
    "avax": {"name": "Avalanche", "id": 43114, "slug": "avalanche", "unit": "AVAX", "zero": "0x0000000000000000000000000000000000000000"},
    "op": {"name": "Optimism", "id": 10, "slug": "optimism", "unit": "ETH", "zero": "0x0000000000000000000000000000000000000000"},
}


def _bridge_state(context) -> dict:
    st = context.user_data.setdefault("bridge", {"from": "sol", "to": "eth", "amt": "0.1"})
    return st


def _bridge_addr(uid: int, key: str) -> str:
    try:
        w = user_wallets.ensure(uid) or {}
    except Exception:
        w = {}
    if key == "sol":
        return w.get("sol_pub") or ""
    return w.get("evm_pub") or ""


def _bridge_bal(uid: int, key: str) -> str:
    addr = _bridge_addr(uid, key)
    unit = (BRIDGE.get(key) or {}).get("unit") or ""
    if not addr:
        return f"Available · open /wallet"
    try:
        if key == "sol":
            import signer

            amt = signer.sol_balance_lamports(addr) / 1e9
            return f"Available · <b>{amt:.6f}</b> SOL"
        import evm_signer

        amt, sym = evm_signer.native_balance(key, addr)
        return f"Available · <b>{float(amt):.6f}</b> {sym or unit}"
    except Exception:
        return f"Available · tap Wallets to refresh {unit}"


def _bridge_url(st: dict, uid: int) -> str:
    src, dst = BRIDGE.get(st["from"]), BRIDGE.get(st["to"])
    if not src or not dst:
        return "https://relay.link"
    dest = _bridge_addr(uid, st["to"])
    q = f"fromChainId={src['id']}&amount={st['amt']}&tradeType=EXACT_INPUT"
    if dest:
        q += f"&toAddress={dest}"
    return f"https://relay.link/bridge/{dst['slug']}?{q}"


def _bridge_text(uid: int, st: dict) -> str:
    src, dst = BRIDGE[st["from"]], BRIDGE[st["to"]]
    send = _bridge_addr(uid, st["from"]) or "open /wallet"
    recv = _bridge_addr(uid, st["to"]) or "open /wallet"
    return (
        "🌉 <b>FERZAN BRIDGE</b>\n"
        "See it. Ape it. Send it.\n\n"
        "1. Pick from / to\n"
        "2. Set size\n"
        "3. Get Quote inside Ferzan. Confirm. We sign with YOUR desk wallet.\n\n"
        f"From ⛓ <b>{src['name']}</b> · {src['unit']}\n"
        f"{_bridge_bal(uid, st['from'])}\n"
        f"To ⛓ <b>{dst['name']}</b> · {dst['unit']}\n"
        f"{_bridge_bal(uid, st['to'])}\n"
        f"Size · <b>{st['amt']}</b> {src['unit']}\n"
        "<i>Leave ~0.02 of the From token for fees.</i>\n\n"
        f"📤 Send from\n<code>{send}</code>\n"
        f"📥 Receive to\n<code>{recv}</code>\n\n"
        "Signed on this box — no Telegram Wallet, no seed prompt.\n"
        "<i>Confirm the receive line is your Ferzan wallet before you tap Send.</i>"
    )


async def _watch_dln(bot, uid: int, order_id: str, dst: str) -> None:
    import asyncio
    import bridge as ferzan_bridge

    link = f"https://app.debridge.finance/order?orderId={order_id}"
    done = {"Fulfilled", "SentUnlock", "ClaimedUnlock"}
    dead = {"OrderCancelled", "ClaimedOrderCancel", "SentOrderCancel"}
    for _ in range(24):
        await asyncio.sleep(12)
        try:
            info = await asyncio.to_thread(ferzan_bridge.dln_status, order_id)
        except Exception:
            continue
        st = (info.get("status") or "").strip()
        if st in done:
            extra = f"\nDest tx: {info['dest_tx']}" if info.get("dest_tx") else ""
            chain = (BRIDGE.get(dst) or {}).get("name") or dst or "destination"
            await bot.send_message(
                uid,
                f"✅ Bridge complete on {chain}.\n{link}{extra}",
            )
            return
        if st in dead:
            await bot.send_message(uid, f"Bridge did not complete.\n{link}\nStatus: {st}")
            return
    await bot.send_message(uid, f"Still settling. Track the order:\n{link}")


def _bridge_kb(st: dict, uid: int) -> InlineKeyboardMarkup:
    src, dst = BRIDGE[st["from"]], BRIDGE[st["to"]]
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("↔️ Flip", callback_data="br:flip")],
            [
                InlineKeyboardButton(f"⛓ {src['name']} | {src['unit']}", callback_data="br:pick:from"),
                InlineKeyboardButton(f"⛓ {dst['name']} | {dst['unit']}", callback_data="br:pick:to"),
            ],
            [
                InlineKeyboardButton("📤 Sending wallet", callback_data="go:wallets"),
                InlineKeyboardButton("📥 Receiving wallet", callback_data="go:wallets"),
            ],
            [
                InlineKeyboardButton("0.05", callback_data="br:a:0.05"),
                InlineKeyboardButton("0.1", callback_data="br:a:0.1"),
                InlineKeyboardButton("0.25", callback_data="br:a:0.25"),
                InlineKeyboardButton("0.5", callback_data="br:a:0.5"),
            ],
            [InlineKeyboardButton("📋 Get Quote", callback_data="br:quote")],
            [InlineKeyboardButton("✖️ Close", callback_data="go:home")],
        ]
    )


async def bridge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    st = _bridge_state(context)
    uid = update.effective_user.id
    await update.effective_message.reply_text(
        _bridge_text(uid, st), parse_mode="HTML", reply_markup=_bridge_kb(st, uid)
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await guard(update):
        return
    uid = update.effective_user.id
    data = query.data or ""
    if data.startswith("br:"):
        st = _bridge_state(context)
        parts = data.split(":")
        if parts[1] == "noop":
            return
        if parts[1] == "quote":
            try:
                import bridge as ferzan_bridge

                pack = ferzan_bridge.quote(uid, st["from"], st["to"], st["amt"])
                context.user_data["bridge_pack"] = pack
                kb = InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("✅ Send from Ferzan wallet", callback_data="br:go")],
                        [InlineKeyboardButton("↩️ Back", callback_data="go:bridge")],
                    ]
                )
                await query.edit_message_text(
                    "🌉 <b>QUOTE</b>\n" + ferzan_bridge.summarize(pack) + "\n\nTap Send. Signed on this box.",
                    parse_mode="HTML",
                    reply_markup=kb,
                )
            except Exception as exc:
                await query.edit_message_text(
                    f"Quote failed.\n{exc}\n\nFund the FROM wallet on /wallet and try a smaller size.",
                    reply_markup=_bridge_kb(st, uid),
                )
            return
        if parts[1] == "go":
            pack = context.user_data.get("bridge_pack")
            if not pack:
                await query.edit_message_text("Quote expired. Tap Get Quote again.", reply_markup=_bridge_kb(st, uid))
                return
            await query.edit_message_text("Signing on the desk… (35s cap)")
            try:
                import asyncio
                import bridge as ferzan_bridge

                msg = await asyncio.wait_for(
                    asyncio.to_thread(ferzan_bridge.execute, uid, pack),
                    timeout=45,
                )
                context.user_data.pop("bridge_pack", None)
                order_id = ""
                dst = pack.get("dst") or ""
                if isinstance(msg, dict):
                    order_id = msg.get("order_id") or ""
                    dst = msg.get("dst") or dst
                    text = msg.get("text") or str(msg)
                else:
                    text = str(msg)
                await context.bot.send_message(uid, "✅ " + text)
                if order_id:
                    asyncio.create_task(_watch_dln(context.bot, uid, order_id, dst))
            except asyncio.TimeoutError:
                await context.bot.send_message(
                    uid,
                    "Bridge timed out after 35s (RPC hung).\nTry Base → ETH, or a smaller SOL size.",
                )
            except Exception as exc:
                await context.bot.send_message(uid, f"Bridge send failed.\n{exc}")
            return
        if parts[1] == "pick":
            side = parts[2] if len(parts) > 2 else "from"
            prefix = "br:f:" if side == "from" else "br:t:"
            title = "FROM chain" if side == "from" else "TO chain"
            rows, row = [], []
            for k, meta in BRIDGE.items():
                row.append(InlineKeyboardButton(meta["name"], callback_data=prefix + k))
                if len(row) == 2:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            rows.append([InlineKeyboardButton("↩️ Back", callback_data="go:bridge")])
            await query.edit_message_text(
                f"Pick {title}",
                reply_markup=InlineKeyboardMarkup(rows),
            )
            return
        if len(parts) > 2 and parts[1] == "f" and parts[2] in BRIDGE:
            st["from"] = parts[2]
            if st["from"] == st["to"]:
                st["to"] = "eth" if st["from"] != "eth" else "base"
        elif parts[1] == "t" and parts[2] in BRIDGE:
            st["to"] = parts[2]
        elif parts[1] == "a":
            st["amt"] = parts[2]
        elif parts[1] == "flip":
            st["from"], st["to"] = st["to"], st["from"]
        try:
            await query.edit_message_text(
                _bridge_text(uid, st), parse_mode="HTML", reply_markup=_bridge_kb(st, uid)
            )
        except Exception:
            await context.bot.send_message(
                uid, _bridge_text(uid, st), parse_mode="HTML", reply_markup=_bridge_kb(st, uid)
            )
        return
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
                "🧲 Collect — sweep your native balance to an address you own.\n"
                "/collectsol <your-sol-address>\n"
                "/collectevm [eth|base|bsc|hood] <your-0x-address>\n"
                "Live send, signed with your Ferzan wallet key. Sends the full "
                "balance minus network fee.",
            )
            return
        if kind == "dis":
            await context.bot.send_message(
                uid,
                "📤 Disperse — split your SOL balance equally across addresses.\n"
                "/disperse <addr1> <addr2> [addr3 ...]\n"
                "Live send, Solana only right now.",
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
        elif cid == "trx":
            try:
                import tron_signer

                _, evm_secret = user_wallets.secrets(uid)
                addr, _ = tron_signer.evm_key_to_tron(evm_secret)
            except Exception:
                addr = row["evm_pub"]
            bal_line = "Fund TRX + energy"
        elif cid == "ton":
            addr = "STON.fi quotes live · send after pytoniq"
            bal_line = "TON wallet next"
        else:
            addr = row["evm_pub"]
            try:
                amt, sym = evm_signer.native_balance(cid, addr)
                bal_line = f"{amt:.6f} {sym}"
            except Exception:
                bal_line = f"— {native}"
        if cid == "ton":
            text = (
                f"{mark} <b>TON</b>\n"
                "STON.fi quotes are live. Paste an EQ… jetton to buy.\n"
                "Send path: pytoniq is on the droplet.\n"
                "Dedicated TON deposit address ships next."
            )
        else:
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
        elif kind == "bridge":
            st = _bridge_state(context)
            await context.bot.send_message(
                uid, _bridge_text(uid, st), parse_mode="HTML", reply_markup=_bridge_kb(st, uid)
            )
        elif kind == "buy":
            await buy_cmd(update, context)
        elif kind == "bag":
            await bag_cmd(update, context)
        elif kind == "settings":
            context.args = []
            await settings_cmd(update, context)
        elif kind == "feeds":
            await feeds_cmd(update, context)
        elif kind == "snipes":
            await snipes_cmd(update, context)
        elif kind == "copy":
            await context.bot.send_message(
                uid,
                "👯 COPY\n"
                "/watchwallet sol <address>\n"
                "/watchwallet eth 0x...\n\n"
                "Ping when they move. Mirror is opt-in.\n"
                "Never posts your key.\n\n"
                "Don't have a wallet to follow yet? /smartmoney has a curated list.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🐋 Smart money", callback_data="go:smartmoney")]]
                ),
            )
        elif kind == "smartmoney":
            await smartmoney_cmd(update, context)
        elif kind == "snipehelp":
            await context.bot.send_message(
                uid,
                "🎯 SNIPE\n"
                "/snipe <CA> 25\n"
                "/snipes\n"
                "/cancelsnipe 3\n\n"
                "Arms a live buy when the pair is tradable.\n"
                "Uses your slip + default size from /settings.",
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
                "📊 BAG\n/bag — live tokens, PnL, sell %",
            )
        elif kind == "buyhelp":
            await context.bot.send_message(
                uid,
                "⚡ BUY & SELL\n"
                "Paste a CA in this chat.\n"
                "Tap $5 / $25 / 0.05 on the card.\n"
                "/settings buy 25\n"
                "/settings buyslip 10\n"
                "/settings sellslip 10\n"
                "/settings autobuy 25\n"
                "/bag to sell.",
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
        sol_secret, evm_secret = user_wallets.secrets(uid)
        if mint.startswith("T") and 30 <= len(mint) <= 36:
            import tron_signer

            _ok, msg = tron_signer.sell_tron(mint, key_hex=evm_secret)
        elif mint.startswith(("EQ", "UQ", "kQ")):
            import ton_signer

            _ok, msg = ton_signer.sell_ton(mint, secret=sol_secret)
        elif mint.startswith("0x"):
            chain = "base"
            evm_addr = (db.get_user_wallet(uid) or {}).get("evm_pub") or ""
            for cid in ("eth", "base", "bsc", "hood", "arb", "avax"):
                try:
                    raw = evm_signer._erc20_balance(CHAINS[cid]["rpc"], mint, evm_addr)
                except Exception:
                    raw = 0
                if raw > 0:
                    chain = cid
                    break
            _ok, msg = evm_signer.sell_evm(chain, mint, key_hex=evm_secret, pct=pct)
        else:
            _ok, msg = signer.sell_sol(
                mint, secret=sol_secret, pct=pct, slip_bps=_slip_bps(uid, "sell")
            )
        await context.bot.send_message(uid, f"{'🟢' if _ok else '🔴'} Sell {pct}% · {chain if mint.startswith('0x') else 'SOL'}\n{msg}")
        return
    if data.startswith("blm:"):
        mint = data[4:]
        try:
            card = analyze(mint)
            px = float(card.snapshot.price_usd or 0)
        except Exception as exc:
            await context.bot.send_message(uid, str(exc))
            return
        if px <= 0:
            await context.bot.send_message(uid, "No mark to hang a limit on.")
            return
        target = px * 0.8
        usd = float(signer.max_usd())
        chain = card.snapshot.chain or ""
        lid = db.add_buy_limit(uid, card.snapshot.token_address or mint, chain, usd, target)
        await context.bot.send_message(
            uid,
            f"⏳ Buy limit #{lid} · {_fmt_px(target)} (−20%) · ${usd:.0f}",
        )
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
    if data.startswith("fd:"):
        cid = data[3:]
        if cid not in FEED_CHAINS:
            return
        now = not db.flag_on(uid, f"feed_{cid}", 1)
        db.set_flag(uid, f"feed_{cid}", now)
        await query.answer("Saved")
        try:
            await query.edit_message_reply_markup(reply_markup=_feeds_keyboard(uid))
        except Exception:
            pass
        return
    if data.startswith("flg:"):
        flag = data[4:]
        if flag == "alerts":
            user = db.get_user(uid) or {}
            on = not bool(user.get("alerts_on"))
            db.update_user(uid, alerts_on=1 if on else 0)
            await query.answer("Saved")
            await context.bot.send_message(uid, f"{'🟢' if on else '🔴'} DM launch alerts")
            return
        if flag not in {
            "rug_buy",
            "honeypot",
            "lp_watch",
            "score_gate",
            "auto_buy",
            "anti_mev",
            "copy_live",
        }:
            return
        now = not db.flag_on(uid, flag, 1)
        db.set_flag(uid, flag, now)
        await query.answer("Saved")
        await context.bot.send_message(
            uid,
            f"{'🟢 ON' if now else '🔴 OFF'} {flag.replace('_', ' ')}",
        )
        return
    if data.startswith("watch:"):
        name = data[6:]
        db.add_watch(uid, name)
        await query.edit_message_reply_markup(reply_markup=card_keyboard(name, 0))
        await context.bot.send_message(uid, f"Watching {name.upper()}.")
        return
    if data.startswith("sw:follow:"):
        try:
            wid = int(data.split(":", 2)[2])
        except ValueError:
            return
        wallets = {w["id"]: w for w in db.list_curated_wallets()}
        w = wallets.get(wid)
        if not w:
            await query.answer("Gone.")
            return
        already = any(
            x["chain"] == w["chain"] and (x.get("address") or "").lower() == w["address"].lower()
            for x in db.list_watched_wallets(uid)
        )
        if already:
            await query.answer("Already following.")
            return
        new_id = db.add_watched_wallet(uid, w["chain"], w["address"], w["label"])
        try:
            events = onchain.recent_activity(w["chain"], w["address"], limit=1)
            if events:
                db.set_wallet_cursor(new_id, events[0].txid)
        except Exception:
            pass
        await query.answer("Following")
        await context.bot.send_message(uid, f"👁 Now following {w['label']} ({w['chain']}). DM ping when it moves.")
        return
    if data.startswith("bnv:"):
        _tag, amt_s, name = data.split(":", 2)
        try:
            amt = float(amt_s)
        except ValueError:
            amt = 0.05
        try:
            card = analyze(name)
        except PriceFetchError as exc:
            await context.bot.send_message(uid, str(exc))
            return
        cid = resolve_chain(card.snapshot.chain) or "sol"
        gecko = {
            "sol": "solana",
            "bsc": "binancecoin",
            "eth": "ethereum",
            "base": "ethereum",
            "arb": "ethereum",
            "avax": "avalanche-2",
        }.get(cid, "ethereum")
        try:
            px = get_price_usd(gecko)
        except Exception:
            px = 0
        usd_o = amt * px if px > 0 else _default_buy_usd(uid)
        usd_o = min(signer.max_usd(), max(1.0, usd_o))
        live_msg = _live_buy_followup(uid, card, name, True, True, usd_override=usd_o)
        if live_msg:
            await context.bot.send_message(uid, f"{amt:g} native ≈ ${usd_o:.2f}\n{live_msg}")
        return
    if data.startswith("buyz:"):
        _tag, usd_s, name = data.split(":", 2)
        try:
            usd_o = float(usd_s)
        except ValueError:
            usd_o = _default_buy_usd(uid)
        try:
            card = analyze(name)
        except PriceFetchError as exc:
            await context.bot.send_message(uid, str(exc))
            return
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
        live_msg = _live_buy_followup(uid, card, name, True, force)
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
    if data.startswith("buyx:"):
        context.user_data["buyx"] = data[5:]
        await context.bot.send_message(
            uid,
            "✏️ Buy X — send the native amount now.\nExample: 0.05",
        )
        return
    if data.startswith("xslip:"):
        cid = resolve_chain(data[6:]) or data[6:] or "sol"
        cur = db.get_chain_trade(uid, cid)
        nxt = {5: 10, 10: 15, 15: 25, 25: 50, 50: 5}.get(int(cur["buy_slip"]), 10)
        db.set_chain_trade(uid, cid, buy_slip=nxt, sell_slip=nxt)
        await query.answer(f"{cid.upper()} slip {nxt}%")
        await context.bot.send_message(uid, f"🎚 {cid.upper()} buy/sell slip → {nxt}%")
        return
    if data.startswith("xgas:"):
        cid = resolve_chain(data[5:]) or data[5:] or "sol"
        cur = db.get_chain_trade(uid, cid)
        now = float(cur.get("gas") or 0)
        nxt = {0.0: 0.001, 0.001: 0.005, 0.005: 0.01, 0.01: 0.0}.get(round(now, 3), 0.005)
        db.set_chain_trade(uid, cid, gas=nxt)
        await query.answer(f"{cid.upper()} gas tip {nxt}")
        await context.bot.send_message(uid, f"⛽ {cid.upper()} priority tip → {nxt}")
        return
    if data.startswith("slc:"):
        mint = data[4:].strip()
        sol_secret, evm_secret = user_wallets.secrets(uid)
        if mint.startswith("0x"):
            await context.bot.send_message(
                uid,
                "Sell pad for EVM: /livesellevm <chain> " + mint,
            )
            return
        amount = 0.0
        addr = ""
        try:
            kp = signer.keypair_from_secret(sol_secret)
            addr = str(kp.pubkey())
            for row in signer.holdings(sol_secret):
                if row.get("mint") == mint:
                    amount = float(row.get("amount") or 0)
                    break
        except Exception:
            pass
        try:
            card = analyze(mint)
            text = render_card(card, uid)
            chain = card.snapshot.chain or ""
        except Exception:
            text = _bag_panel(mint, amount, addr, uid)[0]
            chain = "sol"
        kb = sell_keyboard(mint, mint, chain, uid, amount)
        await context.bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
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
        if mint and db.flag_on(int(row["user_id"]), "copy_live", 0):
            try:
                uid = int(row["user_id"])
                sol_secret, evm_secret = user_wallets.secrets(uid)
                usd = _default_buy_usd(uid)
                if mint.startswith("0x"):
                    _ok, live = evm_signer.buy_evm(
                        chain or "base",
                        mint,
                        usd,
                        key_hex=evm_secret,
                        slip_bps=_slip_bps(uid, "buy", chain),
                        user_id=uid,
                    )
                else:
                    _ok, live = signer.buy_sol(
                        mint, usd, secret=sol_secret, slip_bps=_slip_bps(uid, "buy", "sol")
                    )
                await context.bot.send_message(uid, f"👯 Live copy ${usd:.0f}\n{live}")
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


async def buy_limit_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for row in db.list_buy_limits():
        px = _token_mark_usd(row["mint"])
        if px <= 0 or px > float(row["target_px"]):
            continue
        uid = int(row["user_id"])
        try:
            card = analyze(row["mint"])
            msg = _live_buy_followup(
                uid, card, row["mint"], True, True, usd_override=float(row["usd"])
            )
        except Exception as exc:
            msg = str(exc)
        blocked = str(msg).startswith(("🛡", "Live: skipped", "Live: OFF", "Could not"))
        if not blocked:
            db.fill_buy_limit(int(row["id"]))
        try:
            await context.bot.send_message(
                uid,
                f"⏳ Buy limit #{row['id']} hit @ {_fmt_px(px)}\n{msg}",
            )
        except Exception:
            logger.exception("buy limit notify")


async def lp_watch_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        with db.get_conn() as conn:
            users = [int(r[0]) for r in conn.execute("SELECT DISTINCT user_id FROM live_basis").fetchall()]
    except Exception:
        return
    for uid in users:
        if not db.flag_on(uid, "lp_watch", 0):
            continue
        try:
            sol_secret, evm_secret = user_wallets.secrets(uid)
        except Exception:
            continue
        for mint in db.live_mints(uid):
            liq = _token_liq_usd(mint)
            if liq < 0:
                continue
            prev = db.lp_mark(uid, mint)
            if prev <= 0:
                if liq > 0:
                    db.set_lp_mark(uid, mint, liq)
                continue
            if liq >= prev * 0.80:
                db.set_lp_mark(uid, mint, liq)
                continue
            user = db.get_user(uid) or {}
            drop_pct = float(user.get("lp_drop_pct") or 50)
            floor = float(user.get("lp_floor_usd") or 500)
            trigger = max(0.05, min(0.90, drop_pct / 100.0))
            yanked = prev >= floor and liq <= prev * (1.0 - trigger)
            if not yanked:
                db.set_lp_mark(uid, mint, liq)
                continue
            try:
                if mint.startswith("0x"):
                    chain = "base"
                    evm_addr = (db.get_user_wallet(uid) or {}).get("evm_pub") or ""
                    for cid in ("eth", "base", "bsc", "hood", "arb", "avax"):
                        try:
                            raw = evm_signer._erc20_balance(CHAINS[cid]["rpc"], mint, evm_addr)
                        except Exception:
                            raw = 0
                        if raw > 0:
                            chain = cid
                            break
                    _ok, msg = evm_signer.sell_evm(chain, mint, key_hex=evm_secret)
                else:
                    _ok, msg = signer.sell_sol(mint, secret=sol_secret, pct=100, slip_bps=_slip_bps(uid, "sell"))
            except Exception as exc:
                _ok, msg = False, str(exc)
            db.set_lp_mark(uid, mint, liq)
            if _ok:
                db.clear_live_cost(uid, mint)
                db.clear_live_exit(uid, mint)
            if "Nothing to sell" in str(msg) or "holds 0" in str(msg):
                continue
            try:
                await context.bot.send_message(
                    uid,
                    f"🛡 LP yank  ${prev:,.0f} → ${liq:,.0f}\nAuto-sell\n{msg}",
                )
            except Exception:
                logger.exception("lp watch notify")


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
        for rung in db.list_tp_rungs(uid, mint):
            if rung.get("hit") or pnl_pct < float(rung["pct"]):
                continue
            sell_pct = float(rung["sell_pct"])
            try:
                if mint.startswith("0x"):
                    _rok, rmsg = evm_signer.sell_evm(evm_chain, mint, key_hex=evm_secret, pct=int(sell_pct))
                else:
                    _rok, rmsg = signer.sell_sol(
                        mint, secret=sol_secret, pct=int(sell_pct), slip_bps=_slip_bps(uid, "sell")
                    )
            except Exception as exc:
                _rok, rmsg = False, str(exc)
            db.mark_tp_rung_hit(uid, mint, rung["pct"])
            if _rok:
                db.reduce_live_cost_pct(uid, mint, sell_pct)
            try:
                await context.bot.send_message(
                    uid,
                    f"🎯 TP rung +{float(rung['pct']):.0f}% hit — sold {sell_pct:.0f}% of bag\n{rmsg}",
                )
            except Exception:
                logger.exception("tp ladder notify failed")
            # Re-read cost/worth so the full-exit check below sees the shrunk position.
            cost = db.live_cost(uid, mint)
            if cost <= 0:
                worth = 0.0
                break
            worth = worth * (1 - sell_pct / 100.0)
            pnl_pct = ((worth - cost) / cost) * 100
        if worth <= 0:
            continue
        hit = None
        trail = float(row.get("trail_pct") or 0)
        peak = float(row.get("peak_pct") or 0)
        if pnl_pct > peak:
            peak = pnl_pct
            db.set_live_exit(uid, mint, peak_pct=peak)
        if row.get("tp_pct") and pnl_pct >= float(row["tp_pct"]):
            hit = "tp"
        if row.get("sl_pct") and pnl_pct <= -float(row["sl_pct"]):
            hit = "sl"
        if trail > 0 and peak > 0 and pnl_pct <= peak - trail:
            hit = "trail"
        if not hit:
            continue
        try:
            if mint.startswith("0x"):
                _ok, msg = evm_signer.sell_evm(evm_chain, mint, key_hex=evm_secret)
            else:
                _ok, msg = signer.sell_sol(mint, secret=sol_secret, pct=100, slip_bps=_slip_bps(uid, "sell"))
        except Exception as exc:
            msg = str(exc)
            _ok = False
        db.clear_live_exit(uid, mint)
        if _ok:
            db.clear_live_cost(uid, mint)
        try:
            await context.bot.send_message(
                uid,
                f"{'🎯 TP' if hit == 'tp' else '📉 Trail' if hit == 'trail' else '🛑 SL'} hit ({pnl_pct:+.1f}%)\n{msg}",
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
    try:
        await _launch_feed_job(context)
    except Exception:
        logger.exception("launch feed job crashed; will retry next cycle")


async def _launch_feed_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    binds = db.list_feed_binds()
    logger.info("launch feed binds=%s", len(binds))
    if not binds:
        logger.warning("no /setfeed binds — chain rooms will stay silent")
    cache: dict[str, list] = {}

    def _pool_for(bind: str) -> list:
        key = (resolve_chain(bind) or bind or "*").lower()
        if key in cache:
            return cache[key]
        if key in {"*", ""}:
            rows = []
            for major in ("sol", "bsc", "eth", "base"):
                rows.extend(sniper.fetch_new_pools(major, limit=8))
            cache[key] = rows
            return rows
        try:
            rows = sniper.fetch_new_pools(key, limit=16)
        except Exception:
            logger.exception("pool fetch failed for %s", key)
            rows = []
        cache[key] = rows
        return rows

    def _interesting(rows: list, loose: bool) -> list:
        keep = []
        for ln in rows:
            hot = getattr(ln, "source", "") in {
                "dexscreener-boost",
                "dexscreener-mover",
                "geckoterminal-trend",
                "geckoterminal-mover",
            }
            chg = abs(float(getattr(ln, "chg_1h", 0) or 0))
            if ln.liquidity_usd >= (1 if loose else 250) or hot or chg >= 3:
                keep.append(ln)
        return keep or list(rows[:8])

    for user in db.list_users():
        if not user.get("alerts_on"):
            continue
        uid = int(user["user_id"])
        diverse = _interesting(_pool_for("*"), False)
        for ln in diverse[:8]:
            cid = resolve_chain(ln.chain) or (ln.chain or "").lower()
            if cid and not db.flag_on(uid, f"feed_{cid}", 1):
                continue
            key = f"launch:{ln.chain}:{(ln.token or '')[:24]}"
            if not db.should_resend_signal(uid, key, 1, cooldown_s=45 * 60):
                continue
            text, markup = launch_card(ln)
            try:
                await send_launch(context.bot, uid, text, markup, promo=False)
            except Exception:
                logger.exception("launch feed failed for %s", uid)
            if db.flag_on(uid, "auto_buy", 0):
                auto_usd = float(user.get("auto_buy_usd") or 0)
                if auto_usd > 0 and (ln.token or "").strip():
                    try:
                        auto_card = analyze(ln.token)
                    except Exception:
                        auto_card = None
                    if auto_card is not None:
                        # force=False -- this still goes through the same
                        # score_gate / rug_buy / honeypot checks a manual
                        # paste does. Nothing here bypasses the user's flags.
                        auto_msg = _live_buy_followup(uid, auto_card, ln.token, True, False, usd_override=auto_usd)
                        if auto_msg:
                            try:
                                await context.bot.send_message(uid, f"⚡️ Auto-buy ${auto_usd:.0f} (feed)\n{auto_msg}")
                            except Exception:
                                logger.exception("auto-buy notify failed for %s", uid)

    for chat_id, bind in binds:
        rows = _pool_for(bind)
        pool = _interesting(rows, loose=True)
        if bind not in {"*", ""}:
            want = resolve_chain(bind) or bind
            pool = [
                ln
                for ln in pool
                if (resolve_chain(ln.chain) or (ln.chain or "").lower()) == want
                or (ln.chain or "").lower() == bind
            ] or rows[:4]
        sent = 0
        for ln in pool[:12]:
            key = f"ch:{chat_id}:{ln.chain}:{(ln.token or '')[:20]}"
            if not db.should_resend_signal(int(chat_id), key, 1, cooldown_s=4 * 60):
                continue
            text, markup = launch_card(ln)
            try:
                await send_launch(context.bot, chat_id, text, markup, promo=False)
                sent += 1
            except Exception:
                logger.exception("channel feed failed for %s", chat_id)
        logger.info("feed chat=%s bind=%s rows=%s sent=%s", chat_id, bind, len(rows), sent)


CG_NATIVE = {
    "sol": "solana",
    "bsc": "binancecoin",
    "eth": "ethereum",
    "base": "ethereum",
    "arb": "ethereum",
    "avax": "avalanche-2",
    "hood": "ethereum",
    "hype": "hyperliquid",
    "sonic": "sonic-3",
    "monad": "monad",
    "pol": "polygon-ecosystem-token",
    "pulse": "pulsechain",
    "ink": "ethereum",
    "ton": "the-open-network",
    "trx": "tron",
    "op": "ethereum",
    "linea": "ethereum",
}


def _native_prices() -> dict[str, float]:
    ids = ",".join(dict.fromkeys(CG_NATIVE.values()))
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": ids, "vs_currencies": "usd"},
            timeout=12,
        )
        data = r.json() if r.ok else {}
    except Exception:
        data = {}
    out: dict[str, float] = {}
    for cid, gid in CG_NATIVE.items():
        try:
            px = float((data.get(gid) or {}).get("usd") or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            out[cid] = px
    return out


NATIVE_CA = {
    "sol": "So11111111111111111111111111111111111111112",
    "eth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "base": "0x4200000000000000000000000000000000000006",
    "arb": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    "op": "0x4200000000000000000000000000000000000006",
    "linea": "0xe5D7C2a44FfDDf6b295A15c148167daaAf5Cf34f",
    "bsc": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    "avax": "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
    "pol": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
    "hood": "0x4200000000000000000000000000000000000006",
    "ink": "0x4200000000000000000000000000000000000006",
}


async def native_pulse_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    prices = _native_prices()
    if not prices:
        return
    for chat_id, bind in db.list_feed_binds():
        if bind in {"*", ""}:
            continue
        cid = resolve_chain(bind) or bind
        px = prices.get(cid)
        if not px:
            continue
        prev = db.get_native_mark(cid)
        pulse_chg = None
        pulse_mins = 0
        if prev and prev[0] > 0:
            pulse_chg = (px - prev[0]) / prev[0] * 100
            pulse_mins = max(1, int((time.time() - prev[1]) / 60))
        db.set_native_mark(cid, px)
        ticker = CHAINS.get(cid, {}).get("native") or cid.upper()
        ca = NATIVE_CA.get(cid) or ticker
        ln = sniper.Launch(
            chain=cid,
            symbol=str(ticker),
            name=CHAINS.get(cid, {}).get("label") or cid,
            token=ca,
            pool=ca,
            liquidity_usd=0,
            created_at="",
            source="chain-pulse",
            query=ca,
            price_usd=px,
            pulse_chg=pulse_chg,
            pulse_mins=pulse_mins,
        )
        text, markup = launch_card(ln)
        text += "\n<i>Chain pulse · every 10m · Buy opens the desk</i>"
        try:
            await send_launch(context.bot, chat_id, text, markup, promo=False)
        except Exception:
            logger.exception("native pulse failed for %s", chat_id)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env before running.")

    db.init_db()

    async def _post_init(application: Application) -> None:
        try:
            me = await application.bot.get_me()
            if me.username:
                os.environ["FERZAN_BOT_USERNAME"] = me.username
        except Exception:
            logger.exception("could not cache bot username")
        try:
            await application.bot.set_my_commands(
                [
                    BotCommand("start", "Home"),
                    BotCommand("signal", "Score a market"),
                    BotCommand("buy", "Live buy a CA"),
                    BotCommand("quote", "Quote + cut"),
                    BotCommand("positions", "Live bag"),
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
    app.add_handler(CommandHandler("feeds", feeds_cmd))
    app.add_handler(CommandHandler("setfeed", setfeed_cmd))
    app.add_handler(CommandHandler("setfeed", setfeed_cmd, filters=filters.UpdateType.CHANNEL_POSTS))
    app.add_handler(CommandHandler("unsetfeed", unsetfeed_cmd))
    app.add_handler(CommandHandler("sponsor", sponsor_cmd))
    app.add_handler(CommandHandler("unsetfeed", unsetfeed_cmd, filters=filters.UpdateType.CHANNEL_POSTS))
    app.add_handler(CommandHandler("resetpaper", resetpaper_cmd))
    app.add_handler(CommandHandler("watchwallet", watchwallet_cmd))
    app.add_handler(CommandHandler("smartmoney", smartmoney_cmd))
    app.add_handler(CommandHandler("addsmartwallet", addsmartwallet_cmd))
    app.add_handler(CommandHandler("removesmartwallet", removesmartwallet_cmd))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler("onramp", buy_cmd))
    app.add_handler(CommandHandler("cashout", buy_cmd))
    app.add_handler(CommandHandler("offramp", buy_cmd))
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
    app.add_handler(CommandHandler("tpladder", tpladder_cmd))
    app.add_handler(CommandHandler("trail", trail_cmd))
    app.add_handler(CommandHandler("stake", stake_cmd))
    app.add_handler(CommandHandler("lpguard", lpguard_cmd))
    app.add_handler(CommandHandler("buylimit", buylimit_cmd))
    app.add_handler(CommandHandler("limits", limits_cmd))
    app.add_handler(CommandHandler("cancellimit", cancellimit_cmd))
    app.add_handler(CommandHandler("livesell", livesell_cmd))
    app.add_handler(CommandHandler("livesellevm", livesellevm_cmd))
    app.add_handler(CommandHandler("treasury", treasury_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("ref", ref_cmd))
    app.add_handler(CommandHandler("referral", ref_cmd))
    app.add_handler(CommandHandler("claim", claim_cmd))
    app.add_handler(CommandHandler("snipe", snipe_cmd))
    app.add_handler(CommandHandler("snipes", snipes_cmd))
    app.add_handler(CommandHandler("cancelsnipe", cancelsnipe_cmd))
    app.add_handler(CommandHandler("launches", launches_cmd))
    app.add_handler(CommandHandler("chains", chains_cmd))
    app.add_handler(CommandHandler("quote", quote_cmd))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("bridge", bridge_cmd))
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
        jq.run_repeating(lp_watch_job, interval=40, first=70)
        jq.run_repeating(buy_limit_job, interval=35, first=80)
        jq.run_repeating(launch_feed_job, interval=LAUNCH_FEED_SECONDS, first=35)
        jq.run_repeating(native_pulse_job, interval=600, first=50)
    else:
        logger.warning("job-queue extra missing; commands still work, scanners off")

    logger.info("FERZAN starting")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
