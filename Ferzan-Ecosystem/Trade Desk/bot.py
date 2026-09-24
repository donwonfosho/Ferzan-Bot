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
import datetime as dt
import re
import time

import requests
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonWebApp, Update, WebAppInfo

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
import migration
import portfolio
import rugcheck
import signer
import sniper
import trading
import user_wallets
import withdraw
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


async def _notify_admins(bot, text: str) -> None:
    """Best-effort DM to every configured admin. Never raises -- a notify
    failure must not take down whatever job was reporting the problem."""
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.exception("admin notify failed for %s", admin_id)


def _auto_trading_killed() -> bool:
    """Global kill switch for TP-ladder rung sells, auto-buy-on-feed, and DCA
    scheduled buys. Defaults OFF (auto trading enabled) until an admin flips
    it. Does not touch manual /buy, /livesell, or single-target /tp, /sl,
    /trail exits."""
    return db.flag_on(0, "kill_auto_trading", default=0)


from trade_locks import user_lock as _user_lock


_WAIT = object()
BUSY_MSG = "⏳ You already have a trade running — wait for it to land, then tap again."


async def _off(uid: int, fn, *args, _busy=_WAIT, **kwargs):
    """Run a blocking trade call (quote + sign + broadcast) in a worker thread
    so one user's buy never freezes the bot for everyone else. Trades for the
    SAME user are serialized by a per-user lock (EVM nonce / Solana blockhash
    races).

    Background jobs (exits, TP ladder, copy, limits, DCA) leave _busy unset
    and WAIT their turn — a stop-loss must never be dropped. User taps pass
    _busy=<value to return>: if a trade is already running for this user the
    tap is refused instantly instead of queueing, so a double-tap can't buy
    twice (or sell 50% of the remainder) and a tap-spammer can't park dozens
    of worker threads."""
    lock = _user_lock(uid)
    if _busy is not _WAIT:
        if not lock.acquire(blocking=False):
            return _busy

        def run_held():
            try:
                return fn(*args, **kwargs)
            finally:
                lock.release()

        return await asyncio.to_thread(run_held)

    def run():
        with lock:
            return fn(*args, **kwargs)

    return await asyncio.to_thread(run)


async def _progress(bot, chat_id: int, text: str = "⏳ Sending…"):
    """Post an instant placeholder so a tap never feels dead; the caller
    edits it with the real result via _done()."""
    try:
        return await bot.send_message(chat_id, text)
    except Exception:
        return None


async def _done(bot, chat_id: int, placeholder, text: str, **kwargs) -> None:
    """Replace the placeholder with the result; fall back to a new message if
    the edit fails (message too old, identical text, markup mismatch...)."""
    text = text or "Done."
    if placeholder is not None:
        try:
            await placeholder.edit_text(text, **kwargs)
            return
        except Exception:
            pass
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except Exception:
        logger.exception("result send failed for %s", chat_id)


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


def _is_sol_mint(ca: str) -> bool:
    ca = (ca or "").strip()
    return (
        32 <= len(ca) <= 44
        and not ca.startswith("0x")
        and not ca.startswith(("EQ", "UQ", "kQ"))
        and not (ca.startswith("T") and len(ca) <= 36)
    )


def _safety_line(chain: str, ca: str) -> str:
    """Rug / honeypot summary for a card. Blocking (RPC / GoPlus)."""
    try:
        if ca.startswith("0x"):
            return _security_line(chain, ca)
        if _is_sol_mint(ca):
            return rugcheck.security_line(rugcheck.sol_report(ca))
    except Exception:
        logger.exception("safety line failed for %s", ca)
    return ""


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
    return _fit_html(_render_card(card, uid))


def _render_card(card: SignalCard, uid: int | None = None) -> str:
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
        f"⚡ <b>{_esc(_clip_plain(s.name, 40))}</b>  ${_esc(_clip_plain(str(s.symbol).lstrip('$'), 24))}  ·  {_esc(chain)}",
        f"<code>{_esc(ca)}</code>" if ca else "",
        " · ".join([x for x in [venue, (f"age {html.escape(age)}" if age else ""), curve] if x]),
        " · ".join(social) if social else "",
        (
            f"🧢 {_esc(f'${mc:,.0f}' if mc else '—')}"
            f"  💵 {_esc(_fmt_px(s.price_usd))}"
            f"  💧 {_esc(f'${liq:,.0f}' if liq else '—')}{_esc(liq_pct)}"
        ),
        _esc(_safety_line(s.chain or "", ca)),
        _card_wallet(uid, ca, s.chain or ""),
        f"📊 1h {s.buys_h1}/{s.sells_h1}  ·  24h {_esc(f'${vol:,.0f}' if vol else '—')}  ·  {card.score}/100 {_esc(card.bias)}",
        " · ".join(links),
    ]
    return "\n".join(lines)


def card_keyboard(
    query: str, score: int, ca: str = "", chain: str = "", uid: int | None = None
) -> InlineKeyboardMarkup:
    q = (ca or query)[:48]  # TON addresses are 48 chars; longest prefix keeps this < 64 bytes
    cid = resolve_chain(chain) or ("sol" if q and not str(q).startswith("0x") else "eth")
    unit = {
        "sol": "SOL", "bsc": "BNB", "eth": "ETH", "base": "ETH",
        "arb": "ETH", "avax": "AVAX", "pol": "POL", "hood": "ETH",
    }.get(cid, "ETH")
    presets = db.buy_presets(uid, cid)
    buy_btns = [
        InlineKeyboardButton(f"🟢 {v:g} {unit}", callback_data=f"bnv:{v:g}:{q}") for v in presets
    ]
    rows = [buy_btns[i : i + 3] for i in range(0, len(buy_btns), 3)]
    default_usd = _default_buy_usd(uid) if uid else 25.0
    rows += [
        [
            InlineKeyboardButton(f"✏️ Buy X {unit}", callback_data=f"buyx:{q}"),
            InlineKeyboardButton(f"💵 Buy ${default_usd:g}", callback_data=f"buyz:{default_usd:g}:{q}"),
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
            InlineKeyboardButton("⚙️ Presets", callback_data=f"pst:{cid}"),
        ],
        [
            InlineKeyboardButton("🎯 Snipe", callback_data=f"snp:{q}"),
            InlineKeyboardButton("⏳ Limit", callback_data=f"blm:{q}"),
            InlineKeyboardButton("🔔 Alert", callback_data=f"talt:{q}"),
        ],
        [
            InlineKeyboardButton("↔️ Sell", callback_data=f"slc:{q}"),
            InlineKeyboardButton("📍 Track", callback_data=f"watch:{q}"),
            InlineKeyboardButton("🔄 Refresh", callback_data=f"sig:{q}"),
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
        extra.append(InlineKeyboardButton("📋 CA", callback_data=f"sig:{addr[:48]}"))
    if extra:
        rows.append(extra)
    return InlineKeyboardMarkup(rows)


def sell_keyboard(
    query: str, ca: str = "", chain: str = "", uid: int | None = None, token_amt: float = 0.0
) -> InlineKeyboardMarkup:
    q = (ca or query)[:48]  # TON addresses are 48 chars; longest prefix keeps this < 64 bytes
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
            InlineKeyboardButton("⏳ Buy dip −20%", callback_data=f"blm:{q}"),
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
                InlineKeyboardButton("25%", callback_data=f"xsell:{p['id']}:25"),
                InlineKeyboardButton("50%", callback_data=f"xsell:{p['id']}:50"),
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


def home_keyboard(private: bool = True) -> InlineKeyboardMarkup:
    chat = (os.getenv("FERZAN_CHAT_URL") or "https://t.me/Ferzan_Chat").strip()
    xurl = (os.getenv("FERZAN_X_URL") or "https://x.com/ferzaneco").strip()
    # Telegram sizes a photo card's buttons to the photo, so a row of three
    # only fits short labels. Long labels (Settings, Migrations, Withdraw)
    # get rows of two so nothing is cut off with "…".
    rows = [
        [
            InlineKeyboardButton("⚙️ Settings", callback_data="go:settings"),
            InlineKeyboardButton("👛 Wallets", callback_data="go:wallets"),
        ],
        [
            InlineKeyboardButton("⛓ Chains", callback_data="go:chains"),
            InlineKeyboardButton("📊 Bag", callback_data="go:bag"),
            InlineKeyboardButton("📡 Signals", callback_data="go:feeds"),
        ],
        [
            InlineKeyboardButton("🎯 Snipe", callback_data="go:snipehelp"),
            InlineKeyboardButton("⏱ Limits", callback_data="go:snipes"),
            InlineKeyboardButton("👯 Copy", callback_data="go:copy"),
        ],
        [
            InlineKeyboardButton("🎓 Migrations", callback_data="go:mig"),
            InlineKeyboardButton("📤 Withdraw", callback_data="go:withdraw"),
        ],
        [
            InlineKeyboardButton("🔔 Alerts", callback_data="go:alerts"),
            InlineKeyboardButton("🌉 Bridge", callback_data="go:bridge"),
            InlineKeyboardButton("🚀 Launch", callback_data="go:launches"),
        ],
        [
            InlineKeyboardButton("🤝 Refer", callback_data="go:ref"),
            InlineKeyboardButton("💬 Chat", url=chat),
            InlineKeyboardButton("𝕏 X", url=xurl),
        ],
        [InlineKeyboardButton("⚡ PASTE CA", callback_data="go:buyhelp")],
    ]
    app_url = _webapp_url()
    if app_url and private:  # Telegram rejects web_app buttons outside private chats
        rows.insert(0, [InlineKeyboardButton("📱 Open Ferzan app", web_app=WebAppInfo(url=app_url))])
    return InlineKeyboardMarkup(rows)


def _webapp_url() -> str:
    """Mini App URL (must be https). Empty = app buttons hidden."""
    url = (os.getenv("FERZAN_WEBAPP_URL") or "").strip()
    return url if url.startswith("https://") else ""


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
                mine = db.get_user(me) or {}
                # First link wins, and a link can't make a loop (A->B->A).
                if not mine.get("referred_by") and not db.would_create_ref_loop(me, rid):
                    db.update_user(me, referred_by=rid, discount_until=int(time.time()) + 30 * 86400)
        except (TypeError, ValueError):
            pass
    if extra and extra.startswith("sig_"):
        await _send_signal(update, extra[4:], edit=False)
        return
    if extra and extra.startswith("sell_"):
        # Mini App "Sell in bot" -> the /bag sell panel for that token (any chain).
        mint = extra[5:]
        uid = update.effective_user.id
        try:
            amount, owner, venue = await asyncio.to_thread(_bag_position_amount, uid, mint)
            text, kb = await asyncio.to_thread(_bag_panel, mint, amount, owner, uid, None, venue)
            await update.effective_message.reply_text(
                text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
            )
        except Exception as exc:
            await update.effective_message.reply_text(f"Couldn't open that bag: {exc}")
        return
    if extra == "wallets":
        text, kb = _mywallets_panel(update.effective_user.id)
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        return
    if extra and extra.startswith("buy_"):
        await _send_signal(update, extra[4:], edit=False)
        if update.effective_message:
            await update.effective_message.reply_text(
                "Buy desk. Tap 0.01 / 0.05 / $ on the card. Spends YOUR Ferzan wallet."
            )
        return
    first_time = not db.flag_on(update.effective_user.id, "onboarded", 0) and not db.get_user_wallet(
        update.effective_user.id
    )
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
            "⚙️ Settings: slip, size, gas, anti-MEV.\n"
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
                    reply_markup=home_keyboard(private=bool(update.effective_chat and update.effective_chat.type == "private")),
                )
        else:
            await target.reply_text(
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=home_keyboard(private=bool(update.effective_chat and update.effective_chat.type == "private")),
            )
        try:
            user_wallets.ensure(update.effective_user.id)
        except Exception:
            logger.exception("wallet ensure on start failed")
        if first_time:
            await _tour_step1(context.bot, update.effective_user.id)
    except Exception:
        logger.exception("start failed")
        try:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "FERZAN is up. Desk hit a snag. Try /wallet — do not Redeploy yet."
                )
        except Exception:
            logger.exception("start fallback failed")


# ---- first-run tour (/start for new users, /tour any time) ------------------
TOUR_DEMO_MINT = os.getenv("FERZAN_TOUR_DEMO_MINT", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")  # BONK


async def _tour_step1(bot, uid: int) -> None:
    row = await asyncio.to_thread(user_wallets.ensure, uid)
    text = (
        "👋 <b>Quick tour · 1/3 — your wallet is ready</b>\n\n"
        "Ferzan made you a trading wallet. Keys stay encrypted on the desk; export any time in /wallet.\n\n"
        f"🟣 <b>Solana</b> (send SOL)\n<code>{html.escape(row.get('sol_pub', ''))}</code>\n\n"
        f"🔵 <b>EVM</b> — ETH · Base · BNB · Arb… (send that chain's gas coin)\n"
        f"<code>{html.escape(row.get('evm_pub', ''))}</code>\n\n"
        "Tap an address to copy it, send a little from your exchange or wallet, then tap below."
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ I sent funds — check", callback_data="tour:bal")],
            [InlineKeyboardButton("⏭ Skip — show me how to trade", callback_data="tour:trade")],
        ]
    )
    await bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)


def _tour_balances(uid: int) -> tuple[float, float]:
    row = db.get_user_wallet(uid) or {}
    sol = eth = 0.0
    try:
        sol = signer.sol_balance_lamports(row.get("sol_pub", "")) / 1e9
    except Exception:
        pass
    try:
        eth, _sym = evm_signer.native_balance("base", row.get("evm_pub", ""))
    except Exception:
        pass
    return sol, float(eth or 0)


async def _tour_step2(query, uid: int) -> None:
    sol, eth = await asyncio.to_thread(_tour_balances, uid)
    if sol <= 0 and eth <= 0:
        text = (
            "👋 <b>Quick tour · 2/3 — waiting for your deposit</b>\n\n"
            "Nothing has landed yet. Transfers usually arrive in under a minute "
            "(exchange withdrawals can take longer).\n"
            "Tap again once it's sent."
        )
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Check again", callback_data="tour:bal")],
                [InlineKeyboardButton("⏭ Skip — show me how to trade", callback_data="tour:trade")],
            ]
        )
    else:
        text = (
            "👋 <b>Quick tour · 2/3 — funded ✅</b>\n\n"
            f"🟣 {sol:.4f} SOL   🔵 {eth:.5f} ETH (Base)\n\n"
            "You're ready to trade."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Show me how to trade", callback_data="tour:trade")]])
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def _tour_step3(query, uid: int) -> None:
    user = db.get_user(uid) or {}
    size = float(user.get("buy_usd") or 25)
    text = (
        "👋 <b>Quick tour · 3/3 — your first trade</b>\n\n"
        "1️⃣ <b>Paste any token address (CA)</b> in this chat — Solana, EVM, TON, TRON.\n"
        "2️⃣ Ferzan scores it and shows a buy card. Tap a size to buy.\n"
        "3️⃣ Sell from 📊 Bag: 25 / 50 / 100% in one tap.\n\n"
        "🛡 <b>Protection is on by default</b>\n"
        "• Blocks buys when liquidity is thin or the token is a honeypot\n"
        + ("• Anti-MEV: paused for maintenance — buys use the fast normal route for now\n"
           if signer.anti_mev_paused() else
           "• Anti-MEV: Solana buys go private via Jito (no sandwiches, no fee if it fails)\n")
        + "• A trade only says ✅ once it's confirmed on-chain\n\n"
        f"💵 Default buy size: <b>${size:.0f}</b> — change it in ⚙️ Settings.\n\n"
        "Try it now 👇"
    )
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Score a real token (BONK)", callback_data=f"sig:{TOUR_DEMO_MINT}")],
            [
                InlineKeyboardButton("⚙️ Desk settings", callback_data="go:settings"),
                InlineKeyboardButton("🏠 Home", callback_data="go:home"),
            ],
        ]
    )
    db.set_flag(uid, "onboarded", True)
    try:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await query.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def tour_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    await _tour_step1(context.bot, update.effective_user.id)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    lines = [
        "FERZAN TRADE DESK — commands\n",
        "Paste a token address (CA) anytime to score it and get a buy card.",
        "Pump.fun: paste the mint from DexScreener (usually ends in \"pump\").",
        "Live spend is YOUR /wallet bag, not a shared treasury.\n",

        "💼 <b>Wallet</b>",
        "/wallet — generate / import / view your chain addresses",
        "/importsol /importevm — import a key (private chat only)",
        "/bag — live bag, PnL, and sell buttons",
        "/positions — same as /bag\n",

        "🛒 <b>Trading</b>",
        "/buy &lt;CA&gt; — score + buy card for a token",
        "/livesell &lt;mint&gt; — sell a Solana token",
        "/livesellevm &lt;chain&gt; &lt;0x...&gt; — sell an EVM token",
        "/quote &lt;chain&gt; &lt;CA&gt; — get a swap quote without trading",
        "/chains — pick which network you're working on",
        "/snipe &lt;CA&gt; — arm a live snipe on a new pool (capped size)",
        "/buylimit &lt;CA&gt; &lt;price&gt; &lt;slippage&gt; — buy automatically when price hits a target",
        "/limits — list your active buy limits\n",

        "🎯 <b>Automated exits</b> (all opt-in — nothing sells unless you set it)",
        "/tp 50 — auto-sell 100% when you're up 50%",
        "/sl 30 — auto-sell 100% when you're down 30%",
        "/trail 20 — sell everything if the bag's value falls 20% from its highest point since you set it",
        "💰 Sell initials (bag panel) — sell just enough to get your money back out",
        "/tpladder 50:25 100:25 200:50 — sell in stages: 25% of your bag at +50%, "
        "another 25% at +100%, the rest at +200% (add \"off &lt;mint&gt;\" to cancel)",
        "These stack — /tp, /sl, /trail, and /tpladder can all be armed on the same "
        "position at once, whichever triggers first fires.\n",

        "📅 <b>Automated buying</b> (opt-in — nothing buys unless you set it)",
        "/dca &lt;CA&gt; &lt;$amount&gt; &lt;hourly|daily|weekly&gt; — buy a fixed $ amount "
        "on a repeating schedule (dollar-cost averaging)",
        "/dca — list your active plans; /dca off &lt;CA&gt; to cancel one\n",

        "📡 <b>Signals &amp; feeds</b>",
        "/signal &lt;CA&gt; — score a token (rug/honeypot/liquidity checks)",
        "/launches — browse new pools",
        "/feeds — turn launch alerts on/off per chain in a group",
        "/settings — default buy size, price floor, slippage protection",
        "/alert &lt;CA&gt; 2m — ping when a token hits a market cap (or +50%, -30%, price 0.001)",
        "/alerts — your token alerts",
        "/alert &lt;symbol&gt; &lt;above|below&gt; &lt;price&gt; — big-coin price alert (e.g. sol above 200)",
        "/list — your active price alerts",
        "/migsnipe — pump.fun graduation sniper (alerts or auto-buy)",
        "/presets — your one-tap buy and sell amounts",
        "/recap — your day at a glance (also sent every morning)\n",
        "📤 <b>Moving money</b>",
        "/withdraw — send SOL, ETH, BNB, TON or any token out, with confirm",
        "/addressbook — saved withdrawal addresses",
        "/price &lt;symbol&gt; — current price\n",

        "🐋 <b>Following other wallets</b>",
        "/watchwallet &lt;chain&gt; &lt;address&gt; — get a DM (with a one-tap buy) when that wallet trades (up to 20)",
        "/smartmoney — browse the curated smart-money wallet list and follow one\n",

        "🤝 <b>Referrals</b>",
        "/referral — your invite link, 3 levels of earnings",
        "/claim — cash out once your claimable share hits $5\n",

        "/help — this list",
    ]
    if _is_admin(update.effective_user.id):
        lines += [
            "\n🔧 <b>Admin only</b>",
            "/addsmartwallet &lt;chain&gt; &lt;address&gt; &lt;label&gt; — add to the curated smart-money list",
            "/removesmartwallet &lt;id&gt; — remove one (no id = lists all with their ids)",
            "/killswitch on|off — instantly stop TP-ladder sells, auto-buy-on-feed, and DCA buys bot-wide",
        ]
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML", disable_web_page_preview=True
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
    args = context.args or []
    if args and len(args[0]) >= 32:  # a contract address -> token alert
        if len(args) < 2:
            await update.effective_message.reply_text(
                "Usage: /alert <CA> 2m   (market cap)\n/alert <CA> +50%   (move)\n/alert <CA> price 0.0012"
            )
            return
        await _create_token_alert(update, update.effective_user.id, args[0], " ".join(args[1:]))
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
    placeholder = None
    if not (edit and update.callback_query):
        try:
            placeholder = await update.effective_message.reply_text("🔎 Scanning…")
        except Exception:
            placeholder = None

    async def _out(text: str, **kwargs) -> None:
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text, **kwargs)
            return
        if placeholder is not None:
            try:
                await placeholder.edit_text(text, **kwargs)
                return
            except Exception:
                pass
        await update.effective_message.reply_text(text, **kwargs)

    try:
        card = await asyncio.to_thread(analyze, query)
    except PriceFetchError as exc:
        await _out(f"Could not score {query}: {exc}")
        return
    uid = update.effective_user.id if update.effective_user else None
    text = await asyncio.to_thread(render_card, card, uid)
    markup = card_keyboard(
        card.snapshot.query or query,
        card.score,
        ca=card.snapshot.token_address or "",
        chain=card.snapshot.chain or "",
        uid=uid,
    )
    await _out(text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup)


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
    if db.flag_on(uid, "honeypot", 1) and _is_sol_mint(mint):
        try:
            why = rugcheck.block_reason(rugcheck.sol_report(mint))
        except Exception:
            why = ""
        if why:
            return f"🛡 Honeypot guard ON: {why}. Live buy blocked. (/settings to turn the guard off)"
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


