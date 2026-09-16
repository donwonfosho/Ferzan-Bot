"""Ferzan Buy — channel buy alerts. Token: BUYBOT_TOKEN in .env"""
from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
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

load_dotenv("/opt/ferzan/.env")
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s buybot %(message)s")
log = logging.getLogger("buybot")

DB = Path(os.getenv("BUYBOT_DB", "/opt/ferzan/app/buybot.db"))
TRADE = (os.getenv("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")
CHAT = os.getenv("FERZAN_CHAT") or "https://t.me/Ferzan_Chat"
HUB = os.getenv("FERZAN_HUB_URL") or "https://t.me/Ferzan_Trade_Ecosystem"
RAID_CH = (os.getenv("FERZAN_RAID_CHAT") or "@Ferzan_Raid").strip()
TRENDING_CH = (os.getenv("FERZAN_TRENDING_CHAT") or "@Ferzan_Trending").strip()
TREASURY_SOL = (os.getenv("FEE_WALLET_SOL") or os.getenv("PLATFORM_TREASURY_SOL") or "").strip()
TREASURY_EVM = (os.getenv("FEE_WALLET_EVM") or os.getenv("PLATFORM_TREASURY_EVM") or "").strip()
LAST_MEDIA: dict = {}
SETGIF_WAIT: set = set()
MIN_USD = float(os.getenv("BUYBOT_MIN_USD") or "15")
EMOJI_PACK = (os.getenv("FERZAN_EMOJI_PACK") or "FerzanBuyBot").strip()
_PACK_IDS: list[str] = []
_PACK_FACE: list[str] = []


def _slot(name: str, default: int) -> int:
    raw = os.getenv(f"FERZAN_EMOJI_{name}", "")
    if raw.isdigit():
        return int(raw)
    return default


def _ce(i: int, fallback: str) -> str:
    if 0 <= i < len(_PACK_IDS) and _PACK_IDS[i]:
        return f'<tg-emoji emoji-id="{_PACK_IDS[i]}">{fallback}</tg-emoji>'
    return fallback


def _icon(name: str, default: int, fallback: str) -> str:
    return _ce(_slot(name, default), fallback)


def _face(i: int = 0) -> str:
    if 0 <= i < len(_PACK_FACE) and _PACK_FACE[i]:
        return _PACK_FACE[i]
    return ""


async def _load_pack(bot) -> None:
    global _PACK_IDS, _PACK_FACE
    try:
        st = await bot.get_sticker_set(EMOJI_PACK)
        _PACK_IDS, _PACK_FACE = [], []
        for s in st.stickers or []:
            cid = getattr(s, "custom_emoji_id", None)
            if not cid:
                continue
            _PACK_IDS.append(cid)
            _PACK_FACE.append(getattr(s, "emoji", None) or "")
        log.info("emoji pack %s loaded %s icons", EMOJI_PACK, len(_PACK_IDS))
    except Exception as exc:
        log.warning("emoji pack %s: %s", EMOJI_PACK, exc)
        _PACK_IDS, _PACK_FACE = [], []

GT_NET = {
    "sol": "solana",
    "solana": "solana",
    "eth": "eth",
    "ethereum": "eth",
    "base": "base",
    "bsc": "bsc",
    "bnb": "bsc",
    "arb": "arbitrum",
    "arbitrum": "arbitrum",
    "avax": "avax",
    "pol": "polygon",
    "polygon": "polygon",
    "pump": "solana",
    "pumpfun": "solana",
}


def _db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS watches (
            chat_id INTEGER,
            chain TEXT,
            ca TEXT,
            pool TEXT,
            last_ts INTEGER DEFAULT 0,
            min_usd REAL DEFAULT 15,
            PRIMARY KEY (chat_id, chain, ca)
        )"""
    )
    try:
        con.execute("ALTER TABLE watches ADD COLUMN min_usd REAL DEFAULT 15")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE watches ADD COLUMN emoji TEXT DEFAULT '🟢'")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE watches ADD COLUMN tg_url TEXT")
    except sqlite3.OperationalError:
        pass
    for col in ("discord_url", "x_url"):
        try:
            con.execute(f"ALTER TABLE watches ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass
    con.execute(
        """CREATE TABLE IF NOT EXISTS chat_flags (
            chat_id INTEGER PRIMARY KEY,
            tape INTEGER DEFAULT 1,
            mute_until INTEGER DEFAULT 0
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS buy_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            ca TEXT,
            usd REAL,
            ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS raids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            url TEXT,
            note TEXT,
            created INTEGER,
            likes_t INTEGER DEFAULT 5,
            rt_t INTEGER DEFAULT 5,
            re_t INTEGER DEFAULT 2,
            likes_h INTEGER DEFAULT 0,
            rt_h INTEGER DEFAULT 0,
            re_h INTEGER DEFAULT 0,
            ends INTEGER DEFAULT 0,
            cashtag TEXT,
            active INTEGER DEFAULT 1,
            msg_id INTEGER DEFAULT 0
        )"""
    )
    for col, spec in (
        ("likes_t", "INTEGER DEFAULT 5"),
        ("rt_t", "INTEGER DEFAULT 5"),
        ("re_t", "INTEGER DEFAULT 2"),
        ("likes_h", "INTEGER DEFAULT 0"),
        ("rt_h", "INTEGER DEFAULT 0"),
        ("re_h", "INTEGER DEFAULT 0"),
        ("ends", "INTEGER DEFAULT 0"),
        ("cashtag", "TEXT"),
        ("active", "INTEGER DEFAULT 1"),
        ("msg_id", "INTEGER DEFAULT 0"),
    ):
        try:
            con.execute(f"ALTER TABLE raids ADD COLUMN {col} {spec}")
        except sqlite3.OperationalError:
            pass
    con.execute(
        """CREATE TABLE IF NOT EXISTS raid_taps (
            raid_id INTEGER,
            user_id INTEGER,
            kind TEXT,
            PRIMARY KEY (raid_id, user_id, kind)
        )"""
    )
    for col, spec in (("last_ping", "INTEGER DEFAULT 0"), ("ping_min", "INTEGER DEFAULT 5")):
        try:
            con.execute(f"ALTER TABLE raids ADD COLUMN {col} {spec}")
        except sqlite3.OperationalError:
            pass
    con.execute(
        """CREATE TABLE IF NOT EXISTS raid_scores (
            chat_id INTEGER,
            user_id INTEGER,
            name TEXT,
            pts INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS media (
            chat_id INTEGER PRIMARY KEY,
            kind TEXT,
            file_id TEXT
        )"""
    )
    return con


def _media(chat_id: int):
    con = _db()
    row = con.execute("SELECT kind, file_id FROM media WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return row


def _watch(chat_id: int):
    con = _db()
    row = con.execute(
        "SELECT chain, ca, pool, min_usd FROM watches WHERE chat_id=? ORDER BY last_ts DESC",
        (chat_id,),
    ).fetchone()
    con.close()
    return row


DS_CHAIN = {
    "sol": "solana",
    "solana": "solana",
    "pump": "solana",
    "pumpfun": "solana",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "base": "base",
    "bsc": "bsc",
    "bnb": "bsc",
    "arb": "arbitrum",
    "arbitrum": "arbitrum",
    "avax": "avalanche",
    "pol": "polygon",
    "polygon": "polygon",
}


def _ds(ca: str, chain: str | None = None) -> dict:
    want = DS_CHAIN.get((chain or "").lower(), "")
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=12)
        pairs = (r.json() or {}).get("pairs") or []
        if want:
            pairs = [p for p in pairs if str(p.get("chainId") or "").lower() == want]
        if pairs:
            return pairs[0]
        r = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={ca}", timeout=12)
        pairs = (r.json() or {}).get("pairs") or []
        if want:
            pairs = [p for p in pairs if str(p.get("chainId") or "").lower() == want]
        return pairs[0] if pairs else {}
    except Exception:
        return {}


def _esc(s: str) -> str:
    return html.escape(str(s or ""), quote=False)


def _gt(path: str) -> dict | list | None:
    try:
        r = requests.get(
            "https://api.geckoterminal.com/api/v2" + path,
            timeout=12,
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _pool_for(chain: str, ca: str) -> tuple[str, dict]:
    p = _ds(ca, chain)
    if p:
        pool = p.get("pairAddress") or ""
        base = p.get("baseToken") or {}
        dex = (p.get("dexId") or "").lower()
        attrs = {
            "name": base.get("name") or base.get("symbol"),
            "symbol": base.get("symbol"),
            "address": base.get("address") or ca,
            "dex": dex,
            "source": "dexscreener",
        }
        if pool:
            return pool, attrs
    net = GT_NET.get(chain, chain)
    data = _gt(f"/networks/{net}/tokens/{ca}/pools?page=1")
    rows = (data or {}).get("data") or []
    if not rows:
        return "", {}
    row = rows[0]
    pid = row.get("id") or ""
    pool = pid.split("_", 1)[-1] if "_" in str(pid) else str(pid)
    return pool, (row.get("attributes") or {})


def _trades(net: str, pool: str, last_ts: int) -> list[dict]:
    data = _gt(f"/networks/{net}/pools/{pool}/trades?trade_volume_in_usd_greater_than=1")
    rows = (data or {}).get("data") or []
    out = []
    for row in rows:
        a = row.get("attributes") or {}
        if str(a.get("kind") or "").lower() != "buy":
            continue
        ts = a.get("block_timestamp") or a.get("timestamp") or ""
        try:
            if "T" in str(ts):
                from datetime import datetime, timezone

                epoch = int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp())
            else:
                epoch = int(float(ts))
        except Exception:
            epoch = int(time.time())
        if epoch <= last_ts:
            continue
        out.append({"ts": epoch, **a})
    out.sort(key=lambda x: x["ts"])
    return out


def _tier(usd: float) -> tuple[str, str]:
    if usd < 25:
        return "SIP", "🦍 sip"
    if usd < 150:
        return "APE", "🦍 APE NOW"
    return "SEND", "🦍 SEND IT"


def _bar(usd: float, emoji: str = "🟢") -> str:
    em = (emoji or "🟢").strip()[:8] or "🟢"
    if usd < 25:
        n = 3
    elif usd < 80:
        n = 6
    else:
        n = 8
    return em * min(n, 8)


def _holders(chain: str, ca: str, pair: dict) -> str:
    n = pair.get("holders") or (pair.get("info") or {}).get("holders")
    if n:
        try:
            return f"{int(n):,}"
        except (TypeError, ValueError):
            pass
    net = GT_NET.get(chain, chain)
    data = _gt(f"/networks/{net}/tokens/{ca}")
    attrs = ((data or {}).get("data") or {}).get("attributes") or {}
    for key in ("holders", "holder_count", "unique_holders"):
        if attrs.get(key):
            try:
                return f"{int(float(attrs[key])):,}"
            except (TypeError, ValueError):
                pass
    if str(chain).lower() in {"eth", "ethereum"} and ca.startswith("0x"):
        try:
            r = requests.get(
                f"https://api.ethplorer.io/getTokenInfo/{ca}",
                params={"apiKey": "freekey"},
                timeout=10,
            )
            hc = (r.json() or {}).get("holdersCount")
            if hc:
                return f"{int(hc):,}"
        except Exception:
            pass
    return ""


def _usd(v) -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "—"
    if x >= 1_000_000:
        return f"${x/1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x:,.0f}"
    if x <= 0:
        return "—"
    return f"${x:,.2f}"


def _card(chain: str, ca: str, tr: dict, attrs: dict, emoji: str = "🟢", tg_url: str = "", cluster: int = 1, discord_url: str = "", x_url: str = "") -> tuple[str, InlineKeyboardMarkup]:
    usd = float(tr.get("volume_in_usd") or 0)
    got = tr.get("to_token_amount") or tr.get("to_token_output") or ""
    spent = tr.get("from_token_amount") or ""
    buyer = tr.get("tx_from_address") or tr.get("origin_from_address") or ""
    tx = tr.get("tx_hash") or ""
    pair = _ds(ca)
    name = attrs.get("name") or (pair.get("baseToken") or {}).get("name") or ca[:8]
    sym = attrs.get("symbol") or (pair.get("baseToken") or {}).get("symbol") or name
    mc = (
        attrs.get("fdv_usd")
        or attrs.get("market_cap_usd")
        or pair.get("marketCap")
        or pair.get("fdv")
        or ""
    )
    net = GT_NET.get(chain, chain)
    ds = pair.get("url") or f"https://dexscreener.com/{net}/{ca}"
    info = pair.get("info") or {}
    tg = (tg_url or "").strip()
    for s in info.get("socials") or []:
        if tg:
            break
        if str(s.get("type") or "").lower() in ("telegram", "tg"):
            tg = s.get("url") or ""
            break
    buy = f"https://t.me/{TRADE}?start={ca}"
    scan = {
        "sol": f"https://solscan.io/tx/{tx}",
        "solana": f"https://solscan.io/tx/{tx}",
        "base": f"https://basescan.org/tx/{tx}",
        "eth": f"https://etherscan.io/tx/{tx}",
        "ethereum": f"https://etherscan.io/tx/{tx}",
        "bsc": f"https://bscscan.com/tx/{tx}",
        "arb": f"https://arbiscan.io/tx/{tx}",
    }.get(chain, ds)
    buyer_url = scan.replace("/tx/", "/address/") if buyer and "/tx/" in scan else ds
    liq = (os.getenv("FERZAN_LIQ_BOT") or "FerzanLiqBot").lstrip("@")
    boost = f"https://t.me/{liq}"
    chat = tg or os.getenv("FERZAN_CHAT_URL") or "https://t.me/Ferzan_Trade_Ecosystem"
    def _num(v):
        try:
            x = float(v)
            return f"{x:,.2f}" if x < 1000 else f"{x:,.0f}"
        except (TypeError, ValueError):
            return str(v or "")
    spent_s = _num(spent) if spent else f"{usd:,.2f}"
    got = _num(got) if got else got
    tag, label = _tier(usd)
    dex_name = (pair.get("dexId") or attrs.get("dex") or "").title()
    liq_usd = _usd((pair.get("liquidity") or {}).get("usd"))
    holders = _holders(chain, ca, pair) or attrs.get("holders") or ""
    xurl = ""
    web = ""
    for s in info.get("socials") or []:
        typ = str(s.get("type") or "").lower()
        if typ in ("twitter", "x") and not xurl:
            xurl = s.get("url") or ""
    for w in info.get("websites") or []:
        web = w.get("url") or web
    chg = pair.get("priceChange") or {}
    def _pct(key):
        try:
            x = float(chg.get(key) or 0)
            return f"{'+' if x >= 0 else ''}{x:.1f}%"
        except (TypeError, ValueError):
            return "—"
    age = ""
    created = pair.get("pairCreatedAt") or pair.get("createdAt")
    try:
        created = int(created)
        if created > 10_000_000_000:
            created //= 1000
        sec = max(0, int(time.time()) - created)
        if sec < 3600:
            age = f"{sec // 60}m"
        elif sec < 86400:
            age = f"{sec // 3600}h"
        else:
            age = f"{sec // 86400}d"
    except (TypeError, ValueError):
        age = ""
    tax = attrs.get("buy_tax") or attrs.get("sell_tax") or ""
    locked = ""
    labels = [str(x).lower() for x in (pair.get("labels") or [])]
    if any("lock" in x for x in labels):
        locked = "LP lock"
    top10 = attrs.get("top10") or attrs.get("top_10_holders") or ""
    flags = []
    if any(x in labels for x in ("honeypot", "scam")):
        flags.append("⚠ honeypot flag")
    if any("bundle" in x for x in labels):
        flags.append("bundled")
    if str(attrs.get("dev_sold") or "").lower() in {"1", "true", "yes"}:
        flags.append("dev sold")
    lines = [
        f"<b>{_esc(name)}</b>  [${_esc(sym)}]  ·  {_esc(str(chain).upper())}",
        f"{_esc(label)}",
        _bar(usd, emoji),
        f"<code>{_esc(ca)}</code>",
        "<i>tap CA to copy</i>",
        "",
        f"{_icon('USD', 1, '💵')}  {_esc(spent_s)}   (${usd:,.2f})",
        f"{_icon('BAG', 2, '🎒')}  Got: {_esc(got)} {_esc(sym)}",
        f"{_icon('MC', 3, '🧢')}  Market cap: {_usd(mc)}",
        f"{_icon('LIQ', 4, '💧')}  Liquidity: {liq_usd}",
    ]
    if age or chg:
        lines.append(f"⏱  {age or '—'}   5m {_pct('m5')}   1h {_pct('h1')}")
    if tax:
        lines.append(f"🧾  Tax: {_esc(str(tax))}")
    if locked:
        lines.append(f"🔒  {locked}")
    if top10:
        lines.append(f"📊  Top 10: {_esc(str(top10))}")
    if flags:
        lines.append("⚠  " + " · ".join(flags))
    if dex_name:
        lines.append(f"{_icon('ROUTE', 5, '🛣')}  Route: {_esc(dex_name)}")
    if cluster and cluster > 1:
        lines.append(f"🔥  {cluster} buys in 12s")
    if holders:
        lines.append(f"{_icon('HOLD', 7, '👥')}  Holders: {_esc(str(holders))}")
    links = f"{_icon('BUYER', 6, '👤')}  <a href=\"{_esc(buyer_url)}\">Buyer</a>  ·  <a href=\"{_esc(scan)}\">Txn</a>"
    if tg:
        links += f"  ·  {_icon('TG', 8, '💬')} <a href=\"{_esc(tg)}\">Telegram</a>"
    xurl = (x_url or "").strip() or xurl
    disc = (discord_url or "").strip()
    if xurl:
        links += f"  ·  <a href=\"{_esc(xurl)}\">X</a>"
    if disc:
        links += f"  ·  <a href=\"{_esc(disc)}\">Discord</a>"
    lines.append(links)
    lines.append("")
    lines.append("<i>See it. Ape it. Send it.</i>")
    lines.append(f'{_icon("TITLE", 0, "⚡")} <a href="{_esc(HUB)}">FERZAN ECO HUB</a>')
    text = "\n".join(lines)
    hub = HUB
    rows = [
        [
            InlineKeyboardButton("Buy", url=buy),
            InlineKeyboardButton("Chart", url=ds),
            InlineKeyboardButton("Eco Hub", url=hub),
        ],
        [
            InlineKeyboardButton("0.05", url=buy),
            InlineKeyboardButton("0.1", url=buy),
            InlineKeyboardButton("0.25", url=buy),
        ],
        [InlineKeyboardButton("See it. Ape it. Send it.", url=buy)],
        [InlineKeyboardButton("Boost this alert", url=boost)],
    ]
    kb = InlineKeyboardMarkup(rows)
    return text, kb


SETUP_CHAIN, SETUP_CA, SETUP_MIN, SETUP_EMOJI, SETUP_TG = range(5)

CHAIN_BTNS = [
    [InlineKeyboardButton("◎ Solana", callback_data="su:sol"),
     InlineKeyboardButton("Ξ Ethereum", callback_data="su:eth")],
    [InlineKeyboardButton("🔵 Base", callback_data="su:base"),
     InlineKeyboardButton("🟡 BNB", callback_data="su:bsc")],
    [InlineKeyboardButton("🔵 Arbitrum", callback_data="su:arb"),
     InlineKeyboardButton("🔺 Avalanche", callback_data="su:avax")],
    [InlineKeyboardButton("🟣 Polygon", callback_data="su:pol"),
     InlineKeyboardButton("💊 Pump.fun", callback_data="su:sol")],
]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "⚡ Ferzan Buy — channel buy alerts for any project chat.\n\n"
        "Dev setup (easiest):\n"
        "/setup  — I ask chain → CA → min buy $ → bar emoji\n\n"
        "Or one line: /add base 0xCA 25\n"
        "/settings  current pair + min\n"
        "/setemoji 🚕  buy-size bar\n"
        "/setgif  buy card GIF\n"
        "/untrack  stop alerts\n"
        "/preview  fake card   /status\n"
        "/tape on|off   /mute 1h   /min 50   /who\n"
        "/chart  token card + Dex chart\n"
        "/stats /price /dex /market /vote /raid\n"
        "/help"
    )


async def setup_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.effective_message.reply_text(
            "Add @Ferzan_Buy_Bot to the project channel first, then run /setup there."
        )
        return ConversationHandler.END
    context.user_data["setup"] = {"chat_id": chat.id}
    await update.effective_message.reply_text(
        "⚡ Ferzan Buy setup\n\nWhich chain is this token on?",
        reply_markup=InlineKeyboardMarkup(CHAIN_BTNS),
    )
    return SETUP_CHAIN


async def setup_chain(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chain = (q.data or "").split(":")[-1]
    if chain not in GT_NET:
        await q.edit_message_text("Pick a chain button.")
        return SETUP_CHAIN
    context.user_data.setdefault("setup", {})["chain"] = chain
    await q.edit_message_text(
        f"Chain: {chain.upper()}\n\nPaste the token contract / mint (CA)."
    )
    return SETUP_CA


async def setup_ca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ca = "".join((update.message.text or "").split())
    if ca.startswith("0x") or ca.startswith("0X"):
        ca = "0x" + ca[2:]
        if len(ca) != 42:
            await update.message.reply_text(
                "EVM CA must be 42 characters on ONE line (0x + 40 hex).\n"
                "Telegram wrapped yours. Copy from DexScreener and paste once."
            )
            return SETUP_CA
    elif len(ca) < 32:
        await update.message.reply_text("That is not a CA. Paste the full mint on one line.")
        return SETUP_CA
    context.user_data["setup"]["ca"] = ca
    await update.message.reply_text(
        "Minimum buy in USD to post an alert?\nExample: 15\nSend 1 to show almost every buy."
    )
    return SETUP_MIN


async def setup_min(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").replace("$", "").strip()
    try:
        floor = max(1.0, float(raw))
    except ValueError:
        await update.message.reply_text("Number only. Example: 25")
        return SETUP_MIN
    context.user_data["setup"]["min_usd"] = floor
    await update.message.reply_text(
        "Emoji for the buy-size bar?\nExample: 🟢 or 🚕\nSend skip to keep 🟢"
    )
    return SETUP_EMOJI


def _save_watch(chat_id: int, chain: str, ca: str, pool: str, floor: float, emoji: str, tg_url: str = "") -> None:
    con = _db()
    try:
        con.execute(
            "INSERT OR REPLACE INTO watches(chat_id, chain, ca, pool, last_ts, min_usd, emoji, tg_url) VALUES(?,?,?,?,?,?,?,?)",
            (chat_id, chain, ca, pool, int(time.time()), floor, emoji, tg_url),
        )
    except sqlite3.OperationalError:
        con.execute(
            "INSERT OR REPLACE INTO watches(chat_id, chain, ca, pool, last_ts, min_usd, emoji) VALUES(?,?,?,?,?,?,?)",
            (chat_id, chain, ca, pool, int(time.time()), floor, emoji),
        )
    con.commit()
    con.close()


async def setup_emoji(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    emoji = "🟢" if raw.lower() in {"skip", "-", "default"} else (raw[:8] or "🟢")
    context.user_data.setdefault("setup", {})["emoji"] = emoji
    await update.message.reply_text(
        "Now set the project Telegram.\n"
        "USE https://t.me FORM only.\n"
        "Example: https://t.me/YourGroup\n"
        "Send skip to leave it empty."
    )
    return SETUP_TG


async def setup_tg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = (update.message.text or "").strip()
    tg = ""
    if raw.lower() not in {"skip", "-", "none"}:
        if "t.me/" not in raw.lower() and "telegram.me/" not in raw.lower():
            await update.message.reply_text("Must be https://t.me/YourGroup — or send skip.")
            return SETUP_TG
        tg = raw.split()[0]
        if not tg.startswith("http"):
            tg = "https://" + tg.lstrip("/")
    st = context.user_data.get("setup") or {}
    chain, ca = st.get("chain"), st.get("ca")
    floor = float(st.get("min_usd") or MIN_USD)
    emoji = st.get("emoji") or "🟢"
    pool, attrs = _pool_for(chain, ca)
    if not pool:
        pool = ca
        attrs = attrs or {"name": ca[:10], "source": "manual"}
    _save_watch(update.effective_chat.id, chain, ca, pool, floor, emoji, tg)
    name = attrs.get("name") or ca
    extra = f"\nTelegram: {tg}" if tg else "\nTelegram: not set (/settelegram https://t.me/...)"
    await update.message.reply_text(
        f"✅ Watching {name} on {chain.upper()}\n"
        f"CA: `{ca}`\nBuys ≥ ${floor:.0f}\nBar: {emoji}{extra}\n\n"
        "Optional: /setgif then send a GIF for buy cards.\n"
        "/setlogo — group photo = token logo (bot stays Ferzan).\n"
        "/banner — pin a Ferzan ad in this chat.",
        parse_mode="Markdown",
    )
    await _apply_token_logo(update, ca)
    context.user_data.pop("setup", None)
    return ConversationHandler.END


async def settelegram_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = " ".join(context.args or []).strip()
    if not raw or "t.me/" not in raw.lower():
        await update.effective_message.reply_text("Usage: /settelegram https://t.me/YourGroup")
        return
    url = raw.split()[0]
    if not url.startswith("http"):
        url = "https://" + url
    con = _db()
    con.execute("UPDATE watches SET tg_url=? WHERE chat_id=?", (url, update.effective_chat.id))
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Telegram link set: {url}")


def _set_watch_url(chat_id: int, col: str, url: str) -> None:
    con = _db()
    con.execute(f"UPDATE watches SET {col}=? WHERE chat_id=?", (url, chat_id))
    con.commit()
    con.close()


async def setdiscord_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = " ".join(context.args or []).strip()
    if "discord.gg/" not in raw.lower() and "discord.com/" not in raw.lower():
        await update.effective_message.reply_text("Usage: /setdiscord https://discord.gg/yourinvite")
        return
    url = raw.split()[0]
    if not url.startswith("http"):
        url = "https://" + url
    _set_watch_url(update.effective_chat.id, "discord_url", url)
    await update.effective_message.reply_text(f"Discord set: {url}")


async def setx_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = " ".join(context.args or []).strip()
    low = raw.lower()
    if "x.com/" not in low and "twitter.com/" not in low:
        await update.effective_message.reply_text("Usage: /setx https://x.com/yourproject")
        return
    url = raw.split()[0]
    if not url.startswith("http"):
        url = "https://" + url
    _set_watch_url(update.effective_chat.id, "x_url", url)
    await update.effective_message.reply_text(f"X set: {url}")


def _token_img(ca: str) -> str:
    p = _ds(ca) or {}
    info = p.get("info") or {}
    return info.get("imageUrl") or info.get("header") or ""


async def _apply_token_logo(update: Update, ca: str) -> None:
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return
    url = _token_img(ca)
    if not url:
        return
    try:
        img = requests.get(url, timeout=15)
        img.raise_for_status()
        from io import BytesIO
        bio = BytesIO(img.content)
        bio.name = "logo.jpg"
        await update.get_bot().set_chat_photo(chat.id, photo=bio)
    except Exception as exc:
        log.warning("set chat photo %s", exc)


async def setlogo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = _watch(update.effective_chat.id)
    if not row:
        await update.effective_message.reply_text("Pair a token first. /setup")
        return
    _, ca, _, _ = row
    await _apply_token_logo(update, ca)
    await update.effective_message.reply_text(
        "Tried to set this GROUP photo to the token logo.\n"
        "The bot avatar (circled) stays Ferzan — Telegram does not allow a per-chat bot PFP.\n"
        "Bot needs admin right: Change group info."
    )


async def banner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    banner = Path("/opt/ferzan/app/logo.jpg")
    if not banner.exists():
        banner = Path(__file__).resolve().parent / "logo.jpg"
    cap = (
        "⚡ FERZAN ECOSYSTEM\n"
        "Trade · Signals · Launch · Liquidity · Guardian\n"
        "See it. Ape it. Send it.\n"
        f"{CHAT}"
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Open Ferzan Desk", url=CHAT if CHAT.startswith("http") else f"https://t.me/{CHAT.lstrip('@')}")]]
    )
    try:
        if banner.exists():
            with banner.open("rb") as fh:
                msg = await update.effective_message.reply_photo(fh, caption=cap, reply_markup=kb)
        else:
            msg = await update.effective_message.reply_text(cap, reply_markup=kb)
        try:
            await update.get_bot().pin_chat_message(update.effective_chat.id, msg.message_id, disable_notification=True)
        except Exception:
            pass
    except Exception as exc:
        await update.effective_message.reply_text(f"Banner failed: {exc}")


async def emojimap_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _PACK_IDS:
        await _load_pack(update.get_bot())
    if not _PACK_IDS:
        await update.effective_message.reply_text(f"Pack {EMOJI_PACK} did not load.")
        return
    rows = []
    for i, cid in enumerate(_PACK_IDS[:40]):
        face = _PACK_FACE[i] if i < len(_PACK_FACE) else ""
        rows.append(f"{i}: {face or '—'}  `{cid}`")
    await update.effective_message.reply_text(
        f"Pack {EMOJI_PACK} ({len(_PACK_IDS)} icons)\n"
        "Number = slot on the card if we set FERZAN_EMOJI_TITLE=N etc.\n\n"
        + "\n".join(rows),
        parse_mode="Markdown",
    )


def _flags(chat_id: int) -> tuple[int, int]:
    con = _db()
    row = con.execute("SELECT tape, mute_until FROM chat_flags WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    if not row:
        return 1, 0
    return int(row[0] or 1), int(row[1] or 0)


async def preview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = _watch(update.effective_chat.id)
    if not row:
        await update.effective_message.reply_text("Pair a token first. /setup")
        return
    chain, ca, pool, _ = row
    _, attrs = _pool_for(chain, ca)
    con = _db()
    extra = con.execute(
        "SELECT tg_url, discord_url, x_url, emoji FROM watches WHERE chat_id=?",
        (update.effective_chat.id,),
    ).fetchone()
    con.close()
    tg = extra[0] if extra else ""
    disc = extra[1] if extra and len(extra) > 1 else ""
    xx = extra[2] if extra and len(extra) > 2 else ""
    em = extra[3] if extra and len(extra) > 3 else "🟢"
    fake = {"volume_in_usd": 25, "to_token_amount": "100000", "from_token_amount": "0.01", "tx_hash": "", "tx_from_address": ""}
    text, kb = _card(chain, ca, fake, attrs, em or "🟢", tg or "", 1, disc or "", xx or "")
    await update.effective_message.reply_text("PREVIEW — not a live buy.\n" + text, parse_mode="HTML", reply_markup=kb)


async def tape_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args[0] if context.args else "").lower()
    if arg not in {"on", "off"}:
        await update.effective_message.reply_text("Usage: /tape on   or   /tape off")
        return
    on = 1 if arg == "on" else 0
    con = _db()
    con.execute("INSERT INTO chat_flags(chat_id, tape, mute_until) VALUES(?,?,0) ON CONFLICT(chat_id) DO UPDATE SET tape=excluded.tape", (update.effective_chat.id, on))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Tape ON" if on else "Tape OFF — pairing kept, no buy posts")


async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = (context.args[0] if context.args else "1h").lower()
    mins = 60
    if raw.endswith("h"):
        mins = int(float(raw[:-1] or 1) * 60)
    elif raw.endswith("m"):
        mins = int(float(raw[:-1] or 30))
    until = int(time.time()) + max(5, mins) * 60
    con = _db()
    con.execute("INSERT INTO chat_flags(chat_id, tape, mute_until) VALUES(?,1,?) ON CONFLICT(chat_id) DO UPDATE SET mute_until=excluded.mute_until", (update.effective_chat.id, until))
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Muted until {time.strftime('%H:%M', time.localtime(until))}")


async def min_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /min 50")
        return
    try:
        floor = max(1.0, float(context.args[0]))
    except ValueError:
        await update.effective_message.reply_text("Number only. /min 50")
        return
    con = _db()
    con.execute("UPDATE watches SET min_usd=? WHERE chat_id=?", (floor, update.effective_chat.id))
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Floor set to ${floor:.0f}")


async def who_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute("SELECT ca, usd, ts FROM buy_log WHERE chat_id=? ORDER BY id DESC LIMIT 5", (update.effective_chat.id,)).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No Ferzan buys logged in this chat yet.")
        return
    lines = [f"${r[1]:.2f} · {r[0][:8]}… · {time.strftime('%H:%M', time.localtime(r[2]))}" for r in rows]
    await update.effective_message.reply_text("Last Ferzan posts\n" + "\n".join(lines))


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = _watch(update.effective_chat.id)
    tape, mute = _flags(update.effective_chat.id)
    if not row:
        await update.effective_message.reply_text("No token paired. /setup")
        return
    chain, ca, pool, floor = row
    muted = mute > time.time()
    await update.effective_message.reply_text(
        f"Watching {ca[:10]}… · {chain.upper()} · ≥ ${float(floor):.0f}\n"
        f"Tape {'ON' if tape else 'OFF'} · Mute {'ON' if muted else 'off'}"
    )


async def setup_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("setup", None)
    await update.effective_message.reply_text("Setup cancelled. /setup to start over.")
    return ConversationHandler.END


async def track(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if chat.type == "private":
        await update.effective_message.reply_text("Run /track in the project channel.")
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text("Usage: /track base 0xToken [min_usd]")
        return
    chain = context.args[0].lower()
    ca = context.args[1].strip()
    min_usd = MIN_USD
    if len(context.args) >= 3:
        try:
            min_usd = max(1.0, float(context.args[2]))
        except ValueError:
            await update.effective_message.reply_text("min_usd must be a number, e.g. 25")
            return
    if chain not in GT_NET:
        await update.effective_message.reply_text("Chain: sol eth base bsc arb avax pol")
        return
    pool, attrs = _pool_for(chain, ca)
    if not pool:
        await update.effective_message.reply_text("No pool found for that CA yet.")
        return
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO watches(chat_id, chain, ca, pool, last_ts, min_usd) VALUES(?,?,?,?,?,?)",
        (chat.id, chain, ca, pool, int(time.time()), min_usd),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(
        f"Watching {_esc(attrs.get('name') or ca)} on {chain.upper()}.\nBuys ≥ ${min_usd:.0f} post here."
    )



def _file_from(msg) -> tuple[str, str] | tuple[None, None]:
    if not msg:
        return None, None
    if msg.animation:
        return "animation", msg.animation.file_id
    if msg.video:
        return "animation", msg.video.file_id
    if msg.photo:
        return "photo", msg.photo[-1].file_id
    if msg.document and (msg.document.mime_type or "").startswith(("video", "image")):
        return "animation", msg.document.file_id
    return None, None


async def remember_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    kind, fid = _file_from(update.effective_message)
    if not fid:
        return
    LAST_MEDIA[chat_id] = (kind, fid)
    cap = (update.effective_message.caption or "").strip().lower()
    if chat_id not in SETGIF_WAIT and not cap.startswith("/setgif"):
        return
    SETGIF_WAIT.discard(chat_id)
    con = _db()
    con.execute("INSERT OR REPLACE INTO media(chat_id, kind, file_id) VALUES(?,?,?)", (chat_id, kind, fid))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Saved. Buy posts will use that media.")


async def setgif_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    kind, file_id = _file_from(msg.reply_to_message)
    if not file_id:
        kind, file_id = _file_from(msg)
    if file_id:
        pass
    else:
        SETGIF_WAIT.add(update.effective_chat.id)
        await msg.reply_text("Send the GIF, video, or photo now (reply to this message).")
        return
    con = _db()
    con.execute("INSERT OR REPLACE INTO media(chat_id, kind, file_id) VALUES(?,?,?)",
                (update.effective_chat.id, kind, file_id))
    con.commit()
    con.close()
    await msg.reply_text("Buy posts will use that media.")


async def cleargif_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    con.execute("DELETE FROM media WHERE chat_id=?", (update.effective_chat.id,))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Buy posts are text-only again.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


def _chart_caption(chain: str, ca: str, p: dict) -> str:
    base = p.get("baseToken") or {}
    name = base.get("name") or "Token"
    sym = base.get("symbol") or ""
    cid = p.get("chainId") or chain
    price = p.get("priceUsd") or "—"
    mc = _usd(p.get("marketCap") or p.get("fdv"))
    liq = _usd((p.get("liquidity") or {}).get("usd"))
    vol = p.get("volume") or {}
    ch = p.get("priceChange") or {}
    tx = p.get("txns") or {}
    h24 = tx.get("h24") or {}
    created = p.get("pairCreatedAt")
    age = "—"
    if created:
        try:
            sec = int(time.time() - int(created) / (1000 if int(created) > 10_000_000_000 else 1))
            if sec < 3600:
                age = f"{sec // 60}m"
            elif sec < 86400:
                age = f"{sec // 3600}h"
            else:
                age = f"{sec // 86400}d {(sec % 86400) // 3600}h"
        except Exception:
            age = "—"
    info = p.get("info") or {}
    social_lines = []
    for s in info.get("socials") or []:
        typ = str(s.get("type") or "").lower()
        url = s.get("url") or ""
        if typ in ("telegram", "tg") and url:
            social_lines.append(f"👥 <a href=\"{_esc(url)}\">Telegram</a>")
        if typ in ("twitter", "x") and url:
            social_lines.append(f"🐦 <a href=\"{_esc(url)}\">X</a>")
    ds = p.get("url") or f"https://dexscreener.com/{cid}/{ca}"
    buy = f"https://t.me/{TRADE}?start={ca}"
    v5 = vol.get("m5") or 0
    v1 = vol.get("h1") or 0
    v24 = vol.get("h24") or 0
    return (
        f"<b>{_esc(name)} ({_esc(sym)})</b>  Chain: {_esc(str(cid))}\n"
        f"CA: <code>{_esc(ca)}</code>\n\n"
        f"💵 Price: ${price}\n"
        f"🎯 Market cap: {mc}\n"
        f"💧 Liquidity: {liq}\n"
        f"⏱ Age: {age}\n\n"
        f"Vol 5m {_usd(v5)} · 1h {_usd(v1)} · 24h {_usd(v24)}\n"
        f"Ch 5m {ch.get('m5', '—')}% · 1h {ch.get('h1', '—')}% · 24h {ch.get('h24', '—')}%\n"
        f"Tx 24h {(h24.get('buys') or 0) + (h24.get('sells') or 0)}\n"
        + (("\n" + " · ".join(social_lines)) if social_lines else "")
        + f"\n📈 <a href=\"{_esc(ds)}\">Dex</a> · ⚡ <a href=\"{_esc(buy)}\">Ferzan</a>"
    )


async def chart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = _watch(update.effective_chat.id)
    args = context.args or []
    if args:
        ca = args[0].strip()
        chain = args[1].strip() if len(args) > 1 else (row[0] if row else "base")
    elif row:
        chain, ca, _, _ = row
    else:
        await update.effective_message.reply_text("Pair first: /setup   or  /chart 0xCA base")
        return
    p = _ds(ca, chain)
    if not p:
        await update.effective_message.reply_text("No DexScreener pair for that CA on this chain.")
        return
    cid = p.get("chainId") or DS_CHAIN.get(chain, chain)
    token = (p.get("baseToken") or {}).get("address") or ca
    header = f"https://dd.dexscreener.com/ds-data/tokens/{cid}/{token}/header.png?size=lg"
    cap = _chart_caption(chain, ca, p)
    buy = f"https://t.me/{TRADE}?start={ca}"
    ds = p.get("url") or f"https://dexscreener.com/{cid}/{ca}"
    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📈 Dex", url=ds), InlineKeyboardButton("⚡ Ferzan", url=buy)],
            [InlineKeyboardButton("See it. Ape it. Send it. Use Ferzan", url=buy)],
        ]
    )
    try:
        await update.effective_message.reply_photo(header, caption=cap, parse_mode="HTML", reply_markup=kb)
    except Exception:
        await update.effective_message.reply_text(cap, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=False)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = _watch(update.effective_chat.id)
    if not row:
        await update.effective_message.reply_text("No token paired. /add base 0xCA 25")
        return
    chain, ca, pool, min_usd = row
    con = _db()
    em = con.execute("SELECT emoji FROM watches WHERE chat_id=?", (update.effective_chat.id,)).fetchone()
    con.close()
    mark = (em[0] if em and em[0] else "🟢")
    await update.effective_message.reply_text(
        f"⚙️ Settings\nChain: {chain.upper()}\nCA: `{ca}`\nMin buy: ${float(min_usd or 15):.0f}\nBar: {mark}\n"
        f"Change min: /add {chain} {ca} 50\nChange bar: /setemoji 🚕",
        parse_mode="Markdown",
    )


def _extract_ca(text: str) -> str:
    raw = text or ""
    m = re.search(r"0x[a-fA-F0-9]{40}", raw)
    if m:
        return m.group(0)
    compact = "".join(raw.split())
    m = re.search(r"0x[a-fA-F0-9]{40}", compact)
    if m:
        return m.group(0)
    m = re.search(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b", raw)
    if m and not m.group(0).isdigit():
        return m.group(0)
    return ""


def _chain_from_ds(pair: dict) -> str:
    cid = str((pair or {}).get("chainId") or "").lower()
    return {
        "solana": "sol",
        "ethereum": "eth",
        "base": "base",
        "bsc": "bsc",
        "arbitrum": "arb",
        "polygon": "pol",
        "avalanche": "avax",
        "optimism": "op",
    }.get(cid, cid or "base")


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("setup", None)
    msg = update.effective_message
    if not msg:
        return
    await msg.reply_text("Scanning…")
    blob = " ".join(context.args or []) or (msg.text or "")
    ca = _extract_ca(blob)
    if not ca:
        await msg.reply_text("Usage: /scan 0xCA   (space after /scan, one line)")
        return
    await paste_ca(update, context, forced_ca=ca)


async def paste_ca(update: Update, context: ContextTypes.DEFAULT_TYPE, forced_ca: str = "") -> None:
    msg = update.effective_message
    if not msg:
        return
    txt = msg.text or ""
    is_scan = txt.lower().startswith("/scan")
    if txt.startswith("/") and not is_scan and not forced_ca:
        return
    if context.user_data.get("setup") and not is_scan and not forced_ca:
        return
    ca = forced_ca or _extract_ca(txt)
    if not ca:
        if is_scan:
            await msg.reply_text("Usage: /scan 0xCA   (one line)")
        return
    pair = _ds(ca)
    if not pair:
        await msg.reply_text(
            f"FERZAN · scanned\n<code>{html.escape(ca)}</code>\nNo Dex pair yet. Check the chain and try /setup.",
            parse_mode="HTML",
        )
        return
    chain = _chain_from_ds(pair)
    base = pair.get("baseToken") or {}
    name = base.get("name") or "Token"
    sym = base.get("symbol") or ""
    px = pair.get("priceUsd") or "—"
    mc = _usd(pair.get("marketCap") or pair.get("fdv"))
    liq = _usd((pair.get("liquidity") or {}).get("usd"))
    vol = _usd((pair.get("volume") or {}).get("h24"))
    chg = pair.get("priceChange") or {}
    h24 = chg.get("h24")
    try:
        h24s = f"{float(h24):+.1f}%" if h24 is not None else "—"
    except (TypeError, ValueError):
        h24s = "—"
    created = pair.get("pairCreatedAt") or 0
    age = "—"
    if created:
        hrs = max(0, (time.time() * 1000 - float(created)) / 3600000)
        age = f"{hrs:.1f}h" if hrs < 48 else f"{hrs/24:.1f}d"
    dex = (pair.get("dexId") or "dex").title()
    ds = pair.get("url") or f"https://dexscreener.com/{pair.get('chainId')}/{ca}"
    buy = f"https://t.me/{TRADE}?start={ca}"
    hub = HUB
    scan = {
        "sol": f"https://solscan.io/token/{ca}",
        "eth": f"https://etherscan.io/token/{ca}",
        "base": f"https://basescan.org/token/{ca}",
        "bsc": f"https://bscscan.com/token/{ca}",
        "arb": f"https://arbiscan.io/token/{ca}",
    }.get(chain, ds)
    text = (
        f"⚡ <b>FERZAN SCAN</b> · {html.escape(chain.upper())}\n"
        f"<b>{html.escape(str(name))}</b>  ${html.escape(str(sym))}\n"
        f"<code>{html.escape(ca)}</code>\n"
        f"<i>tap CA to copy</i>\n\n"
        f"💵 ${html.escape(str(px))}   {html.escape(h24s)} 24h\n"
        f"🧢 {mc}   💧 {liq}\n"
        f"📊 24h {vol}   ⏱ {html.escape(age)}\n"
        f"🛣 {html.escape(dex)}\n"
        f"<i>See it. Ape it. Send it.</i>"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Buy", url=buy),
                InlineKeyboardButton("Chart", url=ds),
                InlineKeyboardButton("Scan", url=scan),
            ],
            [InlineKeyboardButton("See it. Ape it. Send it.", url=buy)],
            [InlineKeyboardButton("Eco Hub", url=hub)],
        ]
    )
    header = (pair.get("info") or {}).get("header") or (pair.get("info") or {}).get("imageUrl")
    try:
        if header:
            await msg.reply_photo(header, caption=text, parse_mode="HTML", reply_markup=kb)
            return
    except Exception:
        pass
    await msg.reply_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


async def _token_card(update: Update, extra: str = "") -> None:
    row = _watch(update.effective_chat.id)
    ca = (context_args_ca(update, extra) if False else None)
    args = update.effective_message.text.split()[1:] if update.effective_message and update.effective_message.text else []
    if args:
        ca = args[0]
        chain = "base"
    elif row:
        chain, ca, _, _ = row
    else:
        await update.effective_message.reply_text("Pair first: /add base 0xCA")
        return
    p = _ds(ca)
    if not p:
        await update.effective_message.reply_text("No DexScreener pair yet.")
        return
    base = p.get("baseToken") or {}
    name = base.get("name") or ca[:8]
    sym = base.get("symbol") or ""
    px = (p.get("priceUsd") or "—")
    mc = p.get("marketCap") or p.get("fdv") or "—"
    liq = (p.get("liquidity") or {}).get("usd") or "—"
    vol = (p.get("volume") or {}).get("h24") or "—"
    url = p.get("url") or f"https://dexscreener.com/{p.get('chainId')}/{ca}"
    text = (
        f"📊 <b>{html.escape(str(name))}</b> ${html.escape(str(sym))}\n"
        f"<code>{html.escape(ca)}</code>\n"
        f"💵 {html.escape(str(px))}\n"
        f"🧢 MC {html.escape(str(mc))}\n"
        f"💧 Liq {html.escape(str(liq))}\n"
        f"📈 24h vol {html.escape(str(vol))}"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("DexScreener", url=url)]])
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=kb)


def context_args_ca(update, extra):
    return None


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _token_card(update)


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _token_card(update)


async def dex_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _token_card(update)


async def market_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana&vs_currencies=usd&include_24hr_change=true",
            timeout=12,
        )
        d = r.json()
    except Exception:
        await update.effective_message.reply_text("Market feed busy. Try again.")
        return
    lines = ["🌍 Market"]
    for key, label in (("bitcoin", "BTC"), ("ethereum", "ETH"), ("solana", "SOL")):
        row = d.get(key) or {}
        chg = row.get("usd_24h_change") or 0
        lines.append(f"{label}  ${row.get('usd', 0):,.2f}  ({chg:+.1f}%)")
    await update.effective_message.reply_text("\n".join(lines))


async def vote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = _watch(update.effective_chat.id)
    title = "Vote this token?"
    if row:
        title = f"Vote {row[1][:8]}… ?"
    await context.bot.send_poll(
        update.effective_chat.id,
        title,
        ["Bullish", "Need more info", "Pass"],
        is_anonymous=True,
    )


def _pct_bar(have: int, need: int) -> str:
    need = max(1, int(need or 1))
    have = max(0, int(have or 0))
    return f"{have} | {need}  [{min(100, int(100 * have / need))}%]"


def _raid_text(row: dict) -> str:
    tag = row.get("cashtag") or ""
    mins = max(1, int((row.get("ends") or 0) - time.time()) // 60) if row.get("active") else 0
    finished = (
        int(row.get("likes_h") or 0) >= int(row.get("likes_t") or 1)
        and int(row.get("rt_h") or 0) >= int(row.get("rt_t") or 1)
        and int(row.get("re_h") or 0) >= int(row.get("re_t") or 1)
    )
    if finished:
        status = "DONE"
    elif row.get("active"):
        status = "IN PROGRESS"
    else:
        status = "STOPPED"
    def done(h, t):
        return " ✅" if int(h or 0) >= int(t or 1) else ""

    return (
        f"{_icon('TITLE', 0, '⚡')} <b>FERZAN RAID</b> · {status}\n"
        f"<b>{_esc(tag)}</b>\n\n"
        f"❤️  Likes     {_pct_bar(row['likes_h'], row['likes_t'])}{done(row['likes_h'], row['likes_t'])}\n"
        f"🔁  Reposts   {_pct_bar(row['rt_h'], row['rt_t'])}{done(row['rt_h'], row['rt_t'])}\n"
        f"💬  Replies   {_pct_bar(row['re_h'], row['re_t'])}{done(row['re_h'], row['re_t'])}\n\n"
        f"⏱  {mins}m left\n"
        f"🔗  <a href=\"{_esc(row['url'])}\">Open the post</a>\n"
        + (f"{_icon('USD', 1, '💵')}  {_esc(tag)}\n" if tag else "")
        + "\n<i>Tap ❤️ 🔁 💬 after you smash.</i>\n"
        + "<i>See it. Ape it. Send it.</i>"
    )


def _raid_kb(rid: int, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Open Post", url=url),
                InlineKeyboardButton("Stop", callback_data=f"rd:stop:{rid}"),
            ],
            [
                InlineKeyboardButton("LB", callback_data=f"rd:lb:{rid}"),
                InlineKeyboardButton("❤️", callback_data=f"rd:like:{rid}"),
                InlineKeyboardButton("🔁", callback_data=f"rd:rt:{rid}"),
                InlineKeyboardButton("💬", callback_data=f"rd:re:{rid}"),
            ],
            [InlineKeyboardButton("Eco Hub", url=HUB)],
        ]
    )


def _active_raid(chat_id: int):
    con = _db()
    row = con.execute(
        "SELECT id, url, likes_t, rt_t, re_t, likes_h, rt_h, re_h, ends, cashtag, active, msg_id "
        "FROM raids WHERE chat_id=? AND active=1 ORDER BY id DESC LIMIT 1",
        (chat_id,),
    ).fetchone()
    con.close()
    if not row:
        return None
    keys = ["id", "url", "likes_t", "rt_t", "re_t", "likes_h", "rt_h", "re_h", "ends", "cashtag", "active", "msg_id"]
    return dict(zip(keys, row))


async def raid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text(
            "⚔️ FERZAN RAID\n"
            "/raid <x link> [likes] [reposts] [replies] [minutes]\n"
            "Example:\n"
            "/raid https://x.com/user/status/123 5 5 2 60\n"
            "/raidstop  /raidlb  /queue <link>"
        )
        return
    url = context.args[0]
    if "x.com/" not in url and "twitter.com/" not in url:
        await update.effective_message.reply_text("Need an x.com or twitter.com status link.")
        return
    nums = []
    for a in context.args[1:]:
        if a.isdigit():
            nums.append(int(a))
    likes_t = nums[0] if len(nums) > 0 else 5
    rt_t = nums[1] if len(nums) > 1 else 5
    re_t = nums[2] if len(nums) > 2 else 2
    mins = nums[3] if len(nums) > 3 else 60
    tag = ""
    w = _watch(update.effective_chat.id)
    if w:
        _, ca, _, _ = w
        pair = _ds(ca)
        tag = "$" + ((pair.get("baseToken") or {}).get("symbol") or "")
        if tag == "$":
            tag = ""
    extra = " ".join(a for a in context.args[1:] if not a.isdigit())
    if extra.startswith("$"):
        tag = extra.split()[0]
    con = _db()
    con.execute("UPDATE raids SET active=0 WHERE chat_id=?", (update.effective_chat.id,))
    cur = con.execute(
        "INSERT INTO raids(chat_id,url,note,created,likes_t,rt_t,re_t,ends,cashtag,active) "
        "VALUES(?,?,?,?,?,?,?,?,?,1)",
        (
            update.effective_chat.id,
            url,
            extra,
            int(time.time()),
            likes_t,
            rt_t,
            re_t,
            int(time.time()) + mins * 60,
            tag,
        ),
    )
    rid = cur.lastrowid
    con.commit()
    con.close()
    row = {
        "id": rid,
        "url": url,
        "likes_t": likes_t,
        "rt_t": rt_t,
        "re_t": re_t,
        "likes_h": 0,
        "rt_h": 0,
        "re_h": 0,
        "ends": int(time.time()) + mins * 60,
        "cashtag": tag,
        "active": 1,
    }
    start = _raid_text(row)
    banner = Path("/opt/ferzan/app/raid.jpg")
    if not banner.exists():
        banner = Path(__file__).resolve().parent / "raid.jpg"
    if not banner.exists():
        banner = Path("/opt/ferzan/app/logo.jpg")
    if not banner.exists():
        banner = Path(__file__).resolve().parent / "logo.jpg"
    kb = _raid_kb(rid, url)
    try:
        if banner.exists():
            with banner.open("rb") as fh:
                msg = await update.effective_message.reply_photo(
                    fh, caption=start, parse_mode="HTML", reply_markup=kb
                )
        else:
            msg = await update.effective_message.reply_text(start, parse_mode="HTML", reply_markup=kb)
    except Exception:
        msg = await update.effective_message.reply_text(start, parse_mode="HTML", reply_markup=kb)
    con = _db()
    con.execute(
        "UPDATE raids SET msg_id=?, last_ping=?, ping_min=5 WHERE id=?",
        (msg.message_id, int(time.time()), rid),
    )
    con.commit()
    con.close()
    try:
        await context.bot.pin_chat_message(
            update.effective_chat.id, msg.message_id, disable_notification=True
        )
    except Exception as exc:
        log.warning("raid pin: %s", exc)
    if RAID_CH and str(update.effective_chat.username or "") != RAID_CH.lstrip("@"):
        try:
            if banner.exists():
                with banner.open("rb") as fh:
                    await context.bot.send_photo(
                        RAID_CH, fh, caption=start, parse_mode="HTML", reply_markup=kb
                    )
            else:
                await context.bot.send_message(RAID_CH, start, reply_markup=kb)
        except Exception as exc:
            log.warning("raid mirror %s: %s", RAID_CH, exc)


async def raidstop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    con.execute("UPDATE raids SET active=0 WHERE chat_id=?", (update.effective_chat.id,))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Raid stopped.")


async def raid_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":")
    if len(parts) < 3:
        return
    _, kind, rid = parts[0], parts[1], int(parts[2])
    con = _db()
    if kind == "stop":
        await q.answer()
        con.execute("UPDATE raids SET active=0 WHERE id=?", (rid,))
        con.commit()
        con.close()
        await q.edit_message_caption(caption="⚔️ Raid stopped.") if q.message.photo else None
        try:
            await q.edit_message_text("⚔️ Raid stopped.")
        except Exception:
            pass
        return
    if kind == "lb":
        await q.answer()
        con.close()
        await lb_cmd(update, context)
        return
    col = {"like": "likes_h", "rt": "rt_h", "re": "re_h"}.get(kind)
    if not col:
        await q.answer()
        con.close()
        return
    u = update.effective_user
    cur = con.execute(
        "INSERT OR IGNORE INTO raid_taps(raid_id, user_id, kind) VALUES(?,?,?)",
        (rid, u.id, kind),
    )
    if cur.rowcount == 0:
        con.close()
        await q.answer("Already counted on this button.", show_alert=False)
        return
    con.execute(
        "INSERT INTO raid_scores(chat_id, user_id, name, pts) VALUES(?,?,?,1) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET pts = pts + 1, name=excluded.name",
        (update.effective_chat.id, u.id, u.full_name or u.username or str(u.id)),
    )
    con.execute(f"UPDATE raids SET {col} = {col} + 1 WHERE id=? AND active=1", (rid,))
    con.commit()
    row = con.execute(
        "SELECT id, url, likes_t, rt_t, re_t, likes_h, rt_h, re_h, ends, cashtag, active, msg_id "
        "FROM raids WHERE id=?",
        (rid,),
    ).fetchone()
    con.close()
    if not row:
        return
    keys = ["id", "url", "likes_t", "rt_t", "re_t", "likes_h", "rt_h", "re_h", "ends", "cashtag", "active", "msg_id"]
    d = dict(zip(keys, row))
    done = (
        int(d["likes_h"]) >= int(d["likes_t"])
        and int(d["rt_h"]) >= int(d["rt_t"])
        and int(d["re_h"]) >= int(d["re_t"])
    )
    if done:
        con2 = _db()
        con2.execute("UPDATE raids SET active=0 WHERE id=?", (rid,))
        con2.commit()
        con2.close()
        d["active"] = 0
        await q.answer("Targets hit. Raid done.")
    else:
        await q.answer("+1")
    txt = _raid_text(d)
    try:
        if q.message.photo:
            await q.edit_message_caption(
                caption=txt, parse_mode="HTML", reply_markup=_raid_kb(rid, d["url"])
            )
        else:
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=_raid_kb(rid, d["url"]))
    except Exception:
        pass


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /queue https://x.com/...")
        return
    await raid_cmd(update, context)
    await update.effective_message.reply_text("Queued. /next posts the oldest unused style — latest raid is live above.")


async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        await queue_cmd(update, context)
        return
    con = _db()
    row = con.execute(
        "SELECT id, url, note FROM raids WHERE chat_id=? ORDER BY id DESC LIMIT 1",
        (update.effective_chat.id,),
    ).fetchone()
    con.close()
    if not row:
        await update.effective_message.reply_text("Queue empty. /queue <url>")
        return
    _, url, note = row
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open X", url=url)]])
    await update.effective_message.reply_text(f"📣 NEXT RAID\n{url}\n{note or ''}", reply_markup=kb)


async def list_raids(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute(
        "SELECT url, note FROM raids WHERE chat_id=? ORDER BY id DESC LIMIT 8",
        (update.effective_chat.id,),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No raids queued.")
        return
    lines = ["📋 Raid queue"]
    for url, note in rows:
        lines.append(f"• {url} {note or ''}")
    await update.effective_message.reply_text("\n".join(lines)[:3500])


async def raidjoin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    con = _db()
    con.execute(
        "INSERT INTO raid_scores(chat_id, user_id, name, pts) VALUES(?,?,?,1) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET pts = pts + 1, name=excluded.name",
        (update.effective_chat.id, u.id, u.full_name or u.username or str(u.id)),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"+1 raid point for {u.first_name}")


async def lb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute(
        "SELECT name, pts FROM raid_scores WHERE chat_id=? ORDER BY pts DESC LIMIT 10",
        (update.effective_chat.id,),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No scores yet. /raidjoin after you raid.")
        return
    lines = ["🏆 Raid board"]
    for i, (name, pts) in enumerate(rows, 1):
        lines.append(f"{i}. {name}  {pts}")
    await update.effective_message.reply_text("\n".join(lines))


async def raidevent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🎯 Raid event is ON in this chat.\n"
        "Admin posts /raid <x link>. Members /raidjoin after they engage.\n"
        "/relb for the event board."
    )


async def raidint_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /raidint 5    (minutes, 2–60)")
        return
    mins = max(2, min(60, int(context.args[0])))
    con = _db()
    con.execute(
        "UPDATE raids SET ping_min=? WHERE chat_id=? AND active=1",
        (mins, update.effective_chat.id),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Raid reminder every {mins} min.")


async def setemoji_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /setemoji 🚕")
        return
    em = context.args[0][:8]
    con = _db()
    con.execute("UPDATE watches SET emoji=? WHERE chat_id=?", (em, update.effective_chat.id))
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Buy bar set to {em}{em}{em}")


async def untrack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    con.execute("DELETE FROM watches WHERE chat_id=?", (update.effective_chat.id,))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Stopped buy alerts in this chat.")


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    now = int(time.time())
    try:
        live = list(
            con.execute(
                "SELECT id, chat_id, url, cashtag, likes_h, likes_t, rt_h, rt_t, re_h, re_t, ends, last_ping, ping_min "
                "FROM raids WHERE active=1"
            )
        )
    except sqlite3.OperationalError:
        live = []
    for rid, chat_id, url, tag, lh, lt, rh, rt, eh, et, ends, last_ping, ping_min in live:
        if ends and now > int(ends):
            con.execute("UPDATE raids SET active=0 WHERE id=?", (rid,))
            continue
        every = max(2, int(ping_min or 5)) * 60
        if now - int(last_ping or 0) < every:
            continue
        txt = (
            f"⚔️ RAID LIVE {tag or ''}\n"
            f"❤️ {lh}/{lt}  🔁 {rh}/{rt}  💬 {eh}/{et}\n"
            f"<a href=\"{_esc(url)}\">Open the post</a>\n"
            f"<i>See it. Ape it. Send it.</i>"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open Post", url=url)]])
        try:
            await context.bot.send_message(chat_id, txt, parse_mode="HTML", reply_markup=kb)
            con.execute("UPDATE raids SET last_ping=? WHERE id=?", (now, rid))
        except Exception as exc:
            log.warning("raid ping %s: %s", chat_id, exc)
    con.commit()
    try:
        rows = list(con.execute("SELECT chat_id, chain, ca, pool, last_ts, min_usd, emoji, tg_url, discord_url, x_url FROM watches"))
    except sqlite3.OperationalError:
        rows = [(*r, "") for r in con.execute("SELECT chat_id, chain, ca, pool, last_ts, min_usd, emoji FROM watches")]
    for chat_id, chain, ca, pool, last_ts, min_usd, emoji, *rest in rows:
        tg_url = rest[0] if rest else ""
        discord_url = rest[1] if len(rest) > 1 else ""
        x_url = rest[2] if len(rest) > 2 else ""
        tape, mute_until = _flags(chat_id)
        if not tape or mute_until > time.time():
            continue
        floor = float(min_usd or MIN_USD)
        net = GT_NET.get(chain, chain)
        trades = _trades(net, pool, int(last_ts or 0))
        if not trades:
            continue
        _, attrs = _pool_for(chain, ca)
        newest = last_ts
        for tr in trades:
            usd = float(tr.get("volume_in_usd") or 0)
            if usd < floor:
                newest = max(newest, tr["ts"])
                continue
            cluster = sum(1 for x in trades if abs(x["ts"] - tr["ts"]) <= 12)
            text, kb = _card(chain, ca, tr, attrs, emoji or "🟢", tg_url or "", cluster, discord_url, x_url)
            con.execute("INSERT INTO buy_log(chat_id, ca, usd, ts) VALUES(?,?,?,?)", (chat_id, ca, usd, tr["ts"]))
            try:
                media = _media(chat_id)
                if media and media[0] in {"animation", "video"}:
                    try:
                        await context.bot.send_animation(chat_id, media[1], caption=text, parse_mode="HTML", reply_markup=kb)
                    except Exception:
                        await context.bot.send_video(chat_id, media[1], caption=text, parse_mode="HTML", reply_markup=kb)
                elif media and media[0] == "photo":
                    await context.bot.send_photo(chat_id, media[1], caption=text, parse_mode="HTML", reply_markup=kb)
                else:
                    await context.bot.send_message(
                        chat_id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
                    )
            except Exception as exc:
                log.warning("post %s %s", chat_id, exc)
            newest = max(newest, tr["ts"])
        con.execute(
            "UPDATE watches SET last_ts=? WHERE chat_id=? AND chain=? AND ca=?",
            (newest, chat_id, chain, ca),
        )
        con.commit()
    con.close()


def _pay_box() -> str:
    sol = TREASURY_SOL or "(set FEE_WALLET_SOL in .env)"
    evm = TREASURY_EVM or "(set FEE_WALLET_EVM in .env)"
    return (
        f"Pay treasury\n"
        f"◎ SOL `{sol}`\n"
        f"Ξ EVM `{evm}`\n\n"
        "After payment send /paid &lt;tx hash&gt; and your CA.\n"
        f"Booking help: {CHAT}"
    )


PAY_CHAINS = [
    [InlineKeyboardButton("BNB Smart Chain", callback_data="mk:bsc")],
    [InlineKeyboardButton("Ethereum", callback_data="mk:eth")],
    [InlineKeyboardButton("Base", callback_data="mk:base")],
    [InlineKeyboardButton("Arbitrum One", callback_data="mk:arb")],
    [InlineKeyboardButton("Solana", callback_data="mk:sol")],
    [InlineKeyboardButton("❌ Cancel", callback_data="mk:x")],
]


async def marketing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📣 Ferzan promotion\n\nChoose the chain you want to pay on:",
        reply_markup=InlineKeyboardMarkup(PAY_CHAINS),
    )


async def marketing_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    key = (q.data or "").split(":")[-1]
    if key == "x":
        await q.edit_message_text("Cancelled.")
        return
    if key.startswith("svc"):
        kind = key.split("-")[-1]
        chain = context.user_data.get("mk_chain", "sol")
        pay = TREASURY_SOL if chain == "sol" else TREASURY_EVM
        label = {
            "trend": "Trending 24h · 12 SOL / 0.5 ETH",
            "ads": "Button ad 24h · 2.7 SOL",
            "max": "Max pack · 14 SOL",
        }.get(kind, kind)
        await q.edit_message_text(
            f"✅ {label}\nPay on {chain.upper()}\nTreasury:\n`{pay or 'set FEE_WALLET in .env'}`\n\n"
            "Send payment then /paid <txhash> <CA>",
            parse_mode="Markdown",
        )
        return
    context.user_data["mk_chain"] = key
    await q.edit_message_text(
        f"📣 Marketing on {key.upper()}\n\nChoose a service:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📈 Trending", callback_data="mk:svc-trend")],
                [InlineKeyboardButton("📊 Buy-card button ads", callback_data="mk:svc-ads")],
                [InlineKeyboardButton("🚀 Maximum exposure", callback_data="mk:svc-max")],
                [InlineKeyboardButton("❌ Cancel", callback_data="mk:x")],
            ]
        ),
    )


async def trending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🔥 List on Trending\nPosted in https://t.me/Ferzan_Trending after payment.\n\nFirst, choose your token's CHAIN:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("BNB Smart Chain", callback_data="td:bsc")],
                [InlineKeyboardButton("Ethereum", callback_data="td:eth")],
                [InlineKeyboardButton("Base", callback_data="td:base")],
                [InlineKeyboardButton("Arbitrum One", callback_data="td:arb")],
                [InlineKeyboardButton("Solana", callback_data="td:sol")],
                [InlineKeyboardButton("❌ Cancel", callback_data="td:x")],
            ]
        ),
    )


async def trending_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    key = (q.data or "").split(":")[-1]
    if key == "x":
        await q.edit_message_text("Cancelled.")
        return
    context.user_data["td_chain"] = key
    await q.edit_message_text(
        f"🌐 Chain: {key.upper()}\n\nSend the contract address of the token to list.\n"
        "Then /paid <txhash> after you pay treasury."
    )


async def ads_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📢 BUY-CARD BUTTON AD\n\n"
        "Your link sits on Ferzan buy alerts for the term you pay.\n\n"
        "24 hours  2.7 SOL\n"
        "3 days    6.3 SOL\n"
        "7 days    13.5 SOL\n\n"
        "Include destination URL in /paid.\n\n"
        + _pay_box(),
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )


async def paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Usage: /paid <txhash> [CA or url]")
        return
    tx = args[0]
    extra = " ".join(args[1:])
    log.info("PAID claim uid=%s tx=%s extra=%s", update.effective_user.id, tx, extra)
    await update.effective_message.reply_text(
        "Got it. Treasury will confirm the tx.\n"
        f"Tx: `{tx}`\n{extra}\n"
        f"If it is not confirmed in a bit, ping {CHAT}",
        parse_mode="Markdown",
        disable_web_page_preview=True,
    )
    try:
        await context.bot.send_message(
            TRENDING_CH,
            f"🔥 TRENDING REQUEST\nTx `{tx}`\n{extra or '—'}\nfrom {update.effective_user.mention_html()}",
            parse_mode="HTML",
        )
    except Exception as exc:
        log.warning("trending mirror %s: %s", TRENDING_CH, exc)


def main() -> None:
    token = (os.getenv("BUYBOT_TOKEN") or os.getenv("FERZAN_BUY_TOKEN") or "").strip()
    if not token:
        raise SystemExit("Set BUYBOT_TOKEN in /opt/ferzan/.env")
    app = Application.builder().token(token).build()
    async def _menu(app_):
        await _load_pack(app_.bot)
        await app_.bot.set_my_commands(
            [
                BotCommand("start", "Ferzan Buy home"),
                BotCommand("setup", "Pair a token"),
                BotCommand("scan", "Scan a CA"),
                BotCommand("preview", "Fake buy card"),
                BotCommand("tape", "Tape on or off"),
                BotCommand("mute", "Quiet 1h"),
                BotCommand("min", "Min buy USD"),
                BotCommand("who", "Last buys"),
                BotCommand("status", "Watching"),
                BotCommand("untrack", "Stop alerts"),
                BotCommand("chart", "Token chart"),
                BotCommand("help", "Help"),
            ]
        )
    app.post_init = _menu
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(
        ConversationHandler(
            entry_points=[CommandHandler("setup", setup_start)],
            states={
                SETUP_CHAIN: [CallbackQueryHandler(setup_chain, pattern=r"^su:")],
                SETUP_CA: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_ca)],
                SETUP_MIN: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_min)],
                SETUP_EMOJI: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_emoji)],
                SETUP_TG: [MessageHandler(filters.TEXT & ~filters.COMMAND, setup_tg)],
            },
            fallbacks=[
                CommandHandler("cancel", setup_cancel),
                CommandHandler("scan", scan_cmd),
            ],
            per_chat=True,
            per_user=True,
            block=False,
        )
    )
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("fscan", scan_cmd))
    app.add_handler(CommandHandler("ca", scan_cmd))
    app.add_handler(CommandHandler("setlogo", setlogo_cmd))
    app.add_handler(CommandHandler("banner", banner_cmd))
    app.add_handler(CommandHandler("emojimap", emojimap_cmd))
    app.add_handler(CommandHandler("setgif", setgif_cmd))
    app.add_handler(CommandHandler("settelegram", settelegram_cmd))
    app.add_handler(CommandHandler("setdiscord", setdiscord_cmd))
    app.add_handler(CommandHandler("setx", setx_cmd))
    app.add_handler(CommandHandler("preview", preview_cmd))
    app.add_handler(CommandHandler("tape", tape_cmd))
    app.add_handler(CommandHandler("mute", mute_cmd))
    app.add_handler(CommandHandler("min", min_cmd))
    app.add_handler(CommandHandler("who", who_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cleargif", cleargif_cmd))
    app.add_handler(MessageHandler(
        filters.ANIMATION | filters.VIDEO | filters.PHOTO | filters.Document.ALL,
        remember_media,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, paste_ca))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("add", track))
    app.add_handler(CommandHandler("untrack", untrack))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("setemoji", setemoji_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("dex", dex_cmd))
    app.add_handler(CommandHandler("chart", chart_cmd))
    app.add_handler(CommandHandler("marketing", marketing_cmd))
    app.add_handler(CallbackQueryHandler(marketing_cb, pattern=r"^mk:"))
    app.add_handler(CommandHandler("trending", trending_cmd))
    app.add_handler(CallbackQueryHandler(trending_cb, pattern=r"^td:"))
    app.add_handler(CommandHandler("ads", ads_cmd))
    app.add_handler(CommandHandler("paid", paid_cmd))
    app.add_handler(CommandHandler("market", market_cmd))
    app.add_handler(CommandHandler("vote", vote_cmd))
    app.add_handler(CommandHandler("raid", raid_cmd))
    app.add_handler(CommandHandler("raidstop", raidstop_cmd))
    app.add_handler(CommandHandler("raidint", raidint_cmd))
    app.add_handler(CallbackQueryHandler(raid_cb, pattern=r"^rd:"))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("nextlist", list_raids))
    app.add_handler(CommandHandler("queuelist", list_raids))
    app.add_handler(CommandHandler("raidjoin", raidjoin_cmd))
    app.add_handler(CommandHandler("lb", lb_cmd))
    app.add_handler(CommandHandler("clb", lb_cmd))
    app.add_handler(CommandHandler("raidevent", raidevent_cmd))
    app.add_handler(CommandHandler("relb", lb_cmd))
    app.job_queue.run_repeating(tick, interval=25, first=8)
    log.info("Ferzan Buy running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