def _live_buy(
    uid: int, card, query: str, force: bool, usd_override: float | None = None
) -> tuple[bool, str]:
    """Blocking — call via _off(). Returns (ok, message). Guard refusals
    return their original text (other code matches on those prefixes);
    actual sends come back in the shared _trade_result layout."""
    if not signer.live_enabled():
        return False, "Live buys are off. LIVE_BUYS=0 on the server."
    if db.flag_on(uid, "score_gate", 0) and not force:
        floor = int((db.get_user(uid) or {}).get("min_confluence") or 0)
        if getattr(card, "score", 100) < floor:
            return False, f"Blocked by your score floor ({card.score} < {floor}). /settings floor or tap Override."
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
        return False, "Live: no mint on this card. Paste the full CA, then Buy."
    blocked = _rug_block(uid, card, mint)
    if blocked:
        return False, blocked
    usd = _default_buy_usd(uid)
    if usd_override is not None:
        usd = min(signer.max_usd(), max(1.0, float(usd_override)))
    try:
        sol_secret, evm_secret = user_wallets.secrets(uid)
    except Exception as exc:
        return False, f"Live: open /wallet first.\n{exc}"
    liq_mark = True
    if chain in {"trx", "tron"} or (mint.startswith("T") and 30 <= len(mint) <= 36):
        import tron_signer

        label, liq_mark = "TRX", False
        ok, msg = tron_signer.buy_tron(mint, usd, key_hex=evm_secret)
    elif chain in {"ton"} or mint.startswith(("EQ", "UQ", "kQ")):
        import ton_signer

        label, liq_mark = "TON", False
        ok, msg = ton_signer.buy_ton(mint, usd, secret=sol_secret)
    elif mint.startswith("0x"):
        if not (os.getenv("ZEROX_API_KEY") or "").strip():
            return False, "Live: EVM needs ZEROX_API_KEY on the droplet."
        label = (resolve_chain(chain) or chain or "base").upper()
        ok, msg = evm_signer.buy_evm(
            chain or "base", mint, usd, key_hex=evm_secret, slip_bps=_slip_bps(uid, "buy"), user_id=uid
        )
    else:
        if "sol" not in chain and not (len(mint) >= 32 and not mint.startswith("0x")):
            return False, f"Live: {chain or 'unknown'} is not Solana."
        label = "SOL"
        ok, msg = signer.buy_sol(mint, usd, secret=sol_secret, slip_bps=_slip_bps(uid, "buy"), user_id=uid)
    if ok:
        db.add_live_cost(uid, mint, usd)
        _log_trade_safe(uid, "buy", mint, label, usd)
        if liq_mark:
            db.set_lp_mark(uid, mint, float(card.snapshot.liquidity_usd or 0))
        extra = db.credit_desk_share(uid, usd)
        if extra:
            msg = f"{msg}\n{extra}"
    return bool(ok), _trade_result("buy", bool(ok), label, msg, usd=usd)


def _live_buy_followup(
    uid: int, card, query: str, paper_ok: bool, force: bool, usd_override: float | None = None
) -> str:
    return _live_buy(uid, card, query, force, usd_override)[1]


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


_EVM_SCAN = ("eth", "base", "bsc", "hood", "arb", "avax")


def _chain_of_mint(uid: int, mint: str, evm_addr: str | None = None) -> str:
    """Best-effort chain id for a token address (blocking — call via _off)."""
    if mint.startswith("T") and 30 <= len(mint) <= 36:
        return "trx"
    if mint.startswith(("EQ", "UQ", "kQ")):
        return "ton"
    if mint.startswith("0x"):
        evm_addr = evm_addr or (db.get_user_wallet(uid) or {}).get("evm_pub") or ""
        for cid in _EVM_SCAN:
            try:
                raw = evm_signer._erc20_balance(CHAINS[cid]["rpc"], mint, evm_addr)
            except Exception:
                raw = 0
            if raw > 0:
                return cid
        return "base"
    return "sol"


def _holder_wallet(uid: int, mint: str) -> tuple[str, str, str, str]:
    """(sol_secret, evm_secret, wallet_label, chain_id) for the wallet that
    actually holds `mint`: the ACTIVE wallet first, then the user's others.
    So a stop-loss / TP / manual sell still works after the user switches
    wallets. TRON/TON only check the active wallet. Blocking."""
    wallets = user_wallets.all_secrets(uid)
    act_sol, act_evm = wallets[0][2], wallets[0][3]
    act_label = wallets[0][1]
    if mint.startswith("0x"):
        from eth_account import Account

        for _sid, lab, sol, evm in wallets:
            try:
                addr = Account.from_key(evm if evm.startswith("0x") else "0x" + evm).address
            except Exception:
                continue
            for cid in _EVM_SCAN:
                try:
                    if evm_signer._erc20_balance(CHAINS[cid]["rpc"], mint, addr) > 0:
                        return sol, evm, lab, cid
                except Exception:
                    continue
        return act_sol, act_evm, act_label, "base"
    cid = _chain_of_mint(uid, mint)
    if cid == "sol" and len(wallets) > 1:
        for _sid, lab, sol, evm in wallets:
            try:
                if signer._token_raw_balance(mint, signer.keypair_from_secret(sol)) > 0:
                    return sol, evm, lab, "sol"
            except Exception:
                continue
    return act_sol, act_evm, act_label, cid


def _sell_any(uid: int, mint: str, pct: int = 100) -> tuple[bool, str, str]:
    """One sell path for every chain. Blocking — call via _off().
    Returns (ok, message, chain_label)."""
    pct = max(1, min(100, int(pct)))
    sol_secret, evm_secret, wlabel, cid = _holder_wallet(uid, mint)
    label = cid.upper()
    if len(user_wallets.all_secrets(uid)) > 1:
        label = f"{label} · {wlabel}"
    if cid == "trx":
        if pct < 100:
            return False, "TRON sells are full-bag only right now — tap 100%.", label
        import tron_signer

        ok, msg = tron_signer.sell_tron(mint, key_hex=evm_secret)
    elif cid == "ton":
        import ton_signer

        slip = f"{max(1, _slip_bps(uid, 'sell', 'ton')) / 10000:.4f}"
        ok, msg = ton_signer.sell_ton(mint, secret=sol_secret, pct=pct, slip=slip)
    elif cid == "sol":
        ok, msg = signer.sell_sol(mint, secret=sol_secret, pct=pct, slip_bps=_slip_bps(uid, "sell"), user_id=uid)
    else:
        ok, msg = evm_signer.sell_evm(cid, mint, key_hex=evm_secret, pct=pct)
    if ok:
        _log_trade_safe(uid, "sell", mint, cid, 0.0)
        try:
            if pct >= 100:
                db.clear_live_cost(uid, mint)
            else:
                db.reduce_live_cost_pct(uid, mint, pct)
        except Exception:
            logger.exception("cost-basis update failed after sell")
    return bool(ok), msg, label


def _log_trade_safe(uid: int, side: str, mint: str, chain: str, usd: float, source: str = "") -> None:
    try:
        db.log_trade(uid, side, mint, chain, usd, source)
    except Exception:
        logger.exception("trade log failed")


def _trade_result(side: str, ok: bool, chain_label: str, msg: str, *, usd: float | None = None, pct: int | None = None) -> str:
    """Same confirmation layout for every chain and every entry point."""
    icon = "🟢" if ok else "🔴"
    if side == "buy":
        verb = "Bought" if ok else "Buy failed"
        size = f" ${usd:,.2f}" if usd else ""
    else:
        verb = "Sold" if ok else "Sell failed"
        size = f" {pct}%" if pct else ""
    head = f"{icon} {verb}{size} · {chain_label}"
    body = (msg or "").strip()
    return f"{head}\n{body}" if body else head


def _live_sell_position(uid: int, pos_id: int, pct: int = 100, only_if_live: bool = False) -> str:
    """Live-sell the token behind a position ticket from the USER's wallet.
    (Previously called signer.sell_sol without secret=, which signs with the
    server's SIGNER_KEY wallet — never do that for a user action.)
    only_if_live: skip unless this bot actually bought the token live for
    this user — so closing a PAPER trade never dumps a real bag by surprise."""
    pos = db.get_position(pos_id, uid)
    if not pos:
        return "Live sell: no position."
    mint = _mint_from_position(pos)
    if not mint:
        return "Live sell: no Solana mint on this ticket. Paste the CA and sell from the card."
    if only_if_live and db.live_cost(uid, mint) <= 0:
        return ""
    ok, msg, label = _sell_any(uid, mint, pct)
    return _trade_result("sell", ok, label, msg, pct=pct)


async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /buy sol")
        return
    query = " ".join(context.args)
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    status = await _progress(context.bot, chat_id, "⏳ Buying…")
    try:
        card = await asyncio.to_thread(analyze, query)
    except PriceFetchError as exc:
        await _done(context.bot, chat_id, status, str(exc))
        return
    live_msg = await _off(uid, _live_buy_followup, uid, card, query, True, False, _busy=BUSY_MSG)
    await _done(context.bot, chat_id, status, live_msg or "Buy sent.")


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


def _bag_panel(
    mint: str, amount: float, addr: str, uid: int, meta: dict | None = None, venue_override: str = ""
) -> tuple[str, InlineKeyboardMarkup]:
    short = mint[:48]  # TON addresses are 48 chars
    meta = meta if meta is not None else _token_meta(mint)
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
    elif mint.startswith(("EQ", "UQ", "kQ")):
        href = f"https://tonviewer.com/{mint}"
        venue = "TON"
    else:
        href = f"https://solscan.io/token/{mint}"
        venue = "SOL"
    if venue_override:
        venue = venue_override
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
    ex = db.get_live_exit(uid, mint) or {}
    rungs = [r for r in db.list_tp_rungs(uid, mint) if not r.get("hit")]
    bits = []
    if ex.get("tp_pct"):
        bits.append(f"🎯 TP +{float(ex['tp_pct']):.0f}%")
    if ex.get("sl_pct"):
        bits.append(f"🛑 SL -{float(ex['sl_pct']):.0f}%")
    if ex.get("trail_pct"):
        peak_px = ex.get("peak_px")
        stop = f" (sells under {_fmt_px(float(peak_px) * (1 - float(ex['trail_pct']) / 100))})" if peak_px else ""
        bits.append(f"📉 Trail {float(ex['trail_pct']):.0f}%{stop}")
    if rungs:
        bits.append(f"🪜 {len(rungs)} TP rung{'s' if len(rungs) != 1 else ''}")
    if bits:
        text += "Armed: " + " · ".join(bits) + "\n"
    text += "<i>Tap CA to copy</i>"
    sell_row = [
        InlineKeyboardButton(f"{v}%", callback_data=f"slp:{v}:{short}") for v in db.sell_presets(uid) if v < 100
    ]
    sell_row.append(InlineKeyboardButton("☢️ 100%", callback_data=f"slp:100:{short}"))
    rows = [sell_row]
    if cost > 0 and worth > cost:
        rows.append([InlineKeyboardButton("💰 Sell initials (take my money out)", callback_data=f"sli:{short}")])
    rows += [
        [
            InlineKeyboardButton("🎯 TP +50%", callback_data=f"tpx:50:{short}"),
            InlineKeyboardButton("🛑 SL -30%", callback_data=f"slx:30:{short}"),
            InlineKeyboardButton("📉 Trail 20%", callback_data=f"trl:20:{short}"),
        ],
    ]
    if bits:
        rows.append([InlineKeyboardButton("🧹 Clear exit rules", callback_data=f"exc:{short}")])
    rows += [
        [
            InlineKeyboardButton("📡 Score", callback_data=f"sig:{short}"),
            InlineKeyboardButton("💵 Buy more", callback_data=f"buy:{short}"),
            InlineKeyboardButton("🔔 Alert", callback_data=f"talt:{short}"),
        ],
        [
            InlineKeyboardButton("🔄 Refresh", callback_data=f"bagr:{short}"),
            InlineKeyboardButton("📸 PnL card", callback_data=f"pnlc:{short}"),
        ],
    ]
    return text, InlineKeyboardMarkup(rows)


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


_DCA_INTERVALS = {"hourly": 3600, "daily": 86400, "weekly": 604800}


def _dca_label(seconds: int) -> str:
    for label, secs in _DCA_INTERVALS.items():
        if secs == seconds:
            return label
    return f"{seconds}s"


def _fmt_countdown(seconds: int) -> str:
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


async def dca_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    args = list(context.args or [])
    if not args:
        plans = db.list_dca_plans(uid)
        if not plans:
            await update.effective_message.reply_text(
                "Usage: /dca &lt;CA&gt; &lt;$amount&gt; &lt;hourly|daily|weekly&gt;\n"
                "Example: /dca 7xKX... 25 daily — buys $25 of that token every day.\n\n"
                "/dca off &lt;CA&gt; — cancel a plan.\n"
                "/dca with no args — list your active plans.\n\n"
                "Each scheduled buy still runs through your normal safety checks "
                "(score floor, rug/honeypot). If your wallet doesn't have enough "
                "for a scheduled buy, you'll get a DM saying so instead of it "
                "silently failing — top up and the next cycle will go through.",
                parse_mode="HTML",
            )
            return
        now = int(time.time())
        lines = ["📅 <b>Active DCA plans</b>"]
        for p in plans:
            next_in = max(0, int(p["next_run_at"]) - now)
            lines.append(
                f"<code>{html.escape(str(p['mint'])[:10])}...</code> — "
                f"${float(p['usd_per_buy']):.0f} every {_dca_label(int(p['interval_seconds']))}, "
                f"next in {_fmt_countdown(next_in)}"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")
        return
    if args[0].lower() == "off":
        if len(args) < 2:
            await update.effective_message.reply_text("Usage: /dca off <CA>")
            return
        ok = db.clear_dca_plan(uid, args[1].strip())
        await update.effective_message.reply_text(
            "📅 DCA plan cancelled." if ok else "No active DCA plan for that token."
        )
        return
    if len(args) < 3:
        await update.effective_message.reply_text("Usage: /dca <CA> <$amount> <hourly|daily|weekly>")
        return
    mint = args[0].strip()
    try:
        usd = float(args[1])
        if usd <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Amount must be a positive number.")
        return
    interval_key = args[2].lower()
    interval_s = _DCA_INTERVALS.get(interval_key)
    if not interval_s:
        await update.effective_message.reply_text("Frequency must be hourly, daily, or weekly.")
        return
    chain = "base" if mint.startswith("0x") else "solana"
    db.set_dca_plan(uid, mint, chain, usd, interval_s)
    await update.effective_message.reply_text(
        f"📅 DCA armed: ${usd:.0f} every {interval_key}.\n"
        f"First buy in {_fmt_countdown(interval_s)}. /dca off {mint} to cancel anytime."
    )


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
    if not 1 <= pct <= 90:
        await update.effective_message.reply_text("Pick a trail between 1 and 90 (%).")
        return
    uid = update.effective_user.id
    db.set_live_exit(uid, mint, trail_pct=pct)
    db.reset_live_peak(uid, mint)
    await update.effective_message.reply_text(
        f"📉 Trailing stop armed: sells everything if the bag's value falls {pct:.0f}% "
        "from its highest point from now on. The high only ratchets up."
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


def _bag_position_amount(uid: int, mint: str) -> tuple[float, str, str]:
    """(token amount, wallet address, venue label) for one mint. Blocking."""
    sol_secret, _evm = user_wallets.secrets(uid)
    if mint.startswith(("EQ", "UQ", "kQ")):
        import ton_signer

        amount, owner = ton_signer.jetton_holding(sol_secret, mint)
        return amount, owner, "TON"
    if mint.startswith("0x"):
        evm_addr = (db.get_user_wallet(uid) or {}).get("evm_pub") or ""
        try:
            held = _exit_holdings(uid, mint)  # real decimals, every wallet
        except Exception:
            held = []
        if held:
            return sum(h[3] for h in held), evm_addr, held[0][2].upper()
        return 0.0, evm_addr, "EVM"
    addr = str(signer.keypair_from_secret(sol_secret).pubkey())
    for row in signer.holdings(sol_secret):
        if row.get("mint") == mint:
            return float(row.get("amount") or 0), addr, "SOL"
    return 0.0, addr, "SOL"


def _bag_build(uid: int) -> tuple[str, list[tuple[str, InlineKeyboardMarkup]]]:
    """Everything /bag needs, fetched in one blocking pass (run via to_thread).
    Each token is priced once and the same mark feeds both the portfolio total
    and its panel."""
    sol_secret, _evm = user_wallets.secrets(uid)
    kp = signer.keypair_from_secret(sol_secret)
    addr = str(kp.pubkey())
    rows = signer.holdings(sol_secret)
    lamports = signer.sol_balance_lamports(addr)
    positions: list[tuple[str, float, str, str]] = [
        (r["mint"], float(r["amount"] or 0), addr, "") for r in rows[:6]
    ]
    evm_addr = (db.get_user_wallet(uid) or {}).get("evm_pub") or ""
    if evm_addr:
        for mint in db.live_mints(uid):
            if not str(mint).startswith("0x"):
                continue
            for cid in _EVM_SCAN:
                try:
                    raw = evm_signer._erc20_balance(CHAINS[cid]["rpc"], mint, evm_addr)
                except Exception:
                    raw = 0
                if raw > 0:
                    try:
                        amt = raw / 10 ** _erc20_decimals(cid, mint)
                    except Exception:
                        break  # unknown decimals: skip rather than show a wrong value
                    positions.append((mint, amt, evm_addr, cid.upper()))
                    break
    for mint in db.live_mints(uid):
        if not str(mint).startswith(("EQ", "UQ", "kQ")):
            continue
        try:
            amount, owner, venue = _bag_position_amount(uid, mint)
        except Exception:
            logger.exception("TON bag lookup failed for %s", mint)
            continue
        if amount > 0:
            positions.append((mint, amount, owner, venue))
    total_worth = total_cost = 0.0
    priced = 0
    panels: list[tuple[str, InlineKeyboardMarkup]] = []
    for mint, amount, owner, venue in positions:
        meta = _token_meta(mint)
        worth = amount * float(meta.get("px") or 0)
        cost = db.live_cost(uid, mint)
        if worth > 0:
            total_worth += worth
            if cost > 0:
                total_cost += cost
                priced += 1
        panels.append(_bag_panel(mint, amount, owner, uid, meta=meta, venue_override=venue))
    summary = f"🎒 <b>Wallet positions</b> · SOL\n💰 {lamports / 1e9:.6f} SOL\n<code>{html.escape(addr)}</code>"
    if priced > 0:
        total_pnl = total_worth - total_cost
        total_pct = (total_pnl / total_cost) * 100 if total_cost > 0 else 0.0
        mark = "🟢" if total_pnl >= 0 else "🔴"
        summary += (
            f"\n\n{mark} <b>Portfolio PnL {total_pnl:+,.2f} USD ({total_pct:+.1f}%)</b>\n"
            f"📥 Cost ${total_cost:,.2f}   💰 Worth ${total_worth:,.2f}"
        )
        if priced < len(positions):
            summary += "\n<i>Only counts positions bought live through Ferzan.</i>"
    if not positions:
        summary += "\n\nNo tokens yet. Paste a CA to buy."
    return summary, panels


def _is_bag_panel(message) -> bool:
    try:
        rows = message.reply_markup.inline_keyboard
    except Exception:
        return False
    return any(
        str(getattr(b, "callback_data", "") or "").startswith("bagr:")
        for row in rows
        for b in row
    )


async def _refresh_bag_panel(query, uid: int, mint: str, quiet: bool = False) -> None:
    """Re-render one /bag panel in place with fresh balance + mark."""
    if quiet:
        # Give the chain a moment to reflect the sell before re-reading balance.
        await asyncio.sleep(4)
    try:
        amount, owner, venue = await asyncio.to_thread(_bag_position_amount, uid, mint)
        text, kb = await asyncio.to_thread(_bag_panel, mint, amount, owner, uid, None, venue)
        stamp = time.strftime("%H:%M:%S", time.gmtime())
        await query.edit_message_text(
            f"{text}\n<i>Updated {stamp} UTC</i>",
            parse_mode="HTML",
            reply_markup=kb,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        if quiet or "not modified" in str(exc).lower():
            return
        try:
            await query.message.reply_text(f"Refresh failed: {exc}")
        except Exception:
            pass


def _pnl_card_bytes(uid: int, mint: str) -> tuple[bytes | None, str]:
    """Blocking. Returns (png, caption) or (None, reason)."""
    cost = db.live_cost(uid, mint)
    if cost <= 0:
        return None, "📸 PnL cards are for positions bought live through Ferzan (no cost basis on this one)."
    amount, _owner, venue = _bag_position_amount(uid, mint)
    meta = _token_meta(mint)
    worth = amount * float(meta.get("px") or 0)
    if worth <= 0:
        return None, "📸 No live mark for this token right now — try again in a minute."
    import pnl_card

    bot_name = os.getenv("FERZAN_BOT_USERNAME", "").strip()
    footer = f"Trade on Ferzan  ·  t.me/{bot_name}?start=ref_{uid}" if bot_name else "Trade on Ferzan"
    symbol = meta.get("symbol") or ""
    png = pnl_card.render(symbol=symbol, chain=venue, cost_usd=cost, worth_usd=worth, footer=footer)
    pnl = worth - cost
    caption = f"${symbol.upper() or 'TOKEN'} · {pnl:+,.2f} USD ({pnl / cost * 100:+.1f}%)"
    if bot_name:
        caption += f"\nTrade with me on Ferzan: https://t.me/{bot_name}?start=ref_{uid}"
    return png, caption


async def _send_pnl_card(bot, uid: int, mint: str) -> None:
    status = await _progress(bot, uid, "📸 Rendering your PnL card…")
    try:
        png, caption = await asyncio.to_thread(_pnl_card_bytes, uid, mint)
    except Exception as exc:
        logger.exception("pnl card failed")
        await _done(bot, uid, status, f"📸 Couldn't render the card: {exc}")
        return
    if png is None:
        await _done(bot, uid, status, caption)
        return
    import io

    try:
        await bot.send_photo(uid, photo=io.BytesIO(png), caption=caption)
        if status is not None:
            await status.delete()
    except Exception as exc:
        await _done(bot, uid, status, f"📸 Couldn't send the card: {exc}")


async def bag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    status = await _progress(context.bot, chat_id, "🎒 Loading your bag…")
    try:
        summary, panels = await asyncio.to_thread(_bag_build, uid)
    except Exception as exc:
        await _done(context.bot, chat_id, status, str(exc))
        return
    await _done(context.bot, chat_id, status, summary, parse_mode="HTML")
    for text, kb in panels:
        await update.effective_message.reply_text(
            text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )


async def livesell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /livesell <solana-mint>\nSee /bag")
        return
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    status = await _progress(context.bot, chat_id, "⏳ Selling…")
    sol_secret, _evm = user_wallets.secrets(uid)
    ok, msg = await _off(
        uid,
        signer.sell_sol,
        context.args[0].strip(),
        secret=sol_secret,
        slip_bps=_slip_bps(uid, "sell"),
        user_id=uid,
        _busy=(False, BUSY_MSG),
    )
    if ok:
        db.clear_live_cost(uid, context.args[0].strip())
    await _done(context.bot, chat_id, status, _trade_result("sell", ok, "SOL", msg, pct=100))


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
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    status = await _progress(context.bot, chat_id, "⏳ Selling…")
    _sol, evm_secret = user_wallets.secrets(uid)
    ok, msg = await _off(uid, evm_signer.sell_evm, chain, token, key_hex=evm_secret, _busy=(False, BUSY_MSG))
    if ok:
        db.clear_live_cost(uid, token)
    await _done(
        context.bot, chat_id, status,
        _trade_result("sell", ok, (resolve_chain(chain) or chain).upper(), msg, pct=100),
    )


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
    uid = update.effective_user.id
    live = await _off(uid, _live_sell_position, uid, pos_id, 100, True, _busy=BUSY_MSG)
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
    mev_paused = signer.anti_mev_paused()
    mev_line = "⏸ Anti-MEV paused for maintenance — buys use the fast normal route" if mev_paused else (
        f"{'🟢' if mev else '🔴'} Anti-MEV")
    mev_btn = "⏸ Anti-MEV paused" if mev_paused else f"{'🟢' if mev else '🔴'} Anti-MEV"
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
        f"{mev_line}\n"
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
                    mev_btn,
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
                [InlineKeyboardButton(
                    f"{'🟢' if db.flag_on(uid, 'daily_recap', 1) else '🔴'} Daily morning recap",
                    callback_data="flg:daily_recap",
                )],
                [
                    InlineKeyboardButton("⚙️ Buy/sell presets", callback_data="pst:sol"),
                    InlineKeyboardButton("🎓 Migration sniper", callback_data="mig:x:panel"),
                ],
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


MAX_WATCHED_WALLETS = int(os.getenv("FERZAN_MAX_WATCHED_WALLETS", "20"))


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
    if db.count_watched_wallets(update.effective_user.id) >= MAX_WATCHED_WALLETS:
        await update.effective_message.reply_text(
            f"You're tracking {MAX_WATCHED_WALLETS} wallets (the max). Remove one with /unwatchwallet <id>."
        )
        return
    try:
        chain = onchain.normalize_chain(chain_raw)
        events = await asyncio.to_thread(onchain.recent_activity, chain, address, 3)
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
        f"Watching wallet #{wid} on {chain}\n{address}\n{preview}\n\n"
        f"👁 Alerts only. To copy its buys: /copy {wid} on  (or tap it in /wallets)"
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


async def killswitch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not _is_admin(update.effective_user.id):
        await update.effective_message.reply_text("Admins only.")
        return
    args = list(context.args or [])
    if not args or args[0].lower() not in ("on", "off"):
        state = "ON — auto trading is stopped" if _auto_trading_killed() else "OFF — auto trading is running"
        await update.effective_message.reply_text(
            "Usage: /killswitch on | off\n\n"
            f"Current state: {state}\n\n"
            "ON stops THREE things bot-wide, for every user:\n"
            "  • TP-ladder rung sells (/tpladder)\n"
            "  • Auto-buy-on-feed (the auto_buy setting)\n"
            "  • DCA scheduled buys (/dca)\n\n"
            "It does NOT touch: manual /buy, /livesell, /livesellevm, "
            "or single-target /tp, /sl, /trail exits — those keep working as-is."
        )
        return
    turn_on = args[0].lower() == "on"
    db.set_flag(0, "kill_auto_trading", turn_on)
    if turn_on:
        await update.effective_message.reply_text(
            "🛑 Kill switch ON. TP-ladder rung sells, auto-buy-on-feed, and DCA "
            "buys are stopped bot-wide.\n"
            "Manual trading and single-target /tp, /sl, /trail are unaffected.\n"
            "Run /killswitch off to resume."
        )
    else:
        await update.effective_message.reply_text(
            "✅ Kill switch OFF. TP-ladder rung sells, auto-buy-on-feed, and DCA "
            "buys are running again."
        )


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
            [InlineKeyboardButton("👛 My wallets", callback_data="wsl:list")],
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
    if has:
        rows.append([InlineKeyboardButton("👛 My wallets · switch / new", callback_data="wsl:list")])
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
        n = len(db.list_wallet_slots(update.effective_user.id))
        text = (
            f"👛 Active wallet: <b>{html.escape(user_wallets.active_label(update.effective_user.id))}</b>"
            + (f" ({n} total)" if n > 1 else "")
            + "\nTap a chain for the deposit address and balance."
        )
    else:
        text = "ℹ️ Wallet not found. Generate or import, then every chain lights up."
    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=chain_board_keyboard(update.effective_user.id),
    )


def _mywallets_panel(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    slots = db.list_wallet_slots(uid)
    lines = ["👛 <b>Your wallets</b>", "Buys use the ✅ active wallet. Sells find whichever wallet holds the token.\n"]
    rows = []
    for r in slots:
        mark = "✅" if r["active"] else "▫️"
        sol = r["sol_pub"]
        lines.append(f"{mark} <b>{html.escape(r['label'])}</b>  <code>{sol[:4]}…{sol[-4:]}</code>")
        if not r["active"]:
            rows.append([InlineKeyboardButton(f"Use {r['label'][:20]}", callback_data=f"wsl:use:{r['id']}")])
    if len(slots) < db.MAX_WALLETS:
        rows.append([InlineKeyboardButton("➕ New wallet", callback_data="wsl:new")])
    rows.append([InlineKeyboardButton("📥 Import into new wallet", callback_data="wi:imp")])
    rows.append([InlineKeyboardButton("↩️ Chains", callback_data="wi:chains")])
    lines.append(f"\n{len(slots)}/{db.MAX_WALLETS} · rename the active one: /walletname Sniper")
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def walletname_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    name = " ".join(context.args or []).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /walletname <name>  (renames your active wallet)")
        return
    user_wallets.ensure(uid)
    active = next((r for r in db.list_wallet_slots(uid) if r["active"]), None)
    if active and db.rename_wallet_slot(uid, int(active["id"]), name):
        text, kb = _mywallets_panel(uid)
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.effective_message.reply_text("Couldn't rename — try a shorter name.")


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
        f"Solana imported into a new wallet (now active — your other wallets are untouched).\n`{row['sol_pub']}`",
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
        f"EVM imported into a new wallet (now active — your other wallets are untouched).\n`{row['evm_pub']}`",
        parse_mode="Markdown",
        reply_markup=wallet_keyboard(update.effective_user.id),
    )


async def collectsol_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send ALL SOL to an address (legacy shortcut; /withdraw is the full flow)."""
    if not await guard(update):
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await update.effective_message.reply_text("Private chat only.")
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /collectsol <your-sol-address>  — or use /withdraw")
        return
    ok, dest = withdraw.validate_address("sol", context.args[0])
    if not ok:
        await update.effective_message.reply_text(dest)
        return
    uid = update.effective_user.id
    sol_secret, _evm = user_wallets.secrets(uid)
    status = await _progress(context.bot, update.effective_chat.id, "⏳ Sending all SOL…")
    sent, msg = await _off(uid, withdraw.send_sol, sol_secret, dest, None, _busy=(False, BUSY_MSG))
    await _done(context.bot, update.effective_chat.id, status, msg, disable_web_page_preview=True)


async def disperse_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await update.effective_message.reply_text("Private chat only.")
        return
    dests = []
    for a in context.args or []:
        ok, norm = withdraw.validate_address("sol", a)
        if ok:
            dests.append(norm)
    if len(dests) < 2:
        await update.effective_message.reply_text("📤 Split SOL equally.\n/disperse <addr1> <addr2> [addr3 …]")
        return
    uid = update.effective_user.id
    sol_secret, _evm = user_wallets.secrets(uid)

    def run() -> str:
        kp = signer.keypair_from_secret(sol_secret)
        bag = withdraw.sol_balance(str(kp.pubkey()))
        fee = withdraw.SIG_FEE + withdraw._priority_lamports(800)
        chunk = (bag - fee * len(dests)) // len(dests)
        if chunk < withdraw.SOL_RENT_MIN:
            return "Not enough SOL to split that many ways."
        out = []
        for i, dest in enumerate(dests):
            last = i == len(dests) - 1
            _ok, msg = withdraw.send_sol(sol_secret, dest, None if last else chunk)
            out.append(msg)
            if _ok is not True:
                out.append("Stopped here — check the link before retrying.")
                break
        return "\n".join(out)

    status = await _progress(context.bot, update.effective_chat.id, "⏳ Dispersing…")
    msg = await _off(uid, run, _busy=BUSY_MSG)
    await _done(context.bot, update.effective_chat.id, status, "📤 Disperse\n" + msg, disable_web_page_preview=True)


async def collectevm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await update.effective_message.reply_text("Private chat only.")
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /collectevm [eth|base|bsc|arb] <0x>  — or use /withdraw")
        return
    chain, raw_dest = ("eth", context.args[0]) if len(context.args) == 1 else (context.args[0], context.args[1])
    ok, dest = withdraw.validate_address("evm", raw_dest)
    if not ok:
        await update.effective_message.reply_text(dest)
        return
    uid = update.effective_user.id
    _sol, evm_secret = user_wallets.secrets(uid)
    status = await _progress(context.bot, update.effective_chat.id, "⏳ Sending…")
    sent, msg = await _off(uid, withdraw.send_evm_native, evm_secret, chain, dest, None, _busy=(False, BUSY_MSG))
    await _done(context.bot, update.effective_chat.id, status, msg, disable_web_page_preview=True)


async def wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text, kb = _wallets_panel(update.effective_user.id)
    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
    )


_COPY_SIZES = [0, 10, 25, 50, 100, 250]  # 0 = your default buy size

# Defaults for db.flag_on — must match what settings_cmd displays.
_FLAG_DEFAULTS = {
    "rug_buy": 1,
    "honeypot": 1,
    "anti_mev": 1,
    "lp_watch": 0,
    "score_gate": 0,
    "auto_buy": 0,
    "copy_live": 0,
    "daily_recap": 1,
}


def _is_wallets_panel(message) -> bool:
    try:
        rows = message.reply_markup.inline_keyboard
    except Exception:
        return False
    return any(str(getattr(b, "callback_data", "") or "").startswith("cpy:") for row in rows for b in row)


def _wallets_panel(uid: int) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = db.list_watched_wallets(uid)
    master = db.flag_on(uid, "copy_live", 0)
    default_usd = _default_buy_usd(uid)
    if not rows:
        return (
            "👁 <b>Watched wallets</b>\n\nNone yet.\n"
            "/watchwallet sol &lt;address&gt; [label] — or browse /smartmoney",
            None,
        )
    lines = [
        "👯 <b>Copy trading</b>",
        f"Master switch: {'🟢 ON' if master else '🔴 OFF'}  (applies to wallets marked Copy ON)",
        "",
    ]
    kb_rows = [[InlineKeyboardButton(
        f"{'🟢' if master else '🔴'} Master copy switch", callback_data="flg:copy_live"
    )]]
    for r in rows:
        wid = int(r["id"])
        on = int(r.get("copy_on") or 0)
        sells = int(r.get("copy_sells") or 0)
        size = float(r.get("copy_usd") or 0)
        size_txt = f"${size:g}" if size > 0 else f"default ${default_usd:g}"
        label = html.escape(r.get("label") or r["address"][:8])
        lines.append(
            f"<b>#{wid} {label}</b> · {html.escape(r['chain'].upper())} · "
            f"<code>{html.escape(r['address'][:6])}…{html.escape(r['address'][-4:])}</code>\n"
            f"   {'👯 Copy ON' if on else '👁 Watch only'} · size {size_txt}"
            f"{' · mirrors sells' if sells else ''}"
        )
        kb_rows.append([
            InlineKeyboardButton(f"#{wid} {'🟢 Copy' if on else '⚪️ Copy'}", callback_data=f"cpy:t:{wid}"),
            InlineKeyboardButton(f"💵 {size_txt if size > 0 else 'Default'}", callback_data=f"cpy:z:{wid}"),
            InlineKeyboardButton(f"{'🟢' if sells else '⚪️'} Sells", callback_data=f"cpy:s:{wid}"),
            InlineKeyboardButton("🗑", callback_data=f"cpy:d:{wid}"),
        ])
    lines += [
        "",
        "<i>Copy buys go through your rug / honeypot guards. A token is copied once "
        "per wallet. Mirrored sells only touch tokens bought by copying that wallet.</i>",
        "<i>Exact size: /copy &lt;id&gt; &lt;usd&gt;</i>",
    ]
    return "\n".join(lines), InlineKeyboardMarkup(kb_rows)


async def copy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Usage:\n/copy <id> <usd>   — copy size for that wallet (0 = your default)\n"
            "/copy <id> on|off  — copy that wallet\n"
            "/copy <id> sells on|off\nSee /wallets for ids."
        )
        return
    try:
        wid = int(args[0].lstrip("#"))
    except ValueError:
        await update.effective_message.reply_text("Wallet id must be a number. See /wallets.")
        return
    if not db.get_watched_wallet(wid, uid):
        await update.effective_message.reply_text("No such wallet id. See /wallets.")
        return
    a1 = args[1].lower()
    if a1 in {"on", "off"}:
        db.set_wallet_copy(wid, uid, copy_on=1 if a1 == "on" else 0)
    elif a1 == "sells" and len(args) > 2 and args[2].lower() in {"on", "off"}:
        db.set_wallet_copy(wid, uid, copy_sells=1 if args[2].lower() == "on" else 0)
    else:
        try:
            usd = float(a1.replace("$", ""))
        except ValueError:
            await update.effective_message.reply_text("Send a dollar amount, on/off, or sells on/off.")
            return
        cap = signer.max_usd()
        if usd < 0 or usd > cap:
            await update.effective_message.reply_text(f"Size must be between 0 and ${cap:g}.")
            return
        db.set_wallet_copy(wid, uid, copy_usd=usd)
    text, kb = _wallets_panel(uid)
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)


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
    status, msg = await asyncio.to_thread(sniper.try_fill, armed) if armed else ("armed", "")
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


class DeadChat(Exception):
    """Telegram refused this chat. `permanent` = the chat is gone (deleted,
    bot kicked/blocked) and retrying won't help; otherwise the bot is only
    restricted there (no send rights) and an admin can fix it."""

    def __init__(self, msg: str, permanent: bool = True):
        super().__init__(msg)
        self.permanent = permanent


class ChatMoved(Exception):
    """Group was upgraded to a supergroup; Telegram gave us the new id."""

    def __init__(self, new_chat_id: int):
        super().__init__(f"migrated to {new_chat_id}")
        self.new_chat_id = int(new_chat_id)


_GONE_MARKERS = (
    "chat not found",
    "bot was kicked",
    "bot was blocked",
    "bot is not a member",
    "user is deactivated",
    "chat was deleted",
)
_RESTRICTED_MARKERS = (
    "have no rights to send",
    "not enough rights",
    "chat_write_forbidden",
    "need administrator rights",
)

# Launch + pulse jobs can each count a failure in one cycle, so 5 ≈ 3 cycles.
FEED_MUTE_AFTER = int(os.getenv("FEED_MUTE_AFTER", "5"))
FEED_RETRY_MUTED_S = 3600  # restricted (not gone) chats get one retry per hour


def _dead_chat_kind(exc: Exception) -> str | None:
    """'gone' | 'restricted' | None (transient: flood-wait, network, too long…)."""
    text = str(exc).lower()
    if any(m in text for m in _RESTRICTED_MARKERS):
        return "restricted"
    if any(m in text for m in _GONE_MARKERS):
        return "gone"
    if type(exc).__name__ == "Forbidden":
        return "gone"  # kicked / blocked surface as Forbidden
    return None


def _is_dead_chat_error(exc: Exception) -> bool:
    return _dead_chat_kind(exc) is not None


def _feed_muted(chat_id: int) -> bool:
    fails, last = db.feed_fail_info(int(chat_id))
    return fails >= FEED_MUTE_AFTER and (time.time() - last) < FEED_RETRY_MUTED_S


async def send_launch(bot, chat_id: int, text: str, markup, promo: bool = True) -> None:
    try:
        await _send_launch(bot, chat_id, text, markup, promo)
    except Exception as exc:
        new_id = getattr(exc, "new_chat_id", None)
        if new_id:  # telegram.error.ChatMigrated
            raise ChatMoved(int(new_id)) from exc
        kind = _dead_chat_kind(exc)
        if kind:
            raise DeadChat(str(exc), permanent=(kind == "gone")) from exc
        raise


TG_TEXT_MAX = 4096
TG_CAPTION_MAX = 1024


def _clip_plain(s: str, n: int) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


def _fit_html(text: str, limit: int = TG_TEXT_MAX - 96) -> str:
    """Trim an HTML message to Telegram's limit on a LINE boundary so we
    never cut through a tag (every card line opens and closes its own tags).
    Measured on the raw HTML, which is always >= Telegram's visible count."""
    if len(text) <= limit:
        return text
    out, used = [], 0
    for line in text.split("\n"):
        if used + len(line) + 1 > limit - 2:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out) + "\n…"


async def _send_launch(bot, chat_id: int, text: str, markup, promo: bool = True) -> None:
    text = _fit_html(text)
    clip = PROMO_PATH if promo and PROMO_PATH.exists() else None
    want_gif = (
        bool(clip)
        and os.getenv("FERZAN_PROMO_ON_SIGNALS", "0") == "1"
        and (time.time() - _PROMO_TS.get(int(chat_id), 0) > 3600)
        and len(text) <= TG_CAPTION_MAX  # GIF captions are capped at 1024
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
        if _is_dead_chat_error(exc) or getattr(exc, "new_chat_id", None):
            # Let the caller count it / migrate the chat instead of logging a
            # full traceback on every single card.
            raise
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
    ca = (ln.token or ln.query or "").strip()[:64]
    # Token symbols come straight from chain metadata; EVM tokens can set any
    # length (spam tokens use thousands of chars). Never let them size the card.
    name = html.escape(_clip_plain((ln.symbol or "?").upper(), 24))
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
        text += f"\n🔥 Trending <b>{html.escape(_clip_plain(trends[0]['title'], 64))}</b>"
    if ads:
        text += f"\n📣 {html.escape(_clip_plain(ads[0]['title'], 120))}"
    elif os.getenv("FERZAN_AD_TITLE", "").strip():
        text += f"\n📣 {html.escape(_clip_plain(os.getenv('FERZAN_AD_TITLE', ''), 120))}"
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
        text, markup = await asyncio.to_thread(launch_card, ln)
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
        f"Invites  {st['invites']} direct · {st['invites_l2']} level 2 · {st['invites_l3']} level 3\n"
        f"Their volume  ${st['volume']:,.0f}\n"
        f"Earned  ${st['earned']:.4f}  (L1 ${st['by_level'].get(1, 0):.2f} · L2 ${st['by_level'].get(2, 0):.2f} · L3 ${st['by_level'].get(3, 0):.2f})\n"
        f"Claimable  ${st['open']:.4f}" + (f"  · being paid ${st['pending']:.2f}" if st.get('pending') else "") + "\n\n"
        f"Level 1: Scout 30% of our cut · Captain 35% at $50k · Desk 40% at $250k\n"
        f"Level 2 (their invites): {db.REF_L2_PCT * 100:g}% · Level 3: {db.REF_L3_PCT * 100:g}%\n\n"
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
    upto = db.max_claimed_ref_id(uid)
    sol_pub = (db.get_user_wallet(uid) or {}).get("sol_pub") or "(no wallet yet)"
    await update.effective_message.reply_text(
        f"🧾 Claim of ${amt:.2f} sent to the desk.\n"
        "It's paid from the Ferzan treasury to your SOL wallet — you'll get a message here when it lands."
    )
    for admin in ADMIN_IDS:
        try:
            await context.bot.send_message(
                admin,
                f"🧾 Referral claim ${amt:.2f} from {uid}\nPay to SOL: <code>{html.escape(sol_pub)}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ Mark paid", callback_data=f"refpaid:{uid}:{upto}")]]),
            )
        except Exception:
            logger.exception("claim notify failed for admin %s", admin)


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


async def health_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    if not (_is_admin(uid) or _is_operator(uid)):
        await update.effective_message.reply_text("Operator only.")
        return
    import health

    status = await _progress(context.bot, update.effective_chat.id, "🩺 Checking every service…")
    checks = await asyncio.to_thread(health.run_checks)
    extra = [
        f"Live buys: {'ON' if signer.live_enabled() else 'OFF (LIVE_BUYS=0)'}",
        f"Auto-trading kill switch: {'ENGAGED' if _auto_trading_killed() else 'off'}",
    ]
    try:
        with db.get_conn() as conn:
            muted = conn.execute(
                "SELECT chat_id, last_error FROM feed_failures WHERE fails >= ?", (FEED_MUTE_AFTER,)
            ).fetchall()
        if muted:
            extra.append(f"Muted feed chats: {len(muted)}")
            for r in muted[:5]:
                extra.append(f"  · {r['chat_id']}: {html.escape(str(r['last_error'] or ''))[:60]}")
    except Exception:
        pass
    await _done(
        context.bot,
        update.effective_chat.id,
        status,
        health.render(checks, extra),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    if update.effective_chat and update.effective_chat.type == "private" and await _withdraw_text(update, context, text):
        return
    talert = (context.user_data or {}).get("talert")
    if talert:
        context.user_data["talert"] = None
        await _create_token_alert(update, update.effective_user.id, talert, text)
        return
    pending = (context.user_data or {}).get("buyx")
    if pending:
        context.user_data["buyx"] = None
        try:
            amt = float(text.replace(",", ""))
        except ValueError:
            await update.effective_message.reply_text("Send a number. Example: 0.05")
            return
        uid = update.effective_user.id
        chat_id = update.effective_chat.id
        status = await _progress(context.bot, chat_id, "⏳ Buying…")
        try:
            card = await asyncio.to_thread(analyze, pending)
        except Exception as exc:
            await _done(context.bot, chat_id, status, str(exc))
            return
        cid = resolve_chain(card.snapshot.chain) or "sol"
        gecko = {"sol": "solana", "bsc": "binancecoin", "avax": "avalanche-2"}.get(cid, "ethereum")
        try:
            px = await asyncio.to_thread(get_price_usd, gecko)
        except Exception:
            px = 0
        usd_o = amt * px if px > 0 else _default_buy_usd(uid)
        live_msg = await _off(uid, _live_buy_followup, uid, card, pending, True, True, usd_override=usd_o, _busy=BUSY_MSG)
        await _done(context.bot, chat_id, status, f"{amt:g} native ≈ ${usd_o:.2f}\n{live_msg}")
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
    chat_id = update.effective_chat.id
    status = await _progress(context.bot, chat_id, f"⏳ Auto-buy ${usd:.0f}…")
    try:
        card = await asyncio.to_thread(analyze, text)
    except Exception as exc:
        await _done(context.bot, chat_id, status, f"⚡️ Auto-buy skipped: {exc}")
        return
    live_msg = await _off(uid, _live_buy_followup, uid, card, text, True, True, usd_override=usd, _busy=BUSY_MSG)
    await _done(context.bot, chat_id, status, f"⚡️ Auto-buy ${usd:.0f}\n{live_msg}")


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



async def _safe_answer(query, text: str | None = None) -> None:
    """on_callback already answered the query up front; Telegram rejects a
    second answer, so turn follow-up answers into best-effort no-ops that
    can't abort the handler before its real work/confirmation runs."""
    try:
        await query.answer(text)
    except Exception:
        pass


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
                # NOTE: no local `import asyncio` here — a function-level import
                # makes `asyncio` local to all of on_callback and breaks every
                # other asyncio.to_thread() in it (UnboundLocalError).
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
    if data.startswith("wsl:"):
        parts = data.split(":")
        op = parts[1] if len(parts) > 1 else "list"
        if op == "use" and len(parts) > 2:
            try:
                slot = user_wallets.switch_wallet(uid, int(parts[2]))
            except ValueError:
                slot = None
            if slot:
                await context.bot.send_message(uid, f"✅ Now trading from <b>{html.escape(slot['label'])}</b>.", parse_mode="HTML")
        elif op == "new":
            try:
                row = user_wallets.new_wallet(uid)
                await context.bot.send_message(
                    uid,
                    "✨ New wallet created and set active. Fund it from /wallet → a chain.\n"
                    f"SOL <code>{html.escape(row.get('sol_pub', ''))}</code>",
                    parse_mode="HTML",
                )
            except Exception as exc:
                await context.bot.send_message(uid, str(exc))
        text, kb = _mywallets_panel(uid)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await context.bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
        return
    if data.startswith("wi:"):
        kind = data[3:]
        if kind == "gen":
            if db.get_user_wallet(uid):
                # Already has one: don't silently create + switch (exits/buys
                # would follow the new empty wallet). Show the panel with an
                # explicit ➕ New wallet button instead.
                text, kb = _mywallets_panel(uid)
                await context.bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
                return
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
                "📥 Import — creates a NEW wallet (nothing you have is replaced)\n"
                "/importsol <solana-private-key>\n"
                "/importevm <0x-private-key>\n"
                "Only in this private chat. The bot deletes your message.",
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
            text, kb = _mywallets_panel(uid)
            await context.bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
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
                lamports = await asyncio.to_thread(signer.sol_balance_lamports, addr)
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
            try:
                import ton_signer

                sol_secret, _ = user_wallets.secrets(uid)
                addr, ton_bal = await asyncio.to_thread(ton_signer.address_and_balance, sol_secret)
                bal_line = f"{ton_bal:.6f} TON"
            except Exception as exc:
                logger.info("ton wallet lookup failed for %s: %s", uid, exc)
                # Never fall back to another chain's address here — someone
                # would send TON to it.
                await context.bot.send_message(
                    uid, "💠 TON wallet lookup failed (liteserver busy). Tap TON again in a few seconds."
                )
                return
        else:
            addr = row["evm_pub"]
            try:
                amt, sym = await asyncio.to_thread(evm_signer.native_balance, cid, addr)
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
        elif kind == "mig":
            await _mig_callback(update, context, "mig:x:panel")
        elif kind == "alerts":
            text, kb = _token_alerts_panel(uid)
            await context.bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
        elif kind == "withdraw":
            if update.effective_chat and update.effective_chat.type != "private":
                await context.bot.send_message(uid, "Open /withdraw in your private chat with the bot.")
            else:
                context.user_data["wd"] = {}
                bals = await asyncio.to_thread(_wd_balances, uid)
                text, kb = _wd_menu(uid, bals)
                await context.bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)
        elif kind == "ref":
            await ref_cmd(update, context)
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
    if data.startswith("trl:"):
        _tag, pct_s, mint = data.split(":", 2)
        try:
            pct = max(1.0, min(90.0, float(pct_s)))
        except ValueError:
            pct = 20.0
        db.set_live_exit(uid, mint, trail_pct=pct)
        db.reset_live_peak(uid, mint)
        await context.bot.send_message(
            uid,
            f"📉 Trailing stop {pct:.0f}% armed: sells everything if the bag's value falls "
            f"{pct:.0f}% from its highest point from now on. Custom size: /trail 15 {mint}",
        )
        return
    if data.startswith("exc:"):
        mint = data[4:]
        db.clear_live_exit(uid, mint)
        db.clear_tp_ladder(uid, mint)
        await context.bot.send_message(uid, "🧹 Exit rules cleared for that token (TP, SL, trail, ladder).")
        return
    if data.startswith("sli:"):
        mint = data[4:]
        status = await _progress(context.bot, uid, "⏳ Working out your initials…")
        try:
            amount, _owner, _venue = await asyncio.to_thread(_bag_position_amount, uid, mint)
            px = await asyncio.to_thread(_token_mark_usd, mint)
        except Exception as exc:
            await _done(context.bot, uid, status, f"Couldn't read the bag: {exc}")
            return
        cost = db.live_cost(uid, mint)
        worth = amount * px
        pct = _initials_pct(cost, worth)
        if pct is None:
            await _done(
                context.bot, uid, status,
                "Nothing to take out yet — the bag is worth less than you put in (or the price is unavailable).",
            )
            return
        ok, msg, label = await _off(uid, _sell_any, uid, mint, pct, _busy=(False, BUSY_MSG, ""))
        head = f"💰 Initials: sold {pct}% (≈ ${cost:,.2f} of ${worth:,.2f}) — the rest rides free.\n"
        await _done(context.bot, uid, status, head + _trade_result("sell", ok, label, msg, pct=pct))
        if ok and _is_bag_panel(query.message):
            await _refresh_bag_panel(query, uid, mint, quiet=True)
        return
    if data.startswith("pst:"):
        cid = resolve_chain(data[4:]) or data[4:] or "sol"
        await context.bot.send_message(uid, _presets_text(uid, cid), parse_mode="HTML")
        return
    if data.startswith("talt:"):
        context.user_data["talert"] = data[5:]
        await context.bot.send_message(
            uid,
            "🔔 Send the alert target:\n"
            "• <code>2m</code> or <code>250k</code> — market cap\n"
            "• <code>+50%</code> / <code>-30%</code> — move from the price now\n"
            "• <code>price 0.0012</code> — exact USD price",
            parse_mode="HTML",
        )
        return
    if data.startswith("tax:"):
        try:
            aid = int(data[4:])
        except ValueError:
            return
        ok = db.delete_token_alert(aid, uid)
        await _safe_answer(query, "Alert removed" if ok else "Already gone")
        text, kb = _token_alerts_panel(uid)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        except Exception:
            pass
        return
    if data.startswith("refpaid:") and _is_admin(uid):
        parts = data.split(":")
        try:
            who = int(parts[1])
            upto = int(parts[2]) if len(parts) > 2 else None
        except (ValueError, IndexError):
            return
        amt = db.mark_referral_paid(who, upto)  # only the claim this button was sent for
        await context.bot.send_message(uid, f"✅ Marked ${amt:.2f} paid for {who}.")
        if amt > 0:
            try:
                await context.bot.send_message(who, f"💸 Your referral payout of ${amt:.2f} was sent to your Ferzan SOL wallet.")
            except Exception:
                pass
        return
    if data.startswith("wd:") or data.startswith("ab:"):
        await _withdraw_callback(update, context, data)
        return
    if data.startswith("mig:"):
        await _mig_callback(update, context, data)
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
        status = await _progress(context.bot, uid, f"⏳ Selling {pct}%…")
        ok, msg, label = await _off(uid, _sell_any, uid, mint, pct, _busy=(False, BUSY_MSG, ""))
        await _done(context.bot, uid, status, _trade_result("sell", ok, label, msg, pct=pct))
        if ok and _is_bag_panel(query.message):
            await _refresh_bag_panel(query, uid, mint, quiet=True)
        return
    if data.startswith("bagr:"):
        await _refresh_bag_panel(query, uid, data[5:])
        return
    if data.startswith("pnlc:"):
        await _send_pnl_card(context.bot, uid, data[5:])
        return
    if data.startswith("blm:"):
        mint = data[4:]
        try:
            card = await asyncio.to_thread(analyze, mint)
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
    if data.startswith("tour:"):
        if data == "tour:bal":
            await _tour_step2(query, uid)
        else:
            await _tour_step3(query, uid)
        return
    if data.startswith("sig:"):
        await _send_signal(update, data[4:], edit=data[4:] != TOUR_DEMO_MINT)
        return
    if data.startswith("fd:"):
        cid = data[3:]
        if cid not in FEED_CHAINS:
            return
        now = not db.flag_on(uid, f"feed_{cid}", 1)
        db.set_flag(uid, f"feed_{cid}", now)
        await _safe_answer(query, "Saved")
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
            await _safe_answer(query, "Saved")
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
            "daily_recap",
        }:
            return
        if flag == "anti_mev" and signer.anti_mev_paused():
            await context.bot.send_message(
                uid,
                "⏸ Anti-MEV is paused for maintenance while we fix private (Jito) delivery. "
                "Buys use the fast normal route meanwhile, and your setting comes back when it's fixed.",
            )
            return
        # Read with the SAME default the rest of the bot uses for this flag,
        # otherwise the first tap on a never-set default-OFF flag writes OFF.
        now = not db.flag_on(uid, flag, _FLAG_DEFAULTS.get(flag, 0))
        db.set_flag(uid, flag, now)
        if flag == "copy_live" and _is_wallets_panel(query.message):
            text, kb = _wallets_panel(uid)
            try:
                await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            except Exception:
                pass
            return
        await context.bot.send_message(
            uid,
            f"{'🟢 ON' if now else '🔴 OFF'} {flag.replace('_', ' ')}",
        )
        return
    if data.startswith("cpy:"):
        _tag, op, wid_s = (data.split(":", 2) + ["", ""])[:3]
        try:
            wid = int(wid_s)
        except ValueError:
            return
        w = db.get_watched_wallet(wid, uid)
        if not w:
            return
        if op == "t":
            db.set_wallet_copy(wid, uid, copy_on=0 if int(w.get("copy_on") or 0) else 1)
        elif op == "s":
            db.set_wallet_copy(wid, uid, copy_sells=0 if int(w.get("copy_sells") or 0) else 1)
        elif op == "z":
            cur = float(w.get("copy_usd") or 0)
            cap = signer.max_usd()
            sizes = [s for s in _COPY_SIZES if s <= cap]
            nxt = next((s for s in sizes if s > cur), sizes[0])
            db.set_wallet_copy(wid, uid, copy_usd=nxt)
        elif op == "d":
            # Two-tap delete: first tap arms, second tap within 15s removes.
            import time as _t

            armed = context.user_data.get("cpy_del")
            if not armed or armed[0] != wid or _t.monotonic() - armed[1] > 15:
                context.user_data["cpy_del"] = (wid, _t.monotonic())
                who = w.get("label") or (w.get("address") or "")[:8]
                await context.bot.send_message(uid, f"🗑 Tap 🗑 again within 15s to stop watching {who}.")
                return
            context.user_data.pop("cpy_del", None)
            db.delete_watched_wallet(wid, uid)
            db.clear_copy_fills_for_wallet(uid, wid)
        text, kb = _wallets_panel(uid)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
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
            await _safe_answer(query, "Gone.")
            return
        already = any(
            x["chain"] == w["chain"] and (x.get("address") or "").lower() == w["address"].lower()
            for x in db.list_watched_wallets(uid)
        )
        if already:
            await _safe_answer(query, "Already following.")
            return
        if db.count_watched_wallets(uid) >= MAX_WATCHED_WALLETS:
            await context.bot.send_message(uid, f"You're tracking {MAX_WATCHED_WALLETS} wallets (the max). Remove one first.")
            return
        new_id = db.add_watched_wallet(uid, w["chain"], w["address"], w["label"])
        try:
            events = await asyncio.to_thread(onchain.recent_activity, w["chain"], w["address"], 1)
            if events:
                db.set_wallet_cursor(new_id, events[0].txid)
        except Exception:
            pass
        await _safe_answer(query, "Following")
        await context.bot.send_message(uid, f"👁 Now following {w['label']} ({w['chain']}). DM ping when it moves.")
        return
    if data.startswith("bnv:"):
        _tag, amt_s, name = data.split(":", 2)
        try:
            amt = float(amt_s)
        except ValueError:
            amt = 0.05
        status = await _progress(context.bot, uid, f"⏳ Buying {amt:g}…")
        try:
            card = await asyncio.to_thread(analyze, name)
        except PriceFetchError as exc:
            await _done(context.bot, uid, status, str(exc))
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
            px = await asyncio.to_thread(get_price_usd, gecko)
        except Exception:
            px = 0
        usd_o = amt * px if px > 0 else _default_buy_usd(uid)
        usd_o = min(signer.max_usd(), max(1.0, usd_o))
        live_msg = await _off(uid, _live_buy_followup, uid, card, name, True, True, usd_override=usd_o, _busy=BUSY_MSG)
        await _done(context.bot, uid, status, f"{amt:g} native ≈ ${usd_o:.2f}\n{live_msg or ''}")
        return
    if data.startswith("buyz:"):
        _tag, usd_s, name = data.split(":", 2)
        try:
            usd_o = float(usd_s)
        except ValueError:
            usd_o = _default_buy_usd(uid)
        status = await _progress(context.bot, uid, f"⏳ Buying ${usd_o:g}…")
        try:
            card = await asyncio.to_thread(analyze, name)
        except PriceFetchError as exc:
            await _done(context.bot, uid, status, str(exc))
            return
        live_msg = await _off(uid, _live_buy_followup, uid, card, name, True, True, usd_override=usd_o, _busy=BUSY_MSG)
        await _done(context.bot, uid, status, live_msg or "Buy sent.")
        return
    if data.startswith("buy:") or data.startswith("force:"):
        force = data.startswith("force:")
        name = data.split(":", 1)[1]
        status = await _progress(context.bot, uid, "⏳ Buying…")
        try:
            card = await asyncio.to_thread(analyze, name)
        except PriceFetchError as exc:
            await _done(context.bot, uid, status, str(exc))
            return
        live_msg = await _off(uid, _live_buy_followup, uid, card, name, True, force, _busy=BUSY_MSG)
        await _done(context.bot, uid, status, live_msg or "Buy sent.")
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
        await _safe_answer(query, f"{cid.upper()} slip {nxt}%")
        await context.bot.send_message(uid, f"🎚 {cid.upper()} buy/sell slip → {nxt}%")
        return
    if data.startswith("xgas:"):
        cid = resolve_chain(data[5:]) or data[5:] or "sol"
        cur = db.get_chain_trade(uid, cid)
        now = float(cur.get("gas") or 0)
        nxt = {0.0: 0.001, 0.001: 0.005, 0.005: 0.01, 0.01: 0.0}.get(round(now, 3), 0.005)
        db.set_chain_trade(uid, cid, gas=nxt)
        await _safe_answer(query, f"{cid.upper()} gas tip {nxt}")
        if cid == "sol":
            route = "Jito tip (Anti-MEV on)" if signer.exec_opts(uid)["anti_mev"] else "priority fee (Anti-MEV off)"
            shown = f"{nxt} SOL" if nxt else "default 0.001 SOL"
            await context.bot.send_message(uid, f"⛽ SOL speed → {shown} per trade, paid as {route}.")
        else:
            await context.bot.send_message(
                uid, f"⛽ {cid.upper()} tip → {nxt}. (Applies to Solana trades; EVM gas is priced automatically.)"
            )
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
            for row in await asyncio.to_thread(signer.holdings, sol_secret):
                if row.get("mint") == mint:
                    amount = float(row.get("amount") or 0)
                    break
        except Exception:
            pass
        try:
            card = await asyncio.to_thread(analyze, mint)
            text = render_card(card, uid)
            chain = card.snapshot.chain or ""
        except Exception:
            text = (await asyncio.to_thread(_bag_panel, mint, amount, addr, uid))[0]
            chain = "sol"
        kb = sell_keyboard(mint, mint, chain, uid, amount)
        await context.bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
        return
    if data.startswith("xsell:"):
        # xsell:<pos_id>[:<pct>] — old buttons without a pct mean 100%.
        parts = data.split(":")
        try:
            pos_id = int(parts[1])
            pct = int(parts[2]) if len(parts) > 2 else 100
        except (ValueError, IndexError):
            return
        pct = max(1, min(100, pct))
        status = await _progress(context.bot, uid, f"⏳ Selling {pct}%…")
        await _done(
            context.bot, uid, status, await _off(uid, _live_sell_position, uid, pos_id, pct, _busy=BUSY_MSG)
        )


# ============================================================ batch 4 ====
# Presets, sell-initials, token alerts, withdraw, migration sniper, Mini App
# orders, daily recap. Blocking helpers are plain functions (run them via
# asyncio.to_thread / _off); handlers are async.

_NATIVE_GECKO = {
    "sol": "solana", "bsc": "binancecoin", "eth": "ethereum", "base": "ethereum", "arb": "ethereum",
    "hood": "ethereum", "avax": "avalanche-2", "ton": "the-open-network",
}
_NATIVE_UNIT = {
    "sol": "SOL", "bsc": "BNB", "eth": "ETH", "base": "ETH", "arb": "ETH", "hood": "ETH",
    "avax": "AVAX", "ton": "TON",
}


def _native_usd(cid: str) -> float:
    """USD price of a chain's native coin, 0 if unavailable. Blocking."""
    try:
        return float(get_price_usd(_NATIVE_GECKO.get(cid, "ethereum")) or 0)
    except Exception:
        return 0.0


def _initials_pct(cost: float, worth: float) -> int | None:
    """% of the bag to sell to get the money you put in back out.
    None when the bag isn't worth more than it cost."""
    import math

    if cost <= 0 or worth <= 0 or worth <= cost:
        return None
    return max(1, min(100, math.ceil(cost / worth * 100)))


# ---------------------------------------------------------------- presets --
def _presets_text(uid: int, cid: str) -> str:
    unit = _NATIVE_UNIT.get(cid, "ETH")
    cur = " · ".join(f"{v:g}" for v in db.buy_presets(uid, cid))
    sells = " · ".join(f"{v}%" for v in db.sell_presets(uid))
    return (
        f"⚙️ <b>Quick-buy presets · {html.escape(cid.upper())}</b>\n"
        f"Buy buttons: {html.escape(cur)} {unit}\n"
        f"Sell buttons: {html.escape(sells)} (+ 100%)\n\n"
        f"Change buys: <code>/presets {cid} 0.1 0.25 0.5 1 2 5</code> (up to 6)\n"
        "Change sells: <code>/presets sell 20 50 75</code> (up to 3)\n"
        f"Back to default: <code>/presets {cid} reset</code>"
    )


async def presets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    args = [a.strip().lower() for a in (context.args or [])]
    if not args:
        await update.effective_message.reply_text(_presets_text(uid, "sol"), parse_mode="HTML")
        return
    target = args[0]
    if target == "sell":
        vals = sorted({int(v) for v in db._parse_nums(" ".join(args[1:])) if 1 <= v < 100})
        if args[1:2] == ["reset"]:
            vals = []
        elif not vals:
            await update.effective_message.reply_text("Give 1–3 percentages under 100. Example: /presets sell 20 50 75")
            return
        db.set_sell_presets(uid, vals[:3])
        await update.effective_message.reply_text(_presets_text(uid, "sol"), parse_mode="HTML")
        return
    cid = resolve_chain(target) or ""
    if cid not in _NATIVE_UNIT:
        await update.effective_message.reply_text("Chains: sol, eth, base, bsc, arb, avax, ton, hood — or 'sell'.")
        return
    if args[1:2] == ["reset"]:
        db.set_buy_presets(uid, cid, [])
    else:
        vals = [v for v in db._parse_nums(" ".join(args[1:])) if v <= 1_000_000]
        if not vals:
            await update.effective_message.reply_text(f"Give up to 6 amounts. Example: /presets {cid} 0.1 0.5 1")
            return
        db.set_buy_presets(uid, cid, sorted(set(vals))[:6])
    await update.effective_message.reply_text(_presets_text(uid, cid), parse_mode="HTML")


# ----------------------------------------------------------- token alerts --
_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}


def _parse_money(raw: str) -> float | None:
    t = raw.strip().lower().replace("$", "").replace(",", "")
    mult = 1.0
    if t and t[-1] in _SUFFIX:
        mult, t = _SUFFIX[t[-1]], t[:-1]
    try:
        v = float(t) * mult
    except ValueError:
        return None
    return v if v > 0 else None


def parse_alert_target(text: str, px: float, mc: float) -> tuple[str, str, float, str] | str:
    """-> (kind, direction, target, note) or an error string.
    kind: 'mc' | 'price'."""
    t = " ".join((text or "").strip().lower().split())
    if not t:
        return "Send a target like 2m, +50% or price 0.0012."
    if t.endswith("%"):
        try:
            pct = float(t[:-1].replace("+", ""))
        except ValueError:
            return "Percent looks off. Try +50% or -30%."
        if pct == 0 or pct <= -100:
            return "Pick a move between -99% and anything up."
        if px <= 0:
            return "No live price for this token right now — use a market cap target like 2m."
        target = px * (1 + pct / 100)
        return "price", ("above" if pct > 0 else "below"), target, f"{pct:+g}%"
    if t.startswith("price") or t.startswith("px"):
        v = _parse_money(t.split(" ", 1)[1] if " " in t else "")
        if v is None:
            return "Price looks off. Example: price 0.0012"
        if px <= 0:
            return "No live price for this token right now."
        return "price", ("above" if v > px else "below"), v, f"${v:.6g}"
    if t.startswith("mc"):
        t = t[2:].strip()
    v = _parse_money(t)
    if v is None:
        return "Couldn't read that. Try 2m, 250k, +50% or price 0.0012."
    if v < 1000:
        return "That's too small for a market cap. For a price use: price 0.0012"
    if mc <= 0:
        return "No live market cap for this token right now — try a % move instead."
    return "mc", ("above" if v > mc else "below"), v, f"${v:,.0f} MC"


def _fmt_mc(v: float) -> str:
    for n, s in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if v >= n:
            return f"${v / n:.2f}{s}"
    return f"${v:,.0f}"


def _token_alerts_panel(uid: int) -> tuple[str, InlineKeyboardMarkup | None]:
    rows = db.list_token_alerts(uid)
    if not rows:
        return (
            "🔔 <b>Token alerts</b>\nNone set. Tap 🔔 Alert on any token card, or:\n"
            "<code>/alert &lt;CA&gt; 2m</code> · <code>/alert &lt;CA&gt; +50%</code>",
            None,
        )
    lines = ["🔔 <b>Token alerts</b>"]
    kb = []
    for r in rows:
        what = _fmt_mc(r["target"]) + " MC" if r["kind"] == "mc" else f"${r['target']:.6g}"
        arrow = "▲" if r["direction"] == "above" else "▼"
        sym = html.escape(r["symbol"] or r["mint"][:6])
        lines.append(f"#{r['id']} ${sym} {arrow} {what}" + (f" ({html.escape(r['note'])})" if r["note"] else ""))
        kb.append([InlineKeyboardButton(f"❌ #{r['id']} ${r['symbol'] or r['mint'][:6]}", callback_data=f"tax:{r['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def _create_token_alert(update: Update, uid: int, mint: str, target_text: str) -> None:
    msg = update.effective_message
    if len(db.list_token_alerts(uid)) >= db.MAX_TOKEN_ALERTS:
        await msg.reply_text(f"You have {db.MAX_TOKEN_ALERTS} alerts — remove some in /alerts first.")
        return
    marks = await asyncio.to_thread(portfolio._ds_prices, [mint])
    m = marks.get(mint) or next((v for k, v in marks.items() if k.lower() == mint.lower()), {}) or {}
    got = parse_alert_target(target_text, float(m.get("price") or 0), float(m.get("mc") or 0))
    if isinstance(got, str):
        await msg.reply_text(got)
        return
    kind, direction, target, note = got
    sym = m.get("symbol") or ""
    aid = db.add_token_alert(uid, mint, sym, kind, direction, target, note)
    now = _fmt_mc(float(m.get("mc") or 0)) + " MC" if kind == "mc" else f"${float(m.get('price') or 0):.6g}"
    what = _fmt_mc(target) + " MC" if kind == "mc" else f"${target:.6g}"
    await msg.reply_text(
        f"🔔 Alert #{aid} set: ${sym or mint[:6]} {'▲ above' if direction == 'above' else '▼ below'} {what}\n"
        f"Now: {now}. Checked every minute. /alerts to manage."
    )


async def alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text, kb = _token_alerts_panel(update.effective_user.id)
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def token_alert_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.list_token_alerts()
    if not rows:
        return
    mints = sorted({r["mint"] for r in rows})
    marks = await asyncio.to_thread(portfolio._ds_prices, mints)
    for r in rows:
        m = marks.get(r["mint"]) or {}
        val = float((m.get("mc") if r["kind"] == "mc" else m.get("price")) or 0)
        if val <= 0:
            continue
        hit = val >= r["target"] if r["direction"] == "above" else val <= r["target"]
        if not hit or not db.fire_token_alert(r["id"]):
            continue
        mint = r["mint"]
        cid = portfolio.DS_TO_CID.get(m.get("chain", ""), "") or ("sol" if _is_sol_mint(mint) else "base")
        unit = _NATIVE_UNIT.get(cid, "ETH")
        q = mint[:48]
        buys = [InlineKeyboardButton(f"🟢 {v:g} {unit}", callback_data=f"bnv:{v:g}:{q}") for v in db.buy_presets(r["user_id"], cid)[:3]]
        kb = [buys, [
            InlineKeyboardButton("↔️ Sell", callback_data=f"slc:{q}"),
            InlineKeyboardButton("📡 Card", callback_data=f"sig:{q}"),
        ]]
        if m.get("url"):
            kb[1].append(InlineKeyboardButton("📈 Chart", url=m["url"]))
        what = _fmt_mc(val) + " MC" if r["kind"] == "mc" else f"${val:.6g}"
        tgt = _fmt_mc(r["target"]) + " MC" if r["kind"] == "mc" else f"${r['target']:.6g}"
        try:
            await context.bot.send_message(
                r["user_id"],
                f"🔔 ${html.escape(m.get('symbol') or r['symbol'] or mint[:6])} hit {tgt}"
                f"{' (' + html.escape(r['note']) + ')' if r['note'] else ''}\nNow {what} · 24h {float(m.get('chg24') or 0):+.1f}%\n<code>{html.escape(mint)}</code>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(kb),
            )
        except Exception:
            logger.exception("token alert send failed for %s", r["user_id"])


# --------------------------------------------------------------- withdraw --
_WD_NATIVE = {
    "sol": ("sol", "SOL", "Solana", 9),
    "base": ("evm", "ETH", "Base", 18),
    "eth": ("evm", "ETH", "Ethereum", 18),
    "bsc": ("evm", "BNB", "BNB Chain", 18),
    "arb": ("evm", "ETH", "Arbitrum", 18),
    "ton": ("ton", "TON", "TON", 9),
}


def _wd_balances(uid: int) -> dict:
    """Native balances of the ACTIVE wallet (raw units). Blocking."""
    w = db.get_user_wallet(uid) or {}
    sol_pub, evm_pub = w.get("sol_pub", ""), w.get("evm_pub", "")
    out: dict = {}
    try:
        out["sol"] = withdraw.sol_balance(sol_pub) if sol_pub else 0
    except Exception:
        out["sol"] = None
    for cid in ("base", "eth", "bsc", "arb"):
        try:
            out[cid] = withdraw.evm_native_balance(cid, evm_pub) if evm_pub else 0
        except Exception:
            out[cid] = None
    try:
        import ton_signer

        sol_secret, _evm = user_wallets.secrets(uid)
        _addr, bal = ton_signer.address_and_balance(sol_secret)
        out["ton"] = int(bal * 1e9)
    except Exception:
        out["ton"] = None
    return out


def _wd_menu(uid: int, bals: dict) -> tuple[str, InlineKeyboardMarkup]:
    label = user_wallets.active_label(uid)
    rows, pair = [], []
    for key, (_fam, sym, name, dec) in _WD_NATIVE.items():
        raw = bals.get(key)
        amt = "?" if raw is None else f"{raw / 10 ** dec:.4g}"
        pair.append(InlineKeyboardButton(f"{sym} · {name} ({amt})", callback_data=f"wd:a:{key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([
        InlineKeyboardButton("🪙 A Solana token", callback_data="wd:spl"),
        InlineKeyboardButton("🪙 An EVM token", callback_data="wd:erc"),
    ])
    rows.append([InlineKeyboardButton("📒 Address book", callback_data="ab:list"), InlineKeyboardButton("✖️ Cancel", callback_data="wd:cancel")])
    return (
        f"📤 <b>Withdraw</b> from <b>{html.escape(label)}</b>\nPick what to send:",
        InlineKeyboardMarkup(rows),
    )


async def withdraw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await update.effective_message.reply_text("Withdrawals only work in a private chat with the bot.")
        return
    uid = update.effective_user.id
    context.user_data["wd"] = {}
    status = await update.effective_message.reply_text("⏳ Reading balances…")
    bals = await asyncio.to_thread(_wd_balances, uid)
    text, kb = _wd_menu(uid, bals)
    try:
        await status.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)


def _wd_amount_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("25%", callback_data="wd:p:25"),
            InlineKeyboardButton("50%", callback_data="wd:p:50"),
            InlineKeyboardButton("100%", callback_data="wd:p:100"),
        ],
        [InlineKeyboardButton("✏️ Type an amount", callback_data="wd:x"), InlineKeyboardButton("✖️ Cancel", callback_data="wd:cancel")],
    ])


def _wd_ui(st: dict) -> str:
    return f"{st['bal_raw'] / 10 ** st['decimals']:,.6g} {st['sym']}"


def _wd_amount_text(st: dict) -> str:
    if st.get("raw") is None:
        return f"everything ({_wd_ui(st)}{' minus the network fee' if st['kind'] == 'native' else ''})"
    return f"{st['raw'] / 10 ** st['decimals']:,.6g} {st['sym']}"


async def _wd_ask_dest(bot, uid: int, st: dict) -> None:
    st["await"] = "dest"
    book = db.list_addresses(uid, st["family"])[:8]
    kb = [[InlineKeyboardButton(f"📒 {b['label']} · {withdraw.short(b['address'])}", callback_data=f"wd:d:{b['id']}")] for b in book]
    kb.append([InlineKeyboardButton("✖️ Cancel", callback_data="wd:cancel")])
    await bot.send_message(
        uid,
        f"📤 Sending {_wd_amount_text(st)}\nNow paste the {st['net']} address to send to"
        + (", or pick a saved one:" if book else ":"),
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def _wd_confirm(bot, uid: int, st: dict) -> None:
    import secrets as _secrets

    st["await"] = ""
    st["nonce"] = _secrets.token_hex(4)
    st["confirmed"] = _wd_snapshot(st)
    label = user_wallets.active_label(uid)
    to = st["dest"]
    saved = next((b for b in db.list_addresses(uid, st["family"]) if b["address"] == to), None)
    await bot.send_message(
        uid,
        "📤 <b>Confirm withdrawal</b>\n"
        f"Send: <b>{html.escape(_wd_amount_text(st))}</b>\n"
        f"Network: {html.escape(st['net'])}\n"
        f"From: {html.escape(label)}\n"
        f"To: {html.escape(saved['label'] + ' · ') if saved else ''}<code>{html.escape(to)}</code>\n\n"
        "⚠️ Crypto sends can't be reversed. Check the address matches, character for character.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Send it", callback_data=f"wd:go:{st['nonce']}"),
            InlineKeyboardButton("✖️ Cancel", callback_data="wd:cancel"),
        ]]),
    )


def _wd_from(uid: int) -> dict:
    w = db.get_user_wallet(uid) or {}
    return {"from_sol": w.get("sol_pub", ""), "from_evm": (w.get("evm_pub") or "").lower()}


def _wd_select_native(uid: int, key: str) -> dict:
    fam, sym, net, dec = _WD_NATIVE[key]
    bals = _wd_balances(uid)
    return {"kind": "native", "family": fam, "chain": key, "sym": sym, "net": net, "decimals": dec,
            "bal_raw": int(bals.get(key) or 0), "raw": None, "mint": "", **_wd_from(uid)}


def _wd_select_spl(uid: int, mint: str) -> dict:
    w = db.get_user_wallet(uid) or {}
    h = withdraw.spl_holding(w.get("sol_pub", ""), mint)
    meta = _token_meta(mint)
    return {"kind": "token", "family": "sol", "chain": "sol", "sym": (meta.get("symbol") or mint[:4]).upper(),
            "net": "Solana", "decimals": h["decimals"], "bal_raw": h["raw"], "raw": None, "mint": mint, **_wd_from(uid)}


def _wd_select_erc(uid: int, chain: str, token: str) -> dict:
    w = db.get_user_wallet(uid) or {}
    raw, dec, sym = withdraw.erc20_holding(chain, token, w.get("evm_pub", ""))
    return {"kind": "token", "family": "evm", "chain": chain, "sym": sym or "TOKEN",
            "net": _WD_NATIVE.get(chain, ("", "", chain.upper(), 18))[2], "decimals": dec,
            "bal_raw": raw, "raw": None, "mint": token, **_wd_from(uid)}


def _wd_snapshot(st: dict) -> tuple:
    """Everything a confirm screen promised. Executing re-checks it."""
    return (st.get("kind"), st.get("family"), st.get("chain"), st.get("mint"), st.get("raw"),
            st.get("dest"), st.get("from_sol"), st.get("from_evm"))


def _wd_execute(uid: int, st: dict) -> tuple[bool | None, str]:
    sol_secret, evm_secret = user_wallets.secrets(uid)
    # Send ONLY from the wallet the user picked the balance from: switching
    # the active wallet between confirm and send must not redirect the funds.
    now_sol = str(signer.keypair_from_secret(sol_secret).pubkey()) if sol_secret else ""
    try:
        from eth_account import Account

        now_evm = Account.from_key(evm_secret if evm_secret.startswith("0x") else "0x" + evm_secret).address.lower()
    except Exception:
        now_evm = ""
    if (st["family"] in ("sol", "ton") and now_sol != st.get("from_sol")) or (
        st["family"] == "evm" and now_evm != st.get("from_evm")
    ):
        return False, "Your active wallet changed since you started — nothing sent. Run /withdraw again."
    raw, dest = st.get("raw"), st["dest"]
    if st["kind"] == "native":
        if st["family"] == "sol":
            ok, msg = withdraw.send_sol(sol_secret, dest, raw)
        elif st["family"] == "ton":
            ok, msg = withdraw.send_ton(sol_secret, dest, raw)
        else:
            ok, msg = withdraw.send_evm_native(evm_secret, st["chain"], dest, raw)
    elif st["family"] == "sol":
        ok, msg = withdraw.send_spl(sol_secret, st["mint"], dest, raw)
    else:
        ok, msg = withdraw.send_erc20(evm_secret, st["chain"], st["mint"], dest, raw)
    if ok is not False:
        try:
            db.log_trade(uid, "withdraw", st.get("mint") or st["sym"], st["chain"], 0.0, "withdraw")
        except Exception:
            pass
    return ok, msg


async def _withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    uid = update.effective_user.id
    bot = context.bot
    if update.effective_chat and update.effective_chat.type != "private":
        return
    st = context.user_data.get("wd")
    if data == "ab:list":
        text, kb = _addressbook_view(uid)
        await bot.send_message(uid, text, reply_markup=kb, parse_mode="HTML")
        return
    if data == "wd:save":
        await _withdraw_save_prompt(update, context)
        return
    if data.startswith("ab:del:"):
        try:
            db.delete_address(int(data.split(":")[2]), uid)
        except ValueError:
            pass
        text, kb = _addressbook_view(uid)
        try:
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        return
    if data == "wd:cancel":
        context.user_data["wd"] = {}
        await bot.send_message(uid, "Withdrawal cancelled. Nothing was sent.")
        return
    if data == "wd:spl":
        w = db.get_user_wallet(uid) or {}
        try:
            held = await asyncio.to_thread(signer.holdings_pub, w.get("sol_pub", ""))
        except Exception as exc:
            await bot.send_message(uid, f"Couldn't read tokens: {exc}")
            return
        if not held:
            await bot.send_message(uid, "No Solana tokens in this wallet.")
            return
        metas = await asyncio.to_thread(portfolio._ds_prices, [h["mint"] for h in held[:20]])
        kb = []
        for h in held[:12]:
            sym = (metas.get(h["mint"]) or {}).get("symbol") or h["mint"][:4] + "…"
            kb.append([InlineKeyboardButton(f"${sym} · {h['amount']:,.6g}", callback_data=f"wd:t:{h['mint']}")])
        kb.append([InlineKeyboardButton("✖️ Cancel", callback_data="wd:cancel")])
        await bot.send_message(uid, "🪙 Which token?", reply_markup=InlineKeyboardMarkup(kb))
        return
    if data == "wd:erc":
        context.user_data["wd"] = {"await": "erc"}
        await bot.send_message(uid, "🪙 Send the chain and token address, e.g.\n<code>base 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913</code>", parse_mode="HTML")
        return
    if data.startswith("wd:a:") or data.startswith("wd:t:"):
        try:
            if data.startswith("wd:a:"):
                key = data[5:]
                if key not in _WD_NATIVE:
                    return
                st = await asyncio.to_thread(_wd_select_native, uid, key)
            else:
                st = await asyncio.to_thread(_wd_select_spl, uid, data[5:])
        except Exception as exc:
            await bot.send_message(uid, f"Couldn't read that balance: {exc}")
            return
        if st["bal_raw"] <= 0:
            await bot.send_message(uid, f"No {st['sym']} to send in this wallet.")
            return
        context.user_data["wd"] = st
        await bot.send_message(uid, f"📤 {st['sym']} on {st['net']} — balance {_wd_ui(st)}.\nHow much?", reply_markup=_wd_amount_kb())
        return
    if not st or "family" not in st:
        if data.startswith("wd:"):
            await bot.send_message(uid, "That withdrawal expired. Start again with /withdraw.")
        return
    if data.startswith("wd:p:") or data == "wd:x":
        st.pop("nonce", None)  # amount is changing: any shown confirm is void
        st.pop("confirmed", None)
        st.pop("dest", None)
    if data.startswith("wd:p:"):
        pct = int(data[5:]) if data[5:].isdigit() else 100
        st["raw"] = None if pct >= 100 else st["bal_raw"] * pct // 100
        if st["raw"] is not None and st["raw"] <= 0:
            await bot.send_message(uid, "That rounds to zero. Pick a bigger share.")
            return
        await _wd_ask_dest(bot, uid, st)
        return
    if data == "wd:x":
        st["await"] = "amount"
        await bot.send_message(uid, f"Type the amount of {st['sym']} to send (you have {_wd_ui(st)}).")
        return
    if data.startswith("wd:d:"):
        book = db.get_address(int(data[5:]), uid) if data[5:].isdigit() else None
        if not book or book["family"] != st["family"]:
            await bot.send_message(uid, "That saved address isn't for this network.")
            return
        st["dest"] = book["address"]
        await _wd_confirm(bot, uid, st)
        return
    if data.startswith("wd:go:"):
        if not st.get("dest") or data[6:] != st.get("nonce") or st.get("confirmed") != _wd_snapshot(st):
            await bot.send_message(uid, "That confirmation is stale. Start again with /withdraw.")
            return
        context.user_data["wd"] = {"last_dest": st["dest"], "last_family": st["family"]}  # one tap = one send
        status = await _progress(bot, uid, "⏳ Sending…")
        try:
            ok, msg = await _off(uid, _wd_execute, uid, st, _busy=(False, BUSY_MSG))
        except Exception as exc:
            logger.exception("withdraw failed for %s", uid)
            ok, msg = None, (
                f"Something went wrong mid-send ({str(exc)[:120]}). It may or may not have gone out — "
                "check your wallet on the explorer before trying again."
            )
        icon = "🟢" if ok else ("🟡" if ok is None else "🔴")
        kb = None
        if ok is not False and not any(b["address"] == st["dest"] for b in db.list_addresses(uid, st["family"])):
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("💾 Save this address", callback_data="wd:save")]])
        await _done(bot, uid, status, f"{icon} {msg}", reply_markup=kb, disable_web_page_preview=True)
        return


async def _withdraw_save_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    st = context.user_data.get("wd") or {}
    if not st.get("last_dest"):
        return
    st["await"] = "label"
    await context.bot.send_message(update.effective_user.id, "Name this address (e.g. Coinbase, Ledger):")


async def _withdraw_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    """Handle typed input for an open withdrawal. True if consumed."""
    st = context.user_data.get("wd") or {}
    step = st.get("await")
    if not step:
        return False
    uid = update.effective_user.id
    msg = update.effective_message
    if step == "erc":
        parts = text.split()
        if len(parts) != 2 or not (resolve_chain(parts[0]) in {"base", "eth", "bsc", "arb"}):
            await msg.reply_text("Format: <chain> <0x token>. Chains: base, eth, bsc, arb.")
            return True
        cid = resolve_chain(parts[0])
        ok, tok = withdraw.validate_address("evm", parts[1])
        if not ok:
            await msg.reply_text(tok)
            return True
        try:
            new = await asyncio.to_thread(_wd_select_erc, uid, cid, tok)
        except Exception as exc:
            await msg.reply_text(f"Couldn't read that token: {exc}")
            return True
        if new["bal_raw"] <= 0:
            await msg.reply_text("No balance of that token on that chain in this wallet.")
            context.user_data["wd"] = {}
            return True
        context.user_data["wd"] = new
        await msg.reply_text(f"📤 {new['sym']} on {new['net']} — balance {_wd_ui(new)}.\nHow much?", reply_markup=_wd_amount_kb())
        return True
    if step == "amount":
        st.pop("nonce", None)
        st.pop("confirmed", None)
        from decimal import Decimal, InvalidOperation

        try:
            dec_amt = Decimal(text.replace(",", ""))
            if not dec_amt.is_finite():
                raise ValueError
            raw = int(dec_amt * (10 ** st["decimals"]))
        except (InvalidOperation, ValueError, OverflowError):
            await msg.reply_text("Send just a number, e.g. 0.5")
            return True
        if raw <= 0 or raw > st["bal_raw"]:
            await msg.reply_text(f"Pick an amount between 0 and {_wd_ui(st)}.")
            return True
        st["raw"] = raw
        await _wd_ask_dest(context.bot, uid, st)
        return True
    if step == "dest":
        ok, addr = withdraw.validate_address(st["family"], text)
        if not ok:
            await msg.reply_text(addr + " Paste it again, or /withdraw to restart.")
            return True
        st["dest"] = addr
        await _wd_confirm(context.bot, uid, st)
        return True
    if step == "label":
        db.save_address(uid, st["last_family"], text.strip()[:24] or "Saved", st["last_dest"])
        context.user_data["wd"] = {}
        await msg.reply_text("💾 Saved to your address book. /addressbook to manage.")
        return True
    return False


def _addressbook_view(uid: int) -> tuple[str, InlineKeyboardMarkup | None]:
    book = db.list_addresses(uid)
    if not book:
        return "📒 <b>Address book</b>\nEmpty. After a withdrawal, tap 💾 Save this address.", None
    names = {"sol": "Solana", "evm": "EVM", "ton": "TON"}
    lines = ["📒 <b>Address book</b>"]
    kb = []
    for b in book:
        lines.append(f"• {html.escape(b['label'])} ({names.get(b['family'], b['family'])})\n  <code>{html.escape(b['address'])}</code>")
        kb.append([InlineKeyboardButton(f"🗑 {b['label']}", callback_data=f"ab:del:{b['id']}")])
    return "\n".join(lines), InlineKeyboardMarkup(kb)


async def addressbook_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text, kb = _addressbook_view(update.effective_user.id)
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ------------------------------------------------------- migration sniper --
_MIG_PRIMED = False
_MIG_CYCLES = {
    "usd": [5, 10, 25, 50, 100],
    "max_top10": [20, 30, 40, 50],
    "max_per_day": [1, 3, 5, 10],
    "tp_pct": [0, 50, 100, 200],
    "sl_pct": [0, 20, 30, 50],
    "min_liq": [0, 5000, 15000, 30000],
}


def _mig_panel(uid: int) -> tuple[str, InlineKeyboardMarkup]:
    c = db.get_mig_config(uid)
    mode = c["mode"]
    mode_txt = {"off": "🔴 Off", "watch": "👀 Alerts only", "buy": "🟢 Auto-buy"}[mode if mode in ("off", "watch", "buy") else "off"]
    text = (
        "🎓 <b>Migration sniper</b>\n"
        "When a pump.fun coin graduates to its real pool, Ferzan checks it on-chain and "
        "(if you want) buys in the first seconds.\n\n"
        f"Mode: <b>{mode_txt}</b>\n"
        f"Buy size: ${float(c['usd']):g} · Max {int(c['max_per_day'])}/day\n"
        f"Filters: top-10 holders ≤ {float(c['max_top10']):g}% · liquidity ≥ ${float(c['min_liq']):,.0f}\n"
        "Always required: mint + freeze authority revoked, no trap extensions.\n"
        f"Auto exits: TP {'+' + format(float(c['tp_pct']), 'g') + '%' if c['tp_pct'] else 'off'} · "
        f"SL {'-' + format(float(c['sl_pct']), 'g') + '%' if c['sl_pct'] else 'off'}"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(("✅ " if mode == "off" else "") + "Off", callback_data="mig:m:off"),
            InlineKeyboardButton(("✅ " if mode == "watch" else "") + "Alerts", callback_data="mig:m:watch"),
            InlineKeyboardButton(("✅ " if mode == "buy" else "") + "Auto-buy", callback_data="mig:m:buy"),
        ],
        [
            InlineKeyboardButton(f"💵 ${float(c['usd']):g}", callback_data="mig:c:usd"),
            InlineKeyboardButton(f"🔁 {int(c['max_per_day'])}/day", callback_data="mig:c:max_per_day"),
        ],
        [
            InlineKeyboardButton(f"👥 Top10 ≤{float(c['max_top10']):g}%", callback_data="mig:c:max_top10"),
            InlineKeyboardButton(f"💧 Liq ≥${float(c['min_liq']) / 1000:g}k", callback_data="mig:c:min_liq"),
        ],
        [
            InlineKeyboardButton(f"🎯 TP {('+' + format(float(c['tp_pct']), 'g') + '%') if c['tp_pct'] else 'off'}", callback_data="mig:c:tp_pct"),
            InlineKeyboardButton(f"🛑 SL {('-' + format(float(c['sl_pct']), 'g') + '%') if c['sl_pct'] else 'off'}", callback_data="mig:c:sl_pct"),
        ],
    ])
    return text, kb


async def migsnipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text, kb = _mig_panel(update.effective_user.id)
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def _mig_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    uid = update.effective_user.id
    parts = data.split(":")
    if len(parts) < 3:
        return
    if parts[1] == "m":
        mode = parts[2]
        if mode == "buy":
            c = db.get_mig_config(uid)
            await context.bot.send_message(
                uid,
                f"⚠️ Auto-buy spends real money: up to ${float(c['usd']):g} × {int(c['max_per_day'])} a day "
                "from your active wallet, on brand-new coins that can still go to zero. Turn it on?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Yes, auto-buy", callback_data="mig:m:buyok"),
                    InlineKeyboardButton("Alerts only", callback_data="mig:m:watch"),
                ]]),
            )
            return
        if mode == "buyok":
            mode = "buy"
        if mode not in ("off", "watch", "buy"):
            return
        db.set_mig_config(uid, mode=mode)
    elif parts[1] == "c" and parts[2] in _MIG_CYCLES:
        key = parts[2]
        cycle = _MIG_CYCLES[key]
        cur = float(db.get_mig_config(uid)[key])
        nxt = next((v for v in cycle if v > cur), cycle[0])
        db.set_mig_config(uid, **{key: nxt})
    text, kb = _mig_panel(uid)
    try:
        if query and query.message and "Migration sniper" in (query.message.text or ""):
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
            return
    except Exception:
        pass
    await context.bot.send_message(uid, text, parse_mode="HTML", reply_markup=kb)


def _mig_card(m):
    from types import SimpleNamespace

    snap = SimpleNamespace(token_address=m.mint, chain="solana", liquidity_usd=m.liq_usd, dex=m.dex)
    return SimpleNamespace(snapshot=snap, score=100)


async def migration_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    global _MIG_PRIMED
    users = db.list_mig_users()
    if not users:
        _MIG_PRIMED = False  # re-prime when someone arms it: never act on a backlog
        return
    fetched_ok, migs = await asyncio.to_thread(migration.fetch_migrations)
    if not fetched_ok:
        return  # a failed poll must never count as "primed"
    if not _MIG_PRIMED:
        for m in migs:
            db.mig_mark_seen(m.pool, m.mint)
        _MIG_PRIMED = True
        return
    for m in migs:
        if not db.mig_mark_seen(m.pool, m.mint):
            continue
        rep = await asyncio.to_thread(rugcheck.sol_report, m.mint, True)
        await asyncio.gather(*(_mig_for_user(context, cfg, m, rep) for cfg in users), return_exceptions=True)


async def _mig_for_user(context: ContextTypes.DEFAULT_TYPE, cfg: dict, m, rep: dict) -> None:
    uid = int(cfg["user_id"])
    ok, _why = migration.passes_filters(m, rep, cfg)
    if not ok:
        return
    q = m.mint[:48]
    head = (
        f"🎓 <b>${html.escape(m.symbol)}</b> just graduated to {html.escape(m.dex or 'its pool')}\n"
        f"MC {_fmt_mc(m.fdv_usd)} · Liq {_fmt_mc(m.liq_usd)} · top 10 {float(rep.get('top10_pct') or 0):.0f}%\n"
        f"<code>{html.escape(m.mint)}</code>"
    )
    too_late = time.time() - float(m.created_ts or 0) > migration.MAX_BUY_AGE_S
    if cfg["mode"] != "buy" or too_late or _auto_trading_killed() or not signer.live_enabled():
        buys = [InlineKeyboardButton(f"🟢 {v:g} SOL", callback_data=f"bnv:{v:g}:{q}") for v in db.buy_presets(uid, "sol")[:3]]
        kb = InlineKeyboardMarkup([buys, [InlineKeyboardButton("📡 Full card", callback_data=f"sig:{q}")]])
        try:
            await context.bot.send_message(uid, head, parse_mode="HTML", reply_markup=kb)
        except Exception:
            logger.exception("migration alert failed for %s", uid)
        return
    if db.mig_fills_today(uid) >= int(cfg["max_per_day"]):
        return
    if not db.mig_fill(uid, m.mint):
        return
    usd = min(signer.max_usd(), max(1.0, float(cfg["usd"])))
    bought, msg = await _off(uid, _live_buy, uid, _mig_card(m), m.mint, True, usd)
    if not bought:
        db.mig_unfill(uid, m.mint)  # failures don't use up the daily cap
    elif cfg.get("tp_pct") or cfg.get("sl_pct"):
        db.set_live_exit(uid, m.mint, tp_pct=float(cfg["tp_pct"]) or None, sl_pct=float(cfg["sl_pct"]) or None)
    try:
        await context.bot.send_message(
            uid, head + "\n\n" + html.escape(msg), parse_mode="HTML", disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🎒 Open bag", callback_data=f"slc:{q}")]]) if bought else None,
        )
    except Exception:
        logger.exception("migration buy notify failed for %s", uid)


# ------------------------------------------------------ Mini App orders ----
async def webapp_order_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    orders = await asyncio.to_thread(db.claim_webapp_orders)
    for o in orders:
        asyncio.create_task(_run_webapp_order(context, o))
    # PnL cards asked for from the app: rendered here (the bot owns the
    # renderer) and sent to the user's chat, ready to forward.
    for c in await asyncio.to_thread(db.claim_card_requests):
        if _allowed(int(c["user_id"])):
            asyncio.create_task(_send_pnl_card(context.bot, int(c["user_id"]), c["mint"]))


def _webapp_trade(uid: int, o: dict) -> tuple[bool, str]:
    """Blocking. Runs under the user's trade lock (via _off)."""
    mint = o["mint"]
    if o["side"] == "sell":
        pct = max(1, min(100, int(o["amount"])))
        ok, msg, label = _sell_any(uid, mint, pct)
        return ok, _trade_result("sell", ok, label, msg, pct=pct)
    card = analyze(mint)
    if o["unit"] == "usd":
        usd = float(o["amount"])
    else:
        cid = resolve_chain(card.snapshot.chain or o.get("chain") or "") or ("sol" if _is_sol_mint(mint) else "base")
        px = _native_usd(cid)
        if px <= 0:
            return False, "Couldn't price the native coin right now — nothing sent."
        usd = float(o["amount"]) * px
    usd = min(signer.max_usd(), max(1.0, usd))
    return _live_buy(uid, card, mint, True, usd)


async def _run_webapp_order(context: ContextTypes.DEFAULT_TYPE, o: dict) -> None:
    uid = int(o["user_id"])
    try:
        if not _allowed(uid):
            ok, msg = False, "This desk is locked to an allowlist."
        else:
            ok, msg = await _off(uid, _webapp_trade, uid, o, _busy=(False, BUSY_MSG))
    except Exception as exc:
        logger.exception("webapp order %s failed", o.get("id"))
        ok, msg = False, f"Trade failed: {exc}"
    db.finish_webapp_order(o["id"], bool(ok), msg)
    try:
        await context.bot.send_message(uid, "📱 From the app\n" + msg, disable_web_page_preview=True)
    except Exception:
        pass


# ------------------------------------------------------------ daily recap --
RECAP_HOUR_UTC = int(os.getenv("FERZAN_RECAP_HOUR_UTC", "13"))


def _recap_text(uid: int, mark: bool = True) -> str | None:
    """Yesterday's desk in one message. None = nothing worth sending.
    Blocking (balances + marks)."""
    since = int(time.time()) - 86400
    trades = db.trades_since(uid, since)
    buys = [t for t in trades if t["side"] == "buy"]
    sells = [t for t in trades if t["side"] == "sell"]
    try:
        snap = portfolio.build_portfolio(uid)
    except LookupError:
        return None
    total = float(snap.get("total_usd") or 0)
    positions = snap.get("positions") or []
    if not trades and total < 1:
        return None
    lines = ["🗞 <b>Your Ferzan day</b>"]
    lines.append(f"💼 Desk value <b>${total:,.2f}</b>")
    prev = db.get_recap_mark(uid)
    if prev and prev.get("desk_usd") is not None:
        d = total - float(prev["desk_usd"])
        arrow = "▲" if d > 0 else "▼" if d < 0 else "•"
        lines[-1] += f"  {arrow} {'+' if d >= 0 else '−'}${abs(d):,.2f} vs last recap"
    if buys or sells:
        lines.append(f"🧾 {len(buys)} buy{'s' if len(buys) != 1 else ''} (${sum(float(t['usd'] or 0) for t in buys):,.2f} in) · "
                     f"{len(sells)} sell{'s' if len(sells) != 1 else ''}")
    costed = [p for p in positions if p.get("pnl") is not None]
    if costed:
        upnl = sum(p["pnl"] for p in costed)
        lines.append(f"📊 Open bags {'▲ +' if upnl >= 0 else '▼ −'}${abs(upnl):,.2f} unrealized")
        best = max(costed, key=lambda p: p.get("pnl_pct") or -1e9)
        worst = min(costed, key=lambda p: p.get("pnl_pct") or 1e9)
        if best.get("pnl_pct") is not None:
            lines.append(f"🏆 Best: ${html.escape(best['symbol'])} {best['pnl_pct']:+.1f}%")
        if worst is not best and worst.get("pnl_pct") is not None and worst["pnl_pct"] < 0:
            lines.append(f"🩹 Worst: ${html.escape(worst['symbol'])} {worst['pnl_pct']:+.1f}%")
    lines.append("<i>Value change includes deposits and withdrawals. Turn off: /settings.</i>")
    if mark:
        db.set_recap_mark(uid, dt.datetime.utcnow().strftime("%Y-%m-%d"), total)
    return "\n".join(lines)


async def recap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    status = await update.effective_message.reply_text("⏳ Adding up your day…")
    text = await asyncio.to_thread(_recap_text, uid, False)
    text = text or "Nothing to report yet — make a trade and check back."
    try:
        await status.edit_text(text, parse_mode="HTML", reply_markup=_recap_kb())
    except Exception:
        await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=_recap_kb())


def _recap_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎒 Bag", callback_data="go:bag"),
        InlineKeyboardButton("📸 PnL cards", callback_data="go:bag"),
    ]])


async def daily_recap_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    sem = asyncio.Semaphore(4)
    today = dt.datetime.utcnow().strftime("%Y-%m-%d")

    async def one(uid: int) -> None:
        if not db.flag_on(uid, "daily_recap", 1) or not _allowed(uid):
            return
        mark = db.get_recap_mark(uid)
        if mark and mark.get("day") == today:
            return  # already sent today (restart safety)
        async with sem:
            try:
                text = await asyncio.to_thread(_recap_text, uid)
            except Exception:
                logger.exception("recap build failed for %s", uid)
                return
        if text:
            try:
                await context.bot.send_message(uid, text, parse_mode="HTML", reply_markup=_recap_kb())
            except Exception:
                pass
            await asyncio.sleep(0.05)

    await asyncio.gather(*(one(u) for u in db.users_with_wallets()))


async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    alerts = db.get_all_active_alerts()
    if not alerts:
        return
    coin_ids = [a["coin_id"] for a in alerts]
    try:
        prices = await asyncio.to_thread(get_prices_usd, coin_ids)
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
    for notice in await asyncio.to_thread(trading.mark_open_positions):
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
                card = await asyncio.to_thread(analyze, name)
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
            events = await asyncio.to_thread(onchain.recent_activity, row["chain"], row["address"], 6)
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
            await context.bot.send_message(
                row["user_id"], "\n".join(lines)[:3500], disable_web_page_preview=True,
                reply_markup=_tracker_kb(int(row["user_id"]), row.get("chain") or "", fresh),
            )
        except Exception:
            logger.exception("wallet notify failed for %s", row["user_id"])
        try:
            await _copy_wallet_moves(context, row, fresh)
        except Exception:
            logger.exception("copy-trade failed for wallet #%s", row.get("id"))


def _tracker_kb(uid: int, chain: str, fresh: list) -> InlineKeyboardMarkup | None:
    """One-tap follow: buy what the wallet just bought, or open its card."""
    rows, seen = [], set()
    for ev in fresh:
        tok = getattr(ev, "token", "") or ""
        if not tok or tok in seen or getattr(ev, "kind", "") != "buy":
            continue
        seen.add(tok)
        cid = "sol" if _is_sol_mint(tok) else (resolve_chain(chain) or "base")
        unit = _NATIVE_UNIT.get(cid, "ETH")
        amt = db.buy_presets(uid, cid)[0]
        q = tok[:48]
        rows.append([
            InlineKeyboardButton(f"🟢 Buy {amt:g} {unit} too", callback_data=f"bnv:{amt:g}:{q}"),
            InlineKeyboardButton("📡 Card", callback_data=f"sig:{q}"),
        ])
        if len(rows) == 2:
            break
    return InlineKeyboardMarkup(rows) if rows else None


async def _copy_wallet_moves(context: ContextTypes.DEFAULT_TYPE, row: dict, fresh: list) -> None:
    """Copy-trade v2. Master switch = the user's global copy_live flag;
    each watched wallet must also have copy_on. Buys go through the normal
    live-buy path (rug / honeypot gates apply). A token already copied from
    this wallet isn't re-bought. Sells are mirrored only when copy_sells is
    on, and only for tokens this bot bought by copying THIS wallet."""
    uid = int(row["user_id"])
    wid = int(row["id"])
    if not db.flag_on(uid, "copy_live", 0) or not int(row.get("copy_on") or 0):
        return
    if _auto_trading_killed():
        return
    who = row.get("label") or (row.get("address") or "")[:8]
    # fresh is newest-first; act on at most one buy and one sell per poll
    buy_ev = next((ev for ev in fresh if ev.kind == "buy" and ev.token), None)
    sell_ev = next((ev for ev in fresh if ev.kind == "sell" and ev.token), None)
    if buy_ev and not db.has_copy_fill(uid, wid, buy_ev.token):
        mint = buy_ev.token
        usd = float(row.get("copy_usd") or 0) or _default_buy_usd(uid)
        usd = min(signer.max_usd(), max(1.0, usd))
        status = await _progress(context.bot, uid, f"👯 Copying {who}: buying ${usd:.0f}…")
        try:
            card = await asyncio.to_thread(analyze, mint)
            # force=False: copy buys respect the user's score floor like any
            # other automated buy (rug/honeypot gates apply either way).
            ok, msg = await _off(uid, _live_buy, uid, card, mint, False, usd)
        except Exception as exc:
            ok, msg = False, f"Copy buy skipped: {exc}"
        if ok:
            db.record_copy_fill(uid, wid, mint)
        await _done(context.bot, uid, status, f"👯 Copy {who}\n{msg}")
    if sell_ev and int(row.get("copy_sells") or 0) and db.has_copy_fill(uid, wid, sell_ev.token):
        mint = sell_ev.token
        status = await _progress(context.bot, uid, f"👯 {who} sold — mirroring…")
        ok, msg, label = await _off(uid, _sell_any, uid, mint, 100)
        if ok:
            db.clear_copy_fill(uid, wid, mint)
        await _done(context.bot, uid, status, f"👯 Copy {who}\n" + _trade_result("sell", ok, label, msg, pct=100))


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
        px = await asyncio.to_thread(_token_mark_usd, row["mint"])
        if px <= 0 or px > float(row["target_px"]):
            continue
        uid = int(row["user_id"])
        try:
            card = await asyncio.to_thread(analyze, row["mint"])
            ok, msg = await _off(uid, _live_buy, uid, card, row["mint"], True, float(row["usd"]))
        except Exception as exc:
            ok, msg = False, str(exc)
        # Only a real fill closes the limit; refusals and send failures leave
        # it armed so it retries on the next dip (previous prefix-matching
        # marked failed sends as filled).
        if ok:
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
            liq = await asyncio.to_thread(_token_liq_usd, mint)
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
                _ok, msg, _label = await _off(uid, _sell_any, uid, mint, 100)
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
    try:
        await _live_exit_job(context)
    except Exception:
        logger.exception("live exit job crashed; will retry next cycle")
        await _notify_admins(
            context.bot,
            "⚠️ live_exit_job crashed (see journalctl for the traceback). "
            "It will retry on the next 45s cycle, but any positions due to "
            "check this cycle were skipped.",
        )


EXIT_CONCURRENCY = int(os.getenv("EXIT_CONCURRENCY", "8"))
EXIT_MAX_FAILS = 5
_EXIT_FAILS: dict[tuple[int, str], int] = {}
_ERC20_DEC: dict[tuple[str, str], int] = {}


def _erc20_decimals(cid: str, token: str) -> int:
    """Real token decimals (cached). Never assume 18: a 6-decimal token
    valued as 18 reads as ~-100% and would trip a stop-loss instantly."""
    key = (cid, token.lower())
    if key not in _ERC20_DEC:
        body = evm_signer._rpc(CHAINS[cid]["rpc"], "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"])
        raw = body.get("result") or ""
        if not raw or raw == "0x":
            raise RuntimeError(f"no decimals() for {token} on {cid}")
        _ERC20_DEC[key] = min(max(int(raw, 16), 0), 36)
    return _ERC20_DEC[key]


def _exit_holdings(uid: int, mint: str) -> list[tuple[str, str, str, float]]:
    """Every wallet of this user holding `mint`: [(sol_secret, evm_secret,
    chain_id, token_amount)]. Blocking. Exits act on the whole position, in
    whichever wallets it sits, so switching wallets never strands a stop."""
    out = []
    for _sid, _lab, sol, evm in user_wallets.all_secrets(uid):
        if mint.startswith("0x"):
            from eth_account import Account

            addr = Account.from_key(evm if evm.startswith("0x") else "0x" + evm).address
            data = "0x70a08231" + addr[2:].lower().zfill(64)
            for cid in _EVM_SCAN:
                # Strict read: an RPC error must abort this exit cycle, never
                # count as 0 (a shrunken position = a false stop-loss).
                body = evm_signer._rpc(CHAINS[cid]["rpc"], "eth_call", [{"to": mint, "data": data}, "latest"])
                if body.get("error") or "result" not in body:
                    raise RuntimeError(f"balance read failed on {cid}: {str(body.get('error'))[:80]}")
                val = body.get("result") or "0x"
                raw = int(val, 16) if val not in ("0x", "") else 0
                if raw > 0:
                    out.append((sol, evm, cid, raw / 10 ** _erc20_decimals(cid, mint)))
        else:
            kp = signer.keypair_from_secret(sol)
            held = next((h for h in signer.holdings_pub(str(kp.pubkey()), strict=True) if h["mint"] == mint), None)
            if held and float(held.get("amount") or 0) > 0:
                out.append((sol, evm, "sol", float(held["amount"])))
    return out


def _exit_sell_all(uid: int, mint: str, holdings: list, pct: int) -> tuple[bool, str, float]:
    """Sell `pct`% in every holding wallet. Blocking — call via _off (one
    per-user lock around the whole exit). Returns (all_ok, messages,
    sold_share) where sold_share = fraction of the position (by amount) held
    in wallets whose sell succeeded — so a partial success is accounted
    exactly and never re-sold."""
    oks, msgs = [], []
    total = sum(h[3] for h in holdings) or 0.0
    sold_amt = 0.0
    for sol, evm, cid, amt in holdings:
        try:
            if mint.startswith("0x"):
                ok, msg = evm_signer.sell_evm(cid, mint, key_hex=evm, pct=pct)
            else:
                ok, msg = signer.sell_sol(
                    mint, secret=sol, pct=pct, slip_bps=_slip_bps(uid, "sell"), user_id=uid
                )
        except Exception as exc:
            ok, msg = False, str(exc)
        oks.append(bool(ok))
        msgs.append(msg)
        if ok:
            sold_amt += amt
    share = (sold_amt / total) if total > 0 else 0.0
    return (bool(oks) and all(oks)), "\n".join(msgs), share


async def _live_exit_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    # Parallel across users (bounded); each user's own sells still serialize
    # on their trade lock. A crash can't make one user's stop wait on others.
    sem = asyncio.Semaphore(EXIT_CONCURRENCY)

    async def run(row) -> None:
        uid = int(row["user_id"])
        mint = row["mint"]
        async with sem:
            try:
                await _live_exit_one(context, row, uid, mint)
            except Exception:
                logger.exception("live exit job: row failed for user=%s mint=%s", uid, mint)
                await _notify_admins(
                    context.bot,
                    f"⚠️ live_exit_job: exit check failed for user {uid}, mint {mint} "
                    "(see journalctl). Other users' positions were unaffected.",
                )

    await asyncio.gather(*(run(r) for r in db.list_live_exits()))


async def _exit_failed(context, uid: int, mint: str, what: str, msg: str) -> bool:
    """Count a failed exit sell. Returns True when we should give up (rule
    cleared after EXIT_MAX_FAILS in a row); otherwise the rule stays armed
    and retries next cycle."""
    key = (uid, mint)
    _EXIT_FAILS[key] = _EXIT_FAILS.get(key, 0) + 1
    n = _EXIT_FAILS[key]
    give_up = n >= EXIT_MAX_FAILS
    await _notify_admins(
        context.bot,
        f"⚠️ {what} sell FAILED ({n}/{EXIT_MAX_FAILS}) for user {uid}, mint {mint}: {msg}"
        + ("\nGiving up — rule cleared, position still open." if give_up else "\nRule kept; retrying next cycle."),
    )
    if n == 1 or give_up:
        try:
            await context.bot.send_message(
                uid,
                f"⚠️ {what} sell didn't go through: {msg[:300]}\n"
                + ("I've stopped retrying — check the token and sell manually from 📊 Bag."
                   if give_up else "Your rule stays armed and I'll retry automatically."),
            )
        except Exception:
            pass
    if give_up:
        _EXIT_FAILS.pop(key, None)
    return give_up


def _trail_hit(px: float, peak_px: float, trail_pct: float) -> bool:
    """Real trailing stop: price has fallen trail_pct % from its highest
    point since the trail was armed. (The old check used PnL points:
    +100% -> +80% is only a 10% drop in value.)"""
    return peak_px > 0 and px > 0 and px <= peak_px * (1 - float(trail_pct) / 100.0)


async def _live_exit_one(context: ContextTypes.DEFAULT_TYPE, row, uid: int, mint: str) -> None:
    cost = db.live_cost(uid, mint)
    if cost <= 0:
        return
    px = await asyncio.to_thread(_token_mark_usd, mint)
    if px <= 0:
        return
    try:
        holdings = await asyncio.to_thread(_exit_holdings, uid, mint)
    except Exception as exc:
        logger.warning("exit skipped this cycle for %s %s (balance read failed: %s)", uid, mint, exc)
        return  # never act on a guessed position size
    worth = sum(h[3] for h in holdings) * px
    if worth <= 0:
        return
    pnl_pct = ((worth - cost) / cost) * 100
    rungs = [] if _auto_trading_killed() else db.list_tp_rungs(uid, mint)
    for rung in rungs:
        if rung.get("hit") or pnl_pct < float(rung["pct"]):
            continue
        sell_pct = float(rung["sell_pct"])
        _rok, rmsg, share = await _off(uid, _exit_sell_all, uid, mint, holdings, int(sell_pct))
        label = f"TP rung +{float(rung['pct']):.0f}%"
        if share <= 0:
            if await _exit_failed(context, uid, mint, label, rmsg):
                db.mark_tp_rung_hit(uid, mint, rung["pct"])
            return  # nothing sold: re-measure and retry next cycle
        # Something sold: the rung is DONE (never re-sell a wallet that already
        # sold this rung). A wallet that failed is under-sold, not over-sold.
        db.mark_tp_rung_hit(uid, mint, rung["pct"])
        db.reduce_live_cost_pct(uid, mint, sell_pct * share)
        _log_trade_safe(uid, "sell", mint, "", worth * sell_pct / 100 * share, "tp_rung")
        if _rok:
            _EXIT_FAILS.pop((uid, mint), None)
        else:
            await _exit_failed(context, uid, mint, label + " (some wallets)", rmsg)
        try:
            await context.bot.send_message(uid, f"🎯 {label} hit — sold {sell_pct:.0f}% of bag\n{rmsg}")
        except Exception:
            logger.exception("tp ladder notify failed")
        cost = db.live_cost(uid, mint)
        if cost <= 0:
            return
        # Re-measure rather than estimate what's left after a (partial) sell.
        return
    hit = None
    trail = float(row.get("trail_pct") or 0)
    if pnl_pct > float(row.get("peak_pct") or -1e18):
        db.set_live_exit(uid, mint, peak_pct=pnl_pct)  # display only
    if trail > 0:
        # The trail follows the token PRICE, so adding to the bag (Buy more,
        # DCA) or a partial sell can't move it -- only the market can.
        peak_px = row.get("peak_px")
        if peak_px is None or px > float(peak_px):
            db.set_live_exit(uid, mint, peak_px=px)
            peak_px = px
        if _trail_hit(px, float(peak_px), trail):
            hit = "trail"
    if row.get("tp_pct") and pnl_pct >= float(row["tp_pct"]):
        hit = "tp"
    if row.get("sl_pct") and pnl_pct <= -float(row["sl_pct"]):
        hit = "sl"
    if not hit:
        return
    what = {"tp": "🎯 TP", "trail": "📉 Trail", "sl": "🛑 SL"}[hit]
    _ok, msg, share = await _off(uid, _exit_sell_all, uid, mint, holdings, 100)
    if not _ok:
        if share > 0:
            # Sold out of some wallets: drop exactly that share of the cost so
            # the next cycle compares the REMAINING bag against its own cost.
            db.reduce_live_cost_pct(uid, mint, 100.0 * share)
        if await _exit_failed(context, uid, mint, f"{what} exit ({pnl_pct:+.1f}%)", msg):
            db.clear_live_exit(uid, mint)
        return  # rule stays armed for what's left -> retried next cycle
    _EXIT_FAILS.pop((uid, mint), None)
    _log_trade_safe(uid, "sell", mint, "", worth, hit)
    db.clear_live_exit(uid, mint)
    db.clear_live_cost(uid, mint)
    try:
        await context.bot.send_message(uid, f"{what} hit ({pnl_pct:+.1f}%)\n{msg}")
    except Exception:
        logger.exception("live exit notify failed")


async def snipe_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for user_id, sid, status, msg in await asyncio.to_thread(sniper.scan_armed):
        if status != "filled":
            continue
        try:
            await context.bot.send_message(user_id, f"Snipe #{sid} filled\n{msg}")
        except Exception:
            logger.exception("snipe notify failed for %s", user_id)


async def dca_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _dca_job(context)
    except Exception:
        logger.exception("dca job crashed; will retry next cycle")
        await _notify_admins(
            context.bot,
            "⚠️ dca_job crashed (see journalctl for the traceback). Due plans "
            "were skipped this cycle; will retry next cycle.",
        )


async def _dca_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if _auto_trading_killed():
        return
    now = int(time.time())
    for plan in db.list_due_dca_plans(now):
        try:
            await _dca_run_one(context, plan)
        except Exception:
            logger.exception("dca job: plan #%s failed", plan["id"])
            await _notify_admins(
                context.bot,
                f"⚠️ DCA plan #{plan['id']} (user {plan['user_id']}, mint {plan['mint']}) "
                "failed unexpectedly (see journalctl).",
            )
        finally:
            # Always advance, even on failure -- a broken plan should skip a
            # cycle and retry later, not hammer the same error every 5 minutes.
            db.advance_dca_plan(plan["id"], now + int(plan["interval_seconds"]))


async def _dca_run_one(context: ContextTypes.DEFAULT_TYPE, plan: dict) -> None:
    uid = int(plan["user_id"])
    mint = plan["mint"]
    usd = float(plan["usd_per_buy"])
    try:
        card = await asyncio.to_thread(analyze, mint)
    except Exception as exc:
        try:
            await context.bot.send_message(
                uid, f"📅 DCA buy skipped for {mint[:10]}...: couldn't score it right now ({exc})."
            )
        except Exception:
            logger.exception("dca notify failed")
        return
    # force=False -- same score_gate / rug_buy / honeypot checks a manual
    # buy goes through. If the wallet is short on funds, the signer's own
    # error message (e.g. "insufficient balance") comes back here and gets
    # sent straight to the user below, same as any other failed live buy.
    msg = await _off(uid, _live_buy_followup, uid, card, mint, True, False, usd_override=usd)
    try:
        await context.bot.send_message(uid, f"📅 DCA buy ${usd:.0f}\n{msg}")
    except Exception:
        logger.exception("dca notify failed")


async def launch_feed_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _launch_feed_job(context)
    except Exception:
        logger.exception("launch feed job crashed; will retry next cycle")
        await _notify_admins(
            context.bot,
            "⚠️ launch_feed_job crashed (see journalctl for the traceback). "
            "Launch alerts and auto-buy-on-feed were skipped this cycle; will retry next cycle.",
        )


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
        diverse = _interesting(await asyncio.to_thread(_pool_for, "*"), False)
        for ln in diverse[:8]:
            cid = resolve_chain(ln.chain) or (ln.chain or "").lower()
            if cid and not db.flag_on(uid, f"feed_{cid}", 1):
                continue
            key = f"launch:{ln.chain}:{(ln.token or '')[:24]}"
            if not db.should_resend_signal(uid, key, 1, cooldown_s=45 * 60):
                continue
            text, markup = await asyncio.to_thread(launch_card, ln)
            try:
                await send_launch(context.bot, uid, text, markup, promo=False)
            except DeadChat as exc:
                # User blocked the bot / deleted their account: stop DMing them.
                # They get alerts back the moment they toggle them on again.
                logger.warning("launch DM to %s unreachable, turning alerts off: %s", uid, exc)
                db.update_user(uid, alerts_on=0)
                break
            except Exception:
                logger.exception("launch feed failed for %s", uid)
            if db.flag_on(uid, "auto_buy", 0) and not _auto_trading_killed():
                auto_usd = float(user.get("auto_buy_usd") or 0)
                if auto_usd > 0 and (ln.token or "").strip():
                    try:
                        auto_card = await asyncio.to_thread(analyze, ln.token)
                    except Exception:
                        auto_card = None
                    if auto_card is not None:
                        # force=False -- this still goes through the same
                        # score_gate / rug_buy / honeypot checks a manual
                        # paste does. Nothing here bypasses the user's flags.
                        auto_msg = await _off(
                            uid, _live_buy_followup, uid, auto_card, ln.token, True, False, usd_override=auto_usd
                        )
                        if auto_msg:
                            try:
                                await context.bot.send_message(uid, f"⚡️ Auto-buy ${auto_usd:.0f} (feed)\n{auto_msg}")
                            except Exception:
                                logger.exception("auto-buy notify failed for %s", uid)

    for chat_id, bind in binds:
        if _feed_muted(int(chat_id)):
            continue  # restricted chat, retried hourly; /setfeed there re-enables now
        rows = await asyncio.to_thread(_pool_for, bind)
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
            text, markup = await asyncio.to_thread(launch_card, ln)
            try:
                await send_launch(context.bot, chat_id, text, markup, promo=False)
                sent += 1
            except ChatMoved as moved:
                await _feed_chat_moved(context.bot, int(chat_id), moved.new_chat_id)
                break
            except DeadChat as exc:
                await _feed_chat_dead(context.bot, int(chat_id), bind, str(exc), exc.permanent)
                break  # don't hammer the rest of this chat's cards
            except Exception:
                logger.exception("channel feed failed for %s", chat_id)
        if sent and db.feed_fail_count(int(chat_id)):
            db.clear_feed_failure(int(chat_id))
        logger.info("feed chat=%s bind=%s rows=%s sent=%s", chat_id, bind, len(rows), sent)


async def _feed_chat_dead(bot, chat_id: int, bind: str, err: str, permanent: bool = True) -> None:
    fails = db.note_feed_failure(chat_id, err)
    logger.warning("feed chat %s unreachable (%s/%s): %s", chat_id, fails, FEED_MUTE_AFTER, err)
    if permanent and fails >= FEED_MUTE_AFTER:
        pass  # gone for good (even if it was only "restricted" earlier) — drop below
    elif fails != FEED_MUTE_AFTER:
        return  # restricted: admins get told once, at the threshold
    if permanent:
        db.drop_feed_chat(chat_id)
        await _notify_admins(
            bot,
            f"🔇 Feed chat {chat_id} (bind {bind}) removed after {fails} failures: {err}\n"
            "The chat is gone or the bot was removed. Add the bot back and run /setfeed there to restore it.",
        )
    else:
        await _notify_admins(
            bot,
            f"⏸ Feed chat {chat_id} (bind {bind}) paused: {err}\n"
            "The bot lost send rights there. Bind kept — it retries hourly, or run /setfeed there once fixed.",
        )


async def _feed_chat_moved(bot, old_id: int, new_id: int) -> None:
    db.migrate_feed_chat(old_id, new_id)
    logger.warning("feed chat %s upgraded to supergroup %s — binds moved", old_id, new_id)
    await _notify_admins(bot, f"🔁 Feed chat {old_id} became supergroup {new_id}; feed moved automatically.")


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
    prices = await asyncio.to_thread(_native_prices)
    if not prices:
        return
    for chat_id, bind in db.list_feed_binds():
        if bind in {"*", ""}:
            continue
        if _feed_muted(int(chat_id)):
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
        text, markup = await asyncio.to_thread(launch_card, ln)
        text += "\n<i>Chain pulse · every 10m · Buy opens the desk</i>"
        try:
            await send_launch(context.bot, chat_id, text, markup, promo=False)
        except ChatMoved as moved:
            await _feed_chat_moved(context.bot, int(chat_id), moved.new_chat_id)
        except DeadChat as exc:
            await _feed_chat_dead(context.bot, int(chat_id), bind, str(exc), exc.permanent)
        except Exception:
            logger.exception("native pulse failed for %s", chat_id)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env before running.")

    db.init_db()

    async def _post_init(application: Application) -> None:
        # Network-bound trade/quote calls run in this pool (asyncio.to_thread).
        # The stdlib default is cpu_count+4 — ~5 threads on a small droplet.
        from concurrent.futures import ThreadPoolExecutor

        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(
                max_workers=int(os.getenv("FERZAN_WORKER_THREADS", "32")),
                thread_name_prefix="ferzan-io",
            )
        )
        try:
            me = await application.bot.get_me()
            if me.username:
                os.environ["FERZAN_BOT_USERNAME"] = me.username
        except Exception:
            logger.exception("could not cache bot username")
        try:
            if _webapp_url():
                # The menu button opens a chooser first: the app, or classic
                # chat mode (/start + commands) for people who don't want it.
                sep = "&" if "?" in _webapp_url() else "?"
                menu_url = f"{_webapp_url()}{sep}from=menu&bot={os.getenv('FERZAN_BOT_USERNAME', '')}"
                await application.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="⚡ Ferzan", web_app=WebAppInfo(url=menu_url))
                )
        except Exception:
            logger.exception("set_chat_menu_button failed")
        try:
            await application.bot.set_my_commands(
                [
                    BotCommand("start", "Home"),
                    BotCommand("tour", "Quick 3-step tour"),
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
                    BotCommand("wallets", "Copy trading"),
                    BotCommand("livesell", "Sell a live Solana mint"),
                    BotCommand("settings", "Risk vault"),
                    BotCommand("withdraw", "Send funds out"),
                    BotCommand("alerts", "Token alerts"),
                    BotCommand("migsnipe", "Graduation sniper"),
                    BotCommand("presets", "Quick-buy amounts"),
                    BotCommand("recap", "Your day"),
                    BotCommand("referral", "Invite + earn"),
                ]
            )
        except Exception:
            logger.exception("set_my_commands failed")

    # concurrent_updates: without it PTB handles ONE update at a time, so a
    # user waiting on a buy blocks every other user's taps. Same-user trade
    # sends are still serialized by _user_lock() inside _off().
    app = (
        Application.builder()
        .token(token)
        .post_init(_post_init)
        .concurrent_updates(int(os.getenv("FERZAN_CONCURRENT_UPDATES", "32")))
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tour", tour_cmd))
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
    app.add_handler(CommandHandler("killswitch", killswitch_cmd))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("walletname", walletname_cmd))
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
    app.add_handler(CommandHandler("copy", copy_cmd))
    app.add_handler(CommandHandler("unwatchwallet", unwatchwallet_cmd))
    app.add_handler(CommandHandler("drawdown", drawdown_cmd))
    app.add_handler(CommandHandler("fees", fees_cmd))
    app.add_handler(CommandHandler("signer", signer_cmd))
    app.add_handler(CommandHandler("bag", bag_cmd))
    app.add_handler(CommandHandler("tp", tp_cmd))
    app.add_handler(CommandHandler("sl", sl_cmd))
    app.add_handler(CommandHandler("tpladder", tpladder_cmd))
    app.add_handler(CommandHandler("dca", dca_cmd))
    app.add_handler(CommandHandler("trail", trail_cmd))
    app.add_handler(CommandHandler("stake", stake_cmd))
    app.add_handler(CommandHandler("lpguard", lpguard_cmd))
    app.add_handler(CommandHandler("buylimit", buylimit_cmd))
    app.add_handler(CommandHandler("limits", limits_cmd))
    app.add_handler(CommandHandler("cancellimit", cancellimit_cmd))
    app.add_handler(CommandHandler("livesell", livesell_cmd))
    app.add_handler(CommandHandler("livesellevm", livesellevm_cmd))
    app.add_handler(CommandHandler("treasury", treasury_cmd))
    app.add_handler(CommandHandler("health", health_cmd))
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
    app.add_handler(CommandHandler("withdraw", withdraw_cmd))
    app.add_handler(CommandHandler("send", withdraw_cmd))
    app.add_handler(CommandHandler("addressbook", addressbook_cmd))
    app.add_handler(CommandHandler("alerts", alerts_cmd))
    app.add_handler(CommandHandler("migsnipe", migsnipe_cmd))
    app.add_handler(CommandHandler("migrations", migsnipe_cmd))
    app.add_handler(CommandHandler("presets", presets_cmd))
    app.add_handler(CommandHandler("recap", recap_cmd))
    app.add_handler(CommandHandler("tracker", wallets_cmd))
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
        jq.run_repeating(dca_job, interval=300, first=90)
        jq.run_repeating(token_alert_job, interval=60, first=30)
        jq.run_repeating(migration_job, interval=int(os.getenv("MIG_POLL_SECONDS", "20")), first=45)
        jq.run_repeating(webapp_order_job, interval=2, first=10)
        jq.run_daily(daily_recap_job, time=dt.time(hour=RECAP_HOUR_UTC, minute=0, tzinfo=dt.timezone.utc))
        db.fail_stuck_webapp_orders(max_running_s=0)  # a restart orphans 'running' app orders
    else:
        logger.warning("job-queue extra missing; commands still work, scanners off")

    logger.info("FERZAN starting")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
