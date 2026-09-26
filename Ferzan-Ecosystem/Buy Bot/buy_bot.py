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
def _tg_chat(val: str, fallback: str) -> str:
    v = (val or fallback or "").strip()
    if "t.me/" in v:
        v = "@" + v.split("t.me/")[-1].split("?")[0].strip("/")
    if v and not v.startswith("@") and not v.lstrip("-").isdigit():
        v = "@" + v
    return v


RAID_CH = _tg_chat(os.getenv("FERZAN_RAID_CHAT") or "", "@Ferzan_Raid")
TRENDING_CH = _tg_chat(os.getenv("FERZAN_TRENDING_CHAT") or "", "@Ferzan_Trending")
# Explicit override for the Leaderboard button when RAID_CH is a private chat (numeric ID) —
# a t.me/<username> link only works for public chats, so a private raid channel needs its
# actual invite link (t.me/+xxxx) spelled out here instead.
RAID_INVITE = (os.getenv("FERZAN_RAID_INVITE") or "").strip()
TREASURY_SOL = (os.getenv("FEE_WALLET_SOL") or os.getenv("PLATFORM_TREASURY_SOL") or "").strip()
TREASURY_EVM = (os.getenv("FEE_WALLET_EVM") or os.getenv("PLATFORM_TREASURY_EVM") or "").strip()

# Boost pricing (USD) — deliberately low while the project has low exposure, raise later.
# duration in hours, price in USD.
BOOST_TIERS = {
    "raid": [
        (24, 12.0),
        (72, 28.0),
        (168, 50.0),
    ],
    "trending": [
        (24, 40.0),
        (72, 90.0),
        (168, 165.0),
    ],
}
# Minimum a partial/underpaid tx still has to clear to be worth prorating into any boost time.
BOOST_MIN_USD = 3.0

# Buy-card button ads — SOL-denominated (not USD-pegged like raid/trending), same as before.
ADS_TIERS_SOL = [(24, 2.7), (72, 6.3), (168, 13.5)]
ADS_MIN_SOL = 0.05

OWNER_IDS = {
    int(x) for x in (os.getenv("FERZAN_OWNER_IDS") or "5107098957").replace(" ", "").split(",") if x.strip().isdigit()
}

# Weighted raid points — a reply takes more effort than a like, so it should be worth more.
RAID_WEIGHTS = {"like": 1, "rt": 2, "re": 3, "join": 1}

# XP tiers — display-only labels over a chat's cumulative raid points.
RAID_TIERS = [
    (0, "🥚 Rookie"),
    (15, "🥉 Bronze Raider"),
    (50, "🥈 Silver Raider"),
    (150, "🥇 Gold Raider"),
    (400, "💎 Diamond Raider"),
    (1000, "👑 Raid Legend"),
]

STREAK_BONUS_EVERY = 5  # every 5th consecutive raid-day earns a bonus
STREAK_BONUS_PTS = 5

MILESTONES = [
    100_000, 250_000, 500_000, 1_000_000, 2_500_000, 5_000_000,
    10_000_000, 25_000_000, 50_000_000, 100_000_000, 250_000_000,
    500_000_000, 1_000_000_000,
]


def _next_milestone(mcap: float, last: float) -> float:
    for m in MILESTONES:
        if mcap >= m > last:
            return m
    return 0


def _dev_balance(chain: str, ca: str, wallet: str) -> float | None:
    """Current token balance of a dev/deployer wallet, or None if it couldn't be read."""
    try:
        low = str(chain).lower()
        if low in ("sol", "solana", "pump", "pumpfun"):
            r = requests.post(
                SOL_RPC,
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
                    "params": [wallet, {"mint": ca}, {"encoding": "jsonParsed"}],
                },
                timeout=10,
            )
            accts = ((r.json() or {}).get("result") or {}).get("value") or []
            total = 0.0
            for acc in accts:
                info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
                amt = ((info.get("tokenAmount") or {}).get("uiAmount")) or 0
                total += float(amt or 0)
            return total
        rpc = EVM_RPC.get(low)
        if not rpc:
            return None
        selector = "70a08231"  # balanceOf(address)
        padded = wallet.lower().replace("0x", "").rjust(64, "0")
        r = requests.post(
            rpc,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                  "params": [{"to": ca, "data": "0x" + selector + padded}, "latest"]},
            timeout=10,
        )
        hexval = (r.json() or {}).get("result") or "0x0"
        raw = int(hexval, 16)
        decimals = 18
        try:
            dsel = "313ce567"  # decimals()
            rd = requests.post(
                rpc,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                      "params": [{"to": ca, "data": "0x" + dsel}, "latest"]},
                timeout=8,
            )
            dhex = (rd.json() or {}).get("result")
            if dhex:
                decimals = int(dhex, 16)
        except Exception:
            pass
        return raw / (10 ** decimals)
    except Exception as exc:
        log.warning("dev balance %s %s: %s", chain, wallet, exc)
        return None


def _tier_for_pts(pts: int) -> str:
    label = RAID_TIERS[0][1]
    for floor, name in RAID_TIERS:
        if pts >= floor:
            label = name
        else:
            break
    return label


def _bump_streak(con: sqlite3.Connection, chat_id: int, user_id: int) -> int:
    """Advance a user's daily raid streak. Returns bonus points earned (0 if none), and
    awards them onto raid_scores directly. Only advances once per calendar day."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    row = con.execute(
        "SELECT last_day, streak FROM raid_streaks WHERE chat_id=? AND user_id=?",
        (chat_id, user_id),
    ).fetchone()
    if row and row[0] == today:
        return 0  # already counted today
    if row:
        last_day, streak = row[0], int(row[1] or 0)
        yesterday = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
        streak = streak + 1 if last_day == yesterday else 1
    else:
        streak = 1
    con.execute(
        "INSERT INTO raid_streaks(chat_id, user_id, last_day, streak) VALUES(?,?,?,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET last_day=excluded.last_day, streak=excluded.streak",
        (chat_id, user_id, today, streak),
    )
    if streak and streak % STREAK_BONUS_EVERY == 0:
        return STREAK_BONUS_PTS
    return 0

WALLET_RE = {
    "sol": re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"),
    "evm": re.compile(r"^0x[a-fA-F0-9]{40}$"),
}


async def _is_chat_admin(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    if u.id in OWNER_IDS:
        return True
    chat = update.effective_chat
    if not chat or chat.type == "private":
        return False
    try:
        member = await chat.get_member(u.id)
        return member.status in ("creator", "administrator")
    except Exception:
        return False

SOL_RPC = (os.getenv("FERZAN_SOL_RPC") or "https://api.mainnet-beta.solana.com").strip()
EVM_RPC = {
    "eth": os.getenv("FERZAN_ETH_RPC") or "https://ethereum-rpc.publicnode.com",
    "ethereum": os.getenv("FERZAN_ETH_RPC") or "https://ethereum-rpc.publicnode.com",
    "base": os.getenv("FERZAN_BASE_RPC") or "https://base-rpc.publicnode.com",
    "bsc": os.getenv("FERZAN_BSC_RPC") or "https://bsc-rpc.publicnode.com",
    "arb": os.getenv("FERZAN_ARB_RPC") or "https://arbitrum-one-rpc.publicnode.com",
}
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
        face = _PACK_FACE[i] if i < len(_PACK_FACE) and _PACK_FACE[i] else fallback
        return f'<tg-emoji emoji-id="{_PACK_IDS[i]}">{face}</tg-emoji>'
    return fallback


def _icon(name: str, default: int, fallback: str) -> str:
    i = _slot(name, default)
    face = _PACK_FACE[i] if 0 <= i < len(_PACK_FACE) and _PACK_FACE[i] else "F"
    return _ce(i, face)


def _banner() -> Path | None:
    for p in (
        Path("/opt/ferzan/app/raid.jpg"),
        Path(__file__).resolve().parent / "raid.jpg",
        Path("/opt/ferzan/app/logo.jpg"),
        Path(__file__).resolve().parent / "logo.jpg",
    ):
        if p.exists():
            return p
    return None


def _face(i: int = 0) -> str:
    if 0 <= i < len(_PACK_FACE) and _PACK_FACE[i]:
        return _PACK_FACE[i]
    return ""


async def _load_pack(bot) -> None:
    global _PACK_IDS, _PACK_FACE
    _PACK_IDS, _PACK_FACE = [], []
    for name in (EMOJI_PACK, "FerzanBuyBot"):
        if not name:
            continue
        try:
            st = await bot.get_sticker_set(name)
            for s in st.stickers or []:
                cid = getattr(s, "custom_emoji_id", None)
                if not cid:
                    continue
                _PACK_IDS.append(str(cid))
                _PACK_FACE.append(getattr(s, "emoji", None) or "⚡")
            log.info("emoji pack %s loaded %s icons", name, len(_PACK_IDS))
            if _PACK_IDS:
                return
        except Exception as exc:
            log.warning("emoji pack %s: %s", name, exc)

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
    try:
        con.execute("ALTER TABLE chat_flags ADD COLUMN raid_pin INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE chat_flags ADD COLUMN last_recap INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    con.execute(
        """CREATE TABLE IF NOT EXISTS buy_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            ca TEXT,
            usd REAL,
            ts INTEGER
        )"""
    )
    try:
        con.execute("ALTER TABLE buy_log ADD COLUMN buyer TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE buy_log ADD COLUMN kind TEXT DEFAULT 'buy'")
    except sqlite3.OperationalError:
        pass
    for col, spec in (
        ("last_milestone", "REAL DEFAULT 0"),
        ("ath_mcap", "REAL DEFAULT 0"),
        ("whale_usd", "REAL DEFAULT 0"),
        ("sell_alerts", "INTEGER DEFAULT 0"),
        ("dev_wallet", "TEXT"),
        ("dev_last_bal", "REAL DEFAULT -1"),
    ):
        try:
            con.execute(f"ALTER TABLE watches ADD COLUMN {col} {spec}")
        except sqlite3.OperationalError:
            pass
    con.execute(
        """CREATE TABLE IF NOT EXISTS price_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            user_id INTEGER,
            target_mcap REAL,
            created INTEGER,
            fired INTEGER DEFAULT 0
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
    con.execute("CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT)")
    con.execute(
        """CREATE TABLE IF NOT EXISTS raid_tokens (
            cashtag TEXT PRIMARY KEY,
            chat_id INTEGER,
            invite TEXT,
            pts INTEGER DEFAULT 0,
            ca TEXT,
            mc TEXT,
            dex TEXT
        )"""
    )
    try:
        con.execute("ALTER TABLE raid_tokens ADD COLUMN dex TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        con.execute("ALTER TABLE raid_tokens ADD COLUMN announced INTEGER DEFAULT 0")
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
    for col, spec in (("last_ping", "INTEGER DEFAULT 0"), ("ping_min", "INTEGER DEFAULT 15")):
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
    con.execute(
        """CREATE TABLE IF NOT EXISTS boosts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT,
            chat_id INTEGER,
            cashtag TEXT,
            ca TEXT,
            url TEXT,
            user_id INTEGER,
            chain TEXT,
            tx_hash TEXT UNIQUE,
            usd_paid REAL,
            hours_granted REAL,
            starts_ts INTEGER,
            ends_ts INTEGER,
            status TEXT DEFAULT 'active',
            created_ts INTEGER
        )"""
    )
    try:
        con.execute("ALTER TABLE boosts ADD COLUMN reminded INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    con.execute(
        """CREATE TABLE IF NOT EXISTS user_wallets (
            user_id INTEGER,
            chain TEXT,
            address TEXT,
            updated INTEGER,
            PRIMARY KEY (user_id, chain)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS raid_blacklist (
            chat_id INTEGER,
            user_id INTEGER,
            name TEXT,
            added_by INTEGER,
            added_ts INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS raid_presets (
            chat_id INTEGER,
            name TEXT,
            likes_t INTEGER,
            rt_t INTEGER,
            re_t INTEGER,
            mins INTEGER,
            PRIMARY KEY (chat_id, name)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS raid_links (
            chat_id INTEGER,
            linked_chat_id INTEGER,
            linked_title TEXT,
            PRIMARY KEY (chat_id, linked_chat_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS raid_streaks (
            chat_id INTEGER,
            user_id INTEGER,
            last_day TEXT,
            streak INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS raid_seasons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            ended_ts INTEGER,
            snapshot TEXT
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


# ---- Ferzan bonding curves: tokens still on the curve have no DEX pool, so their
# buys come from the Ferzan launch index instead of DexScreener/GeckoTerminal.
FERZAN_API = (os.getenv("FERZAN_LAUNCH_API") or "https://launch.ferzaneco.com/api").rstrip("/")
_FZ_MISS: dict = {}


def _ferzan_curve(ca: str, since: int = 0, kind: str = "buy") -> dict:
    ca = (ca or "").strip().lower()
    if not (ca.startswith("0x") and len(ca) == 42):
        return {}
    if since == 0 and time.time() - _FZ_MISS.get(ca, 0) < 300:
        return {}
    try:
        r = requests.get(f"{FERZAN_API}/curve-by-token/{ca}", params={"since": int(since), "kind": kind}, timeout=10)
        d = r.json() if r.status_code == 200 else {}
    except Exception:
        d = {}
    if not d.get("found"):
        _FZ_MISS[ca] = time.time()
        return {}
    return d


def _ferzan_pool(ca: str) -> tuple[str, dict]:
    d = _ferzan_curve(ca)
    if not d or d.get("graduated"):
        return "", {}
    return "ferzan:" + ca.strip().lower(), {
        "name": d.get("name"), "symbol": d.get("symbol"), "address": ca, "dex": "ferzan curve",
        "source": "ferzan", "fdv_usd": d.get("mcap_usd"), "market_cap_usd": d.get("mcap_usd"),
    }


def _ferzan_trades(pool: str, last_ts: int, kind: str) -> list[dict]:
    d = _ferzan_curve(pool.split(":", 1)[1], int(last_ts or 0), kind)
    out = []
    for t in d.get("trades") or []:
        native, toks = t.get("native") or 0, t.get("tokens") or 0
        out.append({
            "ts": int(t["ts"]), "kind": kind, "volume_in_usd": t.get("usd") or 0,
            "from_token_amount": native if kind == "buy" else toks,
            "to_token_amount": toks if kind == "buy" else native,
            "tx_from_address": t.get("trader") or "", "tx_hash": t.get("tx") or "",
        })
    return out


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
        return _ferzan_pool(ca)
    row = rows[0]
    pid = row.get("id") or ""
    pool = pid.split("_", 1)[-1] if "_" in str(pid) else str(pid)
    return pool, (row.get("attributes") or {})


def _trades(net: str, pool: str, last_ts: int, kind: str = "buy") -> list[dict]:
    if str(pool).startswith("ferzan:"):
        return _ferzan_trades(pool, last_ts, kind)
    data = _gt(f"/networks/{net}/pools/{pool}/trades?trade_volume_in_usd_greater_than=1")
    rows = (data or {}).get("data") or []
    out = []
    for row in rows:
        a = row.get("attributes") or {}
        if str(a.get("kind") or "").lower() != kind:
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
    raw = (emoji or "🟢").strip()
    if raw.startswith("<tg-emoji"):
        return raw
    token = raw.split()[0] if raw else "🟢"
    if len(token) > 4:
        token = "🚀"
    n = 3 if usd < 25 else 5 if usd < 80 else 6
    return token * n


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


def _card(chain: str, ca: str, tr: dict, attrs: dict, emoji: str = "🟢", tg_url: str = "", cluster: int = 1, discord_url: str = "", x_url: str = "", whale: bool = False, vip: bool = False) -> tuple[str, InlineKeyboardMarkup]:
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
    if not pair.get("url") and attrs.get("source") == "ferzan":
        ds = (_ferzan_curve(ca) or {}).get("url") or ds  # still on its curve: chart lives on the trade page
    info = pair.get("info") or {}
    tg = (tg_url or "").strip()
    for s in info.get("socials") or []:
        if tg:
            break
        if str(s.get("type") or "").lower() in ("telegram", "tg"):
            tg = s.get("url") or ""
            break
    buy = f"https://t.me/{TRADE}?start=buy_{ca}"
    scan = {
        "sol": f"https://solscan.io/tx/{tx}",
        "solana": f"https://solscan.io/tx/{tx}",
        "base": f"https://basescan.org/tx/{tx}",
        "eth": f"https://etherscan.io/tx/{tx}",
        "ethereum": f"https://etherscan.io/tx/{tx}",
        "bsc": f"https://bscscan.com/tx/{tx}",
        "arb": f"https://arbiscan.io/tx/{tx}",
    }.get(chain, ds)
    buyer_url = scan.replace("/tx/", "/address/") if buyer and "/tx/" in scan else scan
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
    extra = []
    if tax:
        extra.append(f"tax {_esc(str(tax))}")
    if locked:
        extra.append(locked)
    if top10:
        extra.append(f"top10 {_esc(str(top10))}")
    if flags:
        extra.extend(flags)
    if cluster and cluster > 1:
        extra.append(f"{cluster} buys / 12s")
    title = f"<b>{_esc(name)}</b>   [${_esc(sym)}]   ·   {_esc(str(chain).upper())}"
    if vip:
        title = "⭐ <b>VIP</b>  " + title
    bar_line = f"{_esc(label)}  {_bar(usd, emoji)}"
    if whale:
        bar_line = f"🐳 <b>WHALE BUY</b>  🐳🐳🐳🐳🐳🐳"
    lines = [
        title,
        bar_line,
        f"<code>{_esc(ca)}</code>",
        "",
        f"{_icon('USD', 1, '💵')}  Spent   {_esc(spent_s)}   (${usd:,.2f})",
        f"{_icon('BAG', 2, '🎒')}  Got     {_esc(got)} {_esc(sym)}",
        f"{_icon('MC', 3, '🧢')}  MC      {_usd(mc)}",
        f"{_icon('LIQ', 4, '💧')}  Liq     {liq_usd}",
    ]
    if age or chg:
        lines.append(f"⏱  Age     {age or '—'}    5m {_pct('m5')}    1h {_pct('h1')}")
    if dex_name:
        lines.append(f"{_icon('ROUTE', 5, '🛣')}  Dex     {_esc(dex_name)}")
    if holders:
        lines.append(f"{_icon('HOLD', 7, '👥')}  Holders {_esc(str(holders))}")
    if extra:
        lines.append("⚠  " + " · ".join(extra))
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
    lines.append("👀 See it.  🦍 Ape it.  🚀 Send it.")
    lines.append(f'{_icon("TITLE", 0, "⚡")} <a href="{_esc(HUB)}">FERZAN ECO HUB</a>')
    text = "\n".join(lines)
    hub = HUB
    rows = [
        [
            InlineKeyboardButton("⚡ Buy on Ferzan", url=buy),
            InlineKeyboardButton("Chart", url=ds),
        ],
        [InlineKeyboardButton("Eco Hub", url=hub)],
        [InlineKeyboardButton("Boost this alert", url=boost)],
    ]
    kb = InlineKeyboardMarkup(rows)
    return text, kb


def _sell_card(chain: str, ca: str, tr: dict, attrs: dict, is_dev: bool = False) -> str:
    usd = float(tr.get("volume_in_usd") or 0)
    seller = tr.get("tx_from_address") or tr.get("origin_from_address") or ""
    tx = tr.get("tx_hash") or ""
    pair = _ds(ca)
    name = attrs.get("name") or (pair.get("baseToken") or {}).get("name") or ca[:8]
    sym = attrs.get("symbol") or (pair.get("baseToken") or {}).get("symbol") or name
    mc = attrs.get("fdv_usd") or attrs.get("market_cap_usd") or pair.get("marketCap") or pair.get("fdv") or ""
    net = GT_NET.get(chain, chain)
    ds = pair.get("url") or f"https://dexscreener.com/{net}/{ca}"
    scan = {
        "sol": f"https://solscan.io/tx/{tx}", "solana": f"https://solscan.io/tx/{tx}",
        "base": f"https://basescan.org/tx/{tx}", "eth": f"https://etherscan.io/tx/{tx}",
        "ethereum": f"https://etherscan.io/tx/{tx}", "bsc": f"https://bscscan.com/tx/{tx}",
        "arb": f"https://arbiscan.io/tx/{tx}",
    }.get(chain, ds)
    header = "🚨 <b>DEV WALLET SELL</b>" if is_dev else "🔴 SELL"
    return (
        f"{header}\n"
        f"<b>{_esc(name)}</b>   [${_esc(sym)}]   ·   {_esc(str(chain).upper())}\n"
        f"🔻🔻🔻\n"
        f"<code>{_esc(ca)}</code>\n\n"
        f"💵  Sold   ${usd:,.2f}\n"
        f"🧢  MC     {_usd(mc)}\n"
        + (f"👤  <a href=\"{_esc(scan)}\">Seller / Txn</a>\n" if seller else f"<a href=\"{_esc(scan)}\">Txn</a>\n")
        + f"📉  <a href=\"{_esc(ds)}\">Chart</a>"
    )


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
    arg = (context.args[0] if context.args else "")
    if arg.startswith("trk_") and update.effective_chat and update.effective_chat.type != "private":
        parts = arg.split("_", 2)
        if len(parts) == 3 and parts[1] in GT_NET:
            if not await _is_chat_admin(update):
                await update.effective_message.reply_text("Only a group admin can turn on buy alerts. Ask an admin to run /add with the chain and contract address.")
                return
            context.args = [parts[1], parts[2]]
            await track(update, context)
            return
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
        "/stats /price /dex /market /vote /raid\n\n"
        "⚔️ Raids:\n"
        "/raid <x link> [likes rt re mins]  or  /raid <link> <preset>\n"
        "/addtag /tags /deltag  — save reusable raid presets\n"
        "/linkgroup /unlinkgroup /linkedgroups  — multi-group raids, shared leaderboard\n"
        "/blacklist add|remove|list  — exclude wallets/users from raid points\n"
        "/exportpoints  — CSV of this chat's raid leaderboard + linked wallets\n"
        "/linkwallet sol|evm <address>  /mywallet  /unlinkwallet\n"
        "/raidpin on|off  — auto-pin raid posts (off by default)\n"
        "/season  /lastseason  — close out + reset the leaderboard\n"
        "/payoutpreview <$/pt>  /exportpayout <$/pt>  — $ owed per raider\n\n"
        "📈 Growth & safety:\n"
        "Auto: milestone alerts, new-ATH alerts, 🐳 whale-buy cards, 24h recap\n"
        "/setwhale <$>  — whale-buy threshold (default 10x your min buy)\n"
        "/sellalerts on|off  /setdev <wallet>  — sell + dev-wallet-move alerts\n"
        "/alert <mcap>  — DM me when it hits that market cap\n"
        "/topbuyers [7d]  — biggest buyers leaderboard\n\n"
        "📣 Promotion:\n"
        "/trending  buy a Trending-board boost\n"
        "/raidboost  buy a Raid-Leaderboard boost\n"
        "/paid <txhash>  activate the boost you just paid for\n"
        "/boosts  see what's currently boosted\n"
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


def _parse_money(raw: str) -> float | None:
    raw = (raw or "").strip().lower().replace("$", "").replace(",", "")
    mult = 1
    if raw.endswith("k"):
        mult, raw = 1_000, raw[:-1]
    elif raw.endswith("m"):
        mult, raw = 1_000_000, raw[:-1]
    elif raw.endswith("b"):
        mult, raw = 1_000_000_000, raw[:-1]
    try:
        return float(raw) * mult
    except ValueError:
        return None


async def setwhale_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    amt = _parse_money(args[0]) if args else None
    if not amt or amt <= 0:
        await update.effective_message.reply_text(
            "Usage: /setwhale <usd>\nExample: /setwhale 1000\n"
            "Buys at or above this get the 🐳 WHALE BUY treatment. Send 0 to use the default (10x your min buy floor)."
        )
        return
    con = _db()
    con.execute("UPDATE watches SET whale_usd=? WHERE chat_id=?", (amt, update.effective_chat.id))
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"🐳 Whale threshold set to ${amt:,.0f}")


async def sellalerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args[0] if context.args else "").lower()
    if arg not in {"on", "off"}:
        await update.effective_message.reply_text("Usage: /sellalerts on   or   /sellalerts off")
        return
    con = _db()
    con.execute("UPDATE watches SET sell_alerts=? WHERE chat_id=?", (1 if arg == "on" else 0, update.effective_chat.id))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Sell alerts ON." if arg == "on" else "Sell alerts OFF.")


async def setdev_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        con = _db()
        row = con.execute("SELECT dev_wallet FROM watches WHERE chat_id=?", (update.effective_chat.id,)).fetchone()
        con.close()
        await update.effective_message.reply_text(
            f"Dev wallet currently: `{row[0]}`" if row and row[0] else
            "No dev wallet set.\nUsage: /setdev <wallet address>\n"
            "The bot will flag it here if the balance drops 5%+ (likely a sell or transfer).",
            parse_mode="Markdown",
        )
        return
    addr = args[0].strip()
    con = _db()
    con.execute("UPDATE watches SET dev_wallet=?, dev_last_bal=-1 WHERE chat_id=?", (addr, update.effective_chat.id))
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"👀 Watching dev wallet `{addr}` for balance drops.", parse_mode="Markdown")


async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    amt = _parse_money(args[0]) if args else None
    if not amt or amt <= 0:
        await update.effective_message.reply_text(
            "Usage: /alert <market cap>\nExample: /alert 1m\n"
            "Run this in a project chat with a token paired — I'll DM you when it hits that market cap.\n"
            "Message me first (/start in DM) so I'm able to DM you."
        )
        return
    row = _watch(update.effective_chat.id)
    if not row:
        await update.effective_message.reply_text("Pair a token here first. /setup")
        return
    con = _db()
    con.execute(
        "INSERT INTO price_alerts(chat_id, user_id, target_mcap, created) VALUES(?,?,?,?)",
        (update.effective_chat.id, update.effective_user.id, amt, int(time.time())),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"🔔 I'll DM you when this hits {_usd(amt)} market cap.")


async def topbuyers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    window = 7 * 86400 if args and args[0].lower() in ("7d", "week") else 86400
    label = "7 days" if window > 86400 else "24h"
    con = _db()
    rows = con.execute(
        "SELECT buyer, SUM(usd) s, COUNT(*) n FROM buy_log "
        "WHERE chat_id=? AND kind='buy' AND ts>=? AND buyer<>'' "
        "GROUP BY buyer ORDER BY s DESC LIMIT 10",
        (update.effective_chat.id, int(time.time()) - window),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text(f"No buys logged in the last {label}.")
        return
    lines = [f"{i+1}. {b[:6]}…{b[-4:]}  ${s:,.0f} ({n} buys)" for i, (b, s, n) in enumerate(rows)]
    await update.effective_message.reply_text(f"🐋 Top buyers — last {label}\n" + "\n".join(lines))


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


async def raidpin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    arg = (context.args[0] if context.args else "").lower()
    if arg not in {"on", "off"}:
        con = _db()
        row = con.execute(
            "SELECT raid_pin FROM chat_flags WHERE chat_id=?", (update.effective_chat.id,)
        ).fetchone()
        con.close()
        on = bool(row and int(row[0] or 0))
        await update.effective_message.reply_text(
            f"Raid auto-pin is currently {'ON' if on else 'OFF'} (default off).\n"
            "Usage: /raidpin on   or   /raidpin off"
        )
        return
    on = 1 if arg == "on" else 0
    con = _db()
    con.execute(
        "INSERT INTO chat_flags(chat_id, tape, mute_until, raid_pin) VALUES(?,1,0,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET raid_pin=excluded.raid_pin",
        (update.effective_chat.id, on),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(
        "Raid posts will now be pinned." if on else "Raid posts will no longer be auto-pinned."
    )


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
    buy = f"https://t.me/{TRADE}?start=buy_{ca}"
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
    buy = f"https://t.me/{TRADE}?start=buy_{ca}"
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
        await update.effective_message.reply_text(cap, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


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
    pending = context.user_data.get("boost_pending")
    if pending and pending.get("hours") and not pending.get("target_set") and not forced_ca and not txt.startswith("/"):
        if pending.get("kind") == "ads":
            await _finalize_boost_target(msg, context, txt.strip())
            return
        ca_candidate = _extract_ca(txt) or txt.strip()
        if ca_candidate:
            await _finalize_boost_target(msg, context, ca_candidate)
            return
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
    buy = f"https://t.me/{TRADE}?start=buy_{ca}"
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


def _raid_ping_kb(rid: int, url: str) -> InlineKeyboardMarkup:
    """Reminder cards get the same one-tap Like / Repost / Reply as the first
    card (":p" marks the tap as coming from a reminder). No Stop button:
    reminders also go to the raid channel."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Open Post", url=url)],
            [
                InlineKeyboardButton("❤️ Like", callback_data=f"rd:like:{rid}:p"),
                InlineKeyboardButton("🔁 Repost", callback_data=f"rd:rt:{rid}:p"),
                InlineKeyboardButton("💬 Reply", callback_data=f"rd:re:{rid}:p"),
            ],
            [InlineKeyboardButton("🏆 Raid Leaderboard", url=_raid_board_url())],
        ]
    )


def _raid_ping_text(tag, url, lh, lt, rh, rt, eh, et) -> str:
    return (
        f"{_icon('TITLE', 0, '⚡')}  <b>{_esc(tag or 'RAID')}</b>\n"
        f"{_icon('ROUTE', 5, '📡')}  LIVE RAID\n"
        f"────────────────\n"
        f"{_icon('USD', 1, '❤️')}  Likes      <b>{lh}</b> / {lt}\n"
        f"{_icon('BAG', 2, '🔁')}  Reposts    <b>{rh}</b> / {rt}\n"
        f"{_icon('TG', 8, '💬')}  Replies    <b>{eh}</b> / {et}\n"
        f"────────────────\n"
        f"{_icon('BUYER', 6, '🔗')}  <a href=\"{_esc(url)}\">Open the post</a>\n"
        f"<i>Tap ❤️ 🔁 💬 after you smash. See it. Ape it. Send it.</i>"
    )


def _raid_kb(rid: int, url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Open Post", url=url),
                InlineKeyboardButton("Stop", callback_data=f"rd:stop:{rid}"),
            ],
            [
                InlineKeyboardButton("❤️ Like", callback_data=f"rd:like:{rid}"),
                InlineKeyboardButton("🔁 Repost", callback_data=f"rd:rt:{rid}"),
                InlineKeyboardButton("💬 Reply", callback_data=f"rd:re:{rid}"),
            ],
            [InlineKeyboardButton("🏆 Raid Leaderboard", url=_raid_board_url())],
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
            "/raid <x link> <preset name>   — use a saved /addtag preset\n"
            "Example:\n"
            "/raid https://x.com/user/status/123 5 5 2 60\n"
            "/raidstop  /raidlb  /queue <link>\n"
            "/addtag  /tags  /linkgroup  /linkedgroups"
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
    preset = None
    words = [a for a in context.args[1:] if not a.isdigit() and not a.startswith("$")]
    if words and not nums:
        con_p = _db()
        prow = con_p.execute(
            "SELECT likes_t, rt_t, re_t, mins FROM raid_presets WHERE chat_id=? AND name=?",
            (update.effective_chat.id, words[0].lower()),
        ).fetchone()
        con_p.close()
        if prow:
            preset = prow
    if preset:
        likes_t, rt_t, re_t, mins = int(preset[0]), int(preset[1]), int(preset[2]), int(preset[3])
    else:
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
        "UPDATE raids SET msg_id=?, last_ping=?, ping_min=15 WHERE id=?",
        (msg.message_id, int(time.time()), rid),
    )
    con.commit()
    con.close()
    # Auto-pin is opt-in per project chat (off by default) — /raidpin on to enable it.
    con_pf = _db()
    pin_row = con_pf.execute(
        "SELECT raid_pin FROM chat_flags WHERE chat_id=?", (update.effective_chat.id,)
    ).fetchone()
    con_pf.close()
    if pin_row and int(pin_row[0] or 0):
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
    # Multi-group raids: any sister chats linked with /linkgroup get the same raid card,
    # same raid id — taps there feed the same leaderboard (see raid_cb, which scores against
    # the raid's home chat_id rather than wherever the tap happened).
    con_l = _db()
    linked = con_l.execute(
        "SELECT linked_chat_id FROM raid_links WHERE chat_id=?", (update.effective_chat.id,)
    ).fetchall()
    con_l.close()
    for (linked_id,) in linked:
        try:
            if banner.exists():
                with banner.open("rb") as fh:
                    await context.bot.send_photo(
                        linked_id, fh, caption=start, parse_mode="HTML", reply_markup=kb
                    )
            else:
                await context.bot.send_message(linked_id, start, parse_mode="HTML", reply_markup=kb)
        except Exception as exc:
            log.warning("raid multi-group send %s: %s", linked_id, exc)
    # Register the token on the leaderboard the moment a raid starts (0 pts is fine) and post/refresh
    # the board right away — previously the board only appeared after the first tap, so a raid group
    # with no taps yet showed no leaderboard at all.
    if tag:
        try:
            con3 = _db()
            con3.execute(
                "INSERT OR IGNORE INTO raid_tokens(cashtag, chat_id, invite, ca, pts) VALUES(?,?,?,?,0)",
                (tag, update.effective_chat.id, f"https://t.me/{update.effective_chat.username}" if update.effective_chat.username else "", w[1] if w else ""),
            )
            con3.commit()
            con3.close()
        except Exception as exc:
            log.warning("raid token register %s", exc)
        try:
            await _post_board(context.bot)
        except Exception as exc:
            log.warning("raid board post-on-start %s", exc)


async def _bump_token(bot, chat, tag: str, ca: str = "") -> None:
    await _load_pack(bot)
    tag = (tag or "").strip()
    if not tag:
        return
    mc = ""
    dex = ""
    if ca:
        pair = _ds(ca) or {}
        mc = _usd(pair.get("marketCap") or pair.get("fdv"))
        dex = (pair.get("dexId") or "").title()
    con = _db()
    tg_row = con.execute("SELECT tg_url FROM watches WHERE chat_id=?", (chat.id,)).fetchone()
    invite = ((tg_row[0] if tg_row else "") or "").strip()
    if not invite and getattr(chat, "username", None):
        invite = f"https://t.me/{chat.username}"
    row = con.execute(
        "SELECT pts, COALESCE(announced,0) FROM raid_tokens WHERE cashtag=?", (tag,)
    ).fetchone()
    first = not row
    announced = int(row[1]) if row else 0
    if row:
        con.execute(
            "UPDATE raid_tokens SET pts=pts+1, chat_id=?, invite=?, ca=?, mc=?, dex=? WHERE cashtag=?",
            (chat.id, invite, ca, mc, dex, tag),
        )
        pts = int(row[0]) + 1
    else:
        con.execute(
            "INSERT INTO raid_tokens(cashtag,chat_id,invite,pts,ca,mc,dex) VALUES(?,?,?,?,?,?,?)",
            (tag, chat.id, invite, 1, ca, mc, dex),
        )
        pts = 1
    con.commit()
    con.close()
    if first or announced == 0:
        rows_kb = []
        if invite:
            rows_kb.append([InlineKeyboardButton("Open group", url=invite)])
        rows_kb.append([InlineKeyboardButton("Buy", url=f"https://t.me/{TRADE}?start=buy_{ca}" if ca else HUB)])
        rows_kb.append([InlineKeyboardButton("Boost", url=HUB)])
        kb = InlineKeyboardMarkup(rows_kb)
        try:
            cap = (
                f"{_icon('TITLE', 0, 'F')} <b>{_esc(tag)}</b> entered the Raid Leaderboard.\n\n"
                + (
                    f"{_icon('TG', 8, 'F')} Group: <a href=\"{_esc(invite)}\">Open group</a>\n"
                    if invite
                    else f"{_icon('TG', 8, 'F')} Group: /settelegram in their chat\n"
                )
                + f"{_icon('USD', 1, 'F')} Points: {pts}\n"
                + f"{_icon('MC', 3, 'F')} Market cap: {mc or '—'}\n\n"
                + f"<i>See it. Ape it. Send it.</i>"
            )
            await bot.send_message(
                RAID_CH, cap, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
            )
            con3 = _db()
            con3.execute("UPDATE raid_tokens SET announced=1 WHERE cashtag=?", (tag,))
            con3.commit()
            con3.close()
        except Exception as exc:
            log.warning("lb enter %s", exc)
    await _post_board(bot)


async def _post_board(bot) -> str:
    await _load_pack(bot)
    now = int(time.time())
    con = _db()
    boost_rows = con.execute(
        "SELECT cashtag FROM boosts WHERE kind='raid' AND status='active' AND ends_ts>? "
        "ORDER BY usd_paid DESC, ends_ts DESC",
        (now,),
    ).fetchall()
    token_rows = con.execute(
        "SELECT cashtag, pts, invite, mc, ca, dex FROM raid_tokens ORDER BY pts DESC"
    ).fetchall()
    mid = con.execute("SELECT v FROM kv WHERE k='raid_board_msg'").fetchone()
    con.close()
    if not token_rows and not boost_rows:
        return "no tokens on the board yet"
    token_by_tag = {t[0]: t for t in token_rows}
    ordered = []
    seen = set()
    # Boosted tokens (bigger spend first) lead the board regardless of organic points; the rest
    # follow sorted by points, same as before.
    for (tag,) in boost_rows:
        if not tag or tag in seen:
            continue
        t = token_by_tag.get(tag) or (tag, 0, "", "", "", "")
        ordered.append((*t, True))
        seen.add(tag)
    for t in token_rows:
        if t[0] in seen:
            continue
        ordered.append((*t, False))
        seen.add(t[0])
    ordered = ordered[:10]
    if not ordered:
        return "no tokens on the board yet"
    medals = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = [f"{_icon('TITLE', 0, 'F')} <b>FERZAN RAID LEADERBOARD</b>\n"]
    btn_rows = []
    for i, (tag, pts, invite, mc, ca, dex, boosted) in enumerate(ordered):
        name = _esc(tag)
        buy = f"https://t.me/{TRADE}?start=buy_{ca}" if ca else HUB
        title = f"<a href=\"{_esc(invite)}\">{name}</a>" if invite else name
        badge = " 🚀" if boosted else ""
        lines.append(f"{medals[i]}  <b>{title}</b>{badge}")
        lines.append(f"{_icon('USD', 1, 'F')} Points: {pts}")
        lines.append(f"{_icon('MC', 3, 'F')} Market cap: {mc or '—'}")
        lines.append(f"{_icon('ROUTE', 5, 'F')} Dex: {_esc(dex or '—')}")
        lines.append(f"{_icon('BAG', 2, 'F')} <a href=\"{_esc(buy)}\">Buy</a>\n")
        btn_rows.append([InlineKeyboardButton(f"Buy {tag[:16]}", url=buy)])
    lines.append("<i>🚀 = boosted. See it. Ape it. Send it.</i>")
    kb = InlineKeyboardMarkup(btn_rows[:8])
    text = "\n".join(lines)
    if mid:
        try:
            await bot.edit_message_caption(
                chat_id=RAID_CH,
                message_id=int(mid[0]),
                caption=text,
                parse_mode="HTML",
                reply_markup=kb,
            )
            return ""
        except Exception:
            try:
                await bot.edit_message_text(
                    chat_id=RAID_CH,
                    message_id=int(mid[0]),
                    text=text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True,
                )
                return ""
            except Exception as exc:
                log.warning("lb edit %s", exc)
                try:
                    await bot.delete_message(RAID_CH, int(mid[0]))
                except Exception:
                    pass
    try:
        ban = _banner()
        if ban:
            with ban.open("rb") as fh:
                msg = await bot.send_photo(
                    RAID_CH, fh, caption=text, parse_mode="HTML", reply_markup=kb
                )
        else:
            msg = await bot.send_message(
                RAID_CH, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
            )
        con = _db()
        con.execute(
            "INSERT OR REPLACE INTO kv(k,v) VALUES('raid_board_msg',?)",
            (str(msg.message_id),),
        )
        con.commit()
        con.close()
        try:
            await bot.pin_chat_message(RAID_CH, msg.message_id, disable_notification=True)
        except Exception:
            pass
        return ""
    except Exception as exc:
        log.warning("lb board %s", exc)
        return str(exc)


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
    if kind == "resume":
        await q.answer("Raid resumed")
        extra = 60 * 60
        con.execute(
            "UPDATE raids SET active=1, ends=? WHERE id=?",
            (int(time.time()) + extra, rid),
        )
        con.commit()
        row = con.execute(
            "SELECT url, cashtag FROM raids WHERE id=?", (rid,)
        ).fetchone()
        con.close()
        url = row[0] if row else "https://x.com"
        try:
            await q.edit_message_caption(
                caption=f"▶️ Raid resumed — 60 more minutes.\n<a href=\"{_esc(url)}\">Open the post</a>",
                parse_mode="HTML",
                reply_markup=_raid_kb(rid, url),
            )
        except Exception:
            await q.edit_message_text(
                f"▶️ Raid resumed — 60 more minutes.",
                reply_markup=_raid_kb(rid, url),
            )
        return
    if kind == "stop":
        chat = q.message.chat if q.message else None
        if chat and chat.type in ("group", "supergroup"):
            try:
                member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
                is_admin = member.status in ("administrator", "creator")
            except Exception:
                is_admin = False
            if not is_admin:
                con.close()
                await q.answer("Only group admins can stop a raid.", show_alert=True)
                return
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
    # Bug fix: this used to INSERT the tap and increment blindly with a WHERE id=? AND active=1
    # clause. If the raid had already ended (timed out or /raidstop'd) by the time someone tapped —
    # very easy to hit once a raid is more than an hour old, exactly what was happening on the
    # cards sitting in the raid group and in project chats — the UPDATE silently touched 0 rows
    # while the code still said "+1" and re-showed the card with unchanged counts. That's the
    # "these buttons don't work" symptom. Now an ended raid tells you plainly instead of faking it,
    # and the tap isn't wasted from raid_taps' per-user/per-kind dedup if the raid gets resumed.
    live_row = con.execute("SELECT active, chat_id FROM raids WHERE id=?", (rid,)).fetchone()
    if not live_row:
        con.close()
        await q.answer("This raid no longer exists.", show_alert=True)
        return
    if not live_row[0]:
        con.close()
        await q.answer("This raid has ended — tap Resume Raid, or start a new one with /raid.", show_alert=True)
        return
    home_chat_id = live_row[1] or update.effective_chat.id
    u = update.effective_user
    blocked = con.execute(
        "SELECT 1 FROM raid_blacklist WHERE chat_id=? AND user_id=?", (home_chat_id, u.id)
    ).fetchone()
    if blocked:
        con.close()
        await q.answer("You're blacklisted from raid points in this project.", show_alert=True)
        return
    cur = con.execute(
        "INSERT OR IGNORE INTO raid_taps(raid_id, user_id, kind) VALUES(?,?,?)",
        (rid, u.id, kind),
    )
    if cur.rowcount == 0:
        con.close()
        await q.answer("Already counted on this button.", show_alert=False)
        return
    weight = RAID_WEIGHTS.get(kind, 1)
    bonus = _bump_streak(con, home_chat_id, u.id)
    # Points pool on the raid's home chat, not wherever the button was tapped — this is what
    # lets a multi-group raid (posted into several linked sister chats) share one leaderboard.
    con.execute(
        "INSERT INTO raid_scores(chat_id, user_id, name, pts) VALUES(?,?,?,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET pts = pts + excluded.pts, name=excluded.name",
        (home_chat_id, u.id, u.full_name or u.username or str(u.id), weight + bonus),
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
        try:
            await context.bot.send_message(
                update.effective_chat.id,
                f"{_icon('TITLE', 0, 'F')} <b>RAID ENDED · TARGETS HIT</b>\n"
                f"{_esc(d.get('cashtag') or 'RAID')}\n\n"
                f"Resume if you want another 60 minutes.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Open Post", url=d["url"])],
                        [
                            InlineKeyboardButton("Resume Raid", callback_data=f"rd:resume:{rid}"),
                            InlineKeyboardButton("Leaderboard", url=_raid_board_url()),
                        ],
                    ]
                ),
            )
        except Exception:
            pass
    else:
        await q.answer("+1")
    w = _watch(update.effective_chat.id)
    try:
        await _bump_token(context.bot, update.effective_chat, d.get("cashtag") or "", w[1] if w else "")
    except Exception as exc:
        log.warning("raid tap bump_token %s", exc)
    if len(parts) > 3 and parts[3] == "p":  # tapped on a reminder card
        txt = _raid_ping_text(d.get("cashtag"), d["url"], d["likes_h"], d["likes_t"],
                              d["rt_h"], d["rt_t"], d["re_h"], d["re_t"])
        kb = _raid_ping_kb(rid, d["url"])
    else:
        txt = _raid_text(d)
        kb = _raid_kb(rid, d["url"])
    try:
        if q.message.photo:
            await q.edit_message_caption(caption=txt, parse_mode="HTML", reply_markup=kb)
        else:
            await q.edit_message_text(txt, parse_mode="HTML", reply_markup=kb)
    except Exception as exc:
        log.warning("raid tap card refresh %s", exc)


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
    blocked = con.execute(
        "SELECT 1 FROM raid_blacklist WHERE chat_id=? AND user_id=?",
        (update.effective_chat.id, u.id),
    ).fetchone()
    if blocked:
        con.close()
        await update.effective_message.reply_text("You're blacklisted from raid points in this project.")
        return
    bonus = _bump_streak(con, update.effective_chat.id, u.id)
    total = RAID_WEIGHTS["join"] + bonus
    con.execute(
        "INSERT INTO raid_scores(chat_id, user_id, name, pts) VALUES(?,?,?,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET pts = pts + excluded.pts, name=excluded.name",
        (update.effective_chat.id, u.id, u.full_name or u.username or str(u.id), total),
    )
    con.commit()
    con.close()
    msg = f"+{total} raid point{'s' if total != 1 else ''} for {u.first_name}"
    if bonus:
        msg += f" (🔥 streak bonus +{bonus}!)"
    await update.effective_message.reply_text(msg)


async def lb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute(
        "SELECT name, pts FROM raid_scores WHERE chat_id=? ORDER BY pts DESC LIMIT 10",
        (update.effective_chat.id,),
    ).fetchall()
    con.close()
    lines = ["🏆 This chat — raiders"]
    if not rows:
        lines.append("No taps in this chat yet.")
    else:
        for i, (name, pts) in enumerate(rows, 1):
            lines.append(f"{i}. {name}  {pts}  {_tier_for_pts(pts)}")
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Token leaderboard", url=_raid_board_url())]]
    )
    await update.effective_message.reply_text("\n".join(lines), reply_markup=kb)
    con2 = _db()
    live = con2.execute(
        "SELECT cashtag FROM raids WHERE chat_id=? ORDER BY id DESC LIMIT 1",
        (update.effective_chat.id,),
    ).fetchone()
    w = _watch(update.effective_chat.id)
    tag = ((live[0] if live else "") or "").strip()
    if not tag and w:
        pair = _ds(w[1]) or {}
        sym = ((pair.get("baseToken") or {}).get("symbol") or "TOKEN")
        tag = "$" + str(sym)
    if tag and not str(tag).startswith("$"):
        tag = "$" + tag
    try:
        con2.execute(
            "INSERT OR IGNORE INTO raid_tokens(cashtag, chat_id, pts) VALUES(?,?,1)",
            (tag, update.effective_chat.id),
        )
        con2.commit()
    except Exception:
        pass
    con2.close()
    await _bump_token(context.bot, update.effective_chat, tag, w[1] if w else "")
    err = await _post_board(context.bot)
    if err:
        await update.effective_message.reply_text(f"Board failed → {RAID_CH}\n{err}")
    else:
        await update.effective_message.reply_text(f"Board updated → {RAID_CH}")


async def raidevent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🎯 Raid event is ON in this chat.\n"
        "Admin posts /raid <x link>. Members /raidjoin after they engage.\n"
        "/relb for the event board."
    )


async def raidgoal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    nums = [int(a) for a in (context.args or []) if a.isdigit()]
    if len(nums) < 3:
        await update.effective_message.reply_text(
            "Usage: /raidgoal <likes> <reposts> <replies>\nExample: /raidgoal 25 25 10"
        )
        return
    likes, rts, reps = nums[0], nums[1], nums[2]
    con = _db()
    con.execute(
        "UPDATE raids SET likes_t=?, rt_t=?, re_t=? WHERE chat_id=? AND active=1",
        (likes, rts, reps, update.effective_chat.id),
    )
    n = con.total_changes
    con.commit()
    con.close()
    if not n:
        await update.effective_message.reply_text("No live raid in this chat.")
        return
    await update.effective_message.reply_text(f"Goals set: ❤️ {likes}  🔁 {rts}  💬 {reps}")


async def raidint_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /raidint 15    (minutes, 2–60). Default 15.")
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
        expired = con.execute(
            "SELECT kind FROM boosts WHERE status='active' AND ends_ts<=?", (now,)
        ).fetchall()
        if expired:
            con.execute("UPDATE boosts SET status='expired' WHERE status='active' AND ends_ts<=?", (now,))
            con.commit()
            kinds = {k for (k,) in expired}
            if "raid" in kinds:
                try:
                    await _post_board(context.bot)
                except Exception as exc:
                    log.warning("raid board refresh on expiry %s", exc)
            if "trending" in kinds:
                try:
                    await _post_trending_board(context.bot)
                except Exception as exc:
                    log.warning("trending board refresh on expiry %s", exc)
    except sqlite3.OperationalError:
        pass
    try:
        soon = con.execute(
            "SELECT id, kind, chat_id, user_id, cashtag, ca, url FROM boosts "
            "WHERE status='active' AND reminded=0 AND ends_ts>? AND ends_ts<=?",
            (now, now + 3600),
        ).fetchall()
        for bid, bkind, chat_id, user_id, tag, ca, url in soon:
            label = {"raid": "Raid Leaderboard boost", "trending": "Trending boost", "ads": "Buy-card button ad"}.get(
                bkind, bkind
            )
            name = tag or ca or url or "your boost"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Renew", callback_data=f"bst:{bkind}:renew:{bid}")]])
            text = f"⏰ Your {label} for {name} expires in under an hour. Tap to renew at the same tier."
            sent = False
            if user_id:
                try:
                    await context.bot.send_message(user_id, text, reply_markup=kb)
                    sent = True
                except Exception:
                    pass
            if not sent and chat_id:
                try:
                    await context.bot.send_message(chat_id, text, reply_markup=kb)
                except Exception as exc:
                    log.warning("boost reminder %s", exc)
            con.execute("UPDATE boosts SET reminded=1 WHERE id=?", (bid,))
        if soon:
            con.commit()
    except Exception as exc:
        log.warning("boost reminders %s", exc)
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
            con.commit()
            ok_l = "✅" if int(lh) >= int(lt) else "❌"
            ok_r = "✅" if int(rh) >= int(rt) else "❌"
            ok_e = "✅" if int(eh) >= int(et) else "❌"
            end = (
                f"{_icon('TITLE', 0, 'F')} <b>RAID ENDED · TIMEOUT</b>\n"
                f"{_esc(tag or 'RAID')} — 60 minutes up.\n\n"
                f"{_icon('USD', 1, 'F')} Likes  {lh} | {lt}  {ok_l}\n"
                f"{_icon('BAG', 2, 'F')} Reposts  {rh} | {rt}  {ok_r}\n"
                f"{_icon('TG', 8, 'F')} Replies  {eh} | {et}  {ok_e}\n\n"
                f"<i>Resume to keep the same post live.</i>"
            )
            ekb = InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Open Post", url=url)],
                    [
                        InlineKeyboardButton("Resume Raid", callback_data=f"rd:resume:{rid}"),
                        InlineKeyboardButton("Leaderboard", url=_raid_board_url()),
                    ],
                ]
            )
            try:
                await context.bot.send_message(
                    chat_id, end, parse_mode="HTML", reply_markup=ekb, disable_web_page_preview=True
                )
            except Exception as exc:
                log.warning("raid end %s", exc)
            continue
        every = max(2, int(ping_min or 15)) * 60
        if now - int(last_ping or 0) < every:
            continue
        txt = (
            f"{_icon('TITLE', 0, '⚡')}  <b>{_esc(tag or 'RAID')}</b>\n"
            f"{_icon('ROUTE', 5, '📡')}  LIVE RAID\n"
            f"────────────────\n"
            f"{_icon('USD', 1, '❤️')}  Likes      <b>{lh}</b> / {lt}\n"
            f"{_icon('BAG', 2, '🔁')}  Reposts    <b>{rh}</b> / {rt}\n"
            f"{_icon('TG', 8, '💬')}  Replies    <b>{eh}</b> / {et}\n"
            f"────────────────\n"
            f"{_icon('BUYER', 6, '🔗')}  <a href=\"{_esc(url)}\">Open the post</a>\n"
            f"<i>See it. Ape it. Send it.</i>"
        )
        txt = _raid_ping_text(tag, url, lh, lt, rh, rt, eh, et)
        kb = _raid_ping_kb(rid, url)
        banner = Path("/opt/ferzan/app/raid.jpg")
        if not banner.exists():
            banner = Path(__file__).resolve().parent / "raid.jpg"
        async def _send(dest):
            if banner.exists():
                with banner.open("rb") as fh:
                    await context.bot.send_photo(
                        dest, fh, caption=txt, parse_mode="HTML", reply_markup=kb
                    )
            else:
                await context.bot.send_message(
                    dest, txt, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
                )
        try:
            await _send(chat_id)
            if RAID_CH and str(chat_id) != RAID_CH:
                try:
                    await _send(RAID_CH)
                except Exception as exc:
                    log.warning("raid ping channel %s", exc)
            con.execute("UPDATE raids SET last_ping=? WHERE id=?", (now, rid))
        except Exception as exc:
            log.warning("raid ping %s: %s", chat_id, exc)
    con.commit()
    try:
        rows = list(con.execute(
            "SELECT chat_id, chain, ca, pool, last_ts, min_usd, emoji, tg_url, discord_url, x_url, "
            "last_milestone, ath_mcap, whale_usd, sell_alerts, dev_wallet, dev_last_bal FROM watches"
        ))
    except sqlite3.OperationalError:
        rows = [(*r, "", "", "", 0, 0, 0, 0, "", -1) for r in con.execute("SELECT chat_id, chain, ca, pool, last_ts, min_usd, emoji FROM watches")]
    for (chat_id, chain, ca, pool, last_ts, min_usd, emoji, tg_url, discord_url, x_url,
         last_milestone, ath_mcap, whale_usd, sell_alerts, dev_wallet, dev_last_bal) in rows:
        tape, mute_until = _flags(chat_id)
        if not tape or mute_until > time.time():
            continue
        floor = float(min_usd or MIN_USD)
        whale_floor = float(whale_usd or 0) or max(500.0, floor * 10)
        net = GT_NET.get(chain, chain)
        if str(pool).startswith("ferzan:"):
            fz = _ferzan_curve(ca)
            if fz.get("graduated"):
                newp, _ = _pool_for(chain, ca)
                if newp and not newp.startswith("ferzan:"):
                    con.execute("UPDATE watches SET pool=? WHERE chat_id=? AND ca=?", (newp, chat_id, ca))
                    con.commit()
                    pool = newp
        trades = _trades(net, pool, int(last_ts or 0), "buy")
        _, attrs = _pool_for(chain, ca)
        vip = bool(con.execute(
            "SELECT 1 FROM boosts WHERE kind='trending' AND status='active' AND (ca=? OR cashtag LIKE ?) LIMIT 1",
            (ca, f"%{(attrs.get('symbol') or '')}%"),
        ).fetchone())
        newest = last_ts
        for tr in trades:
            usd = float(tr.get("volume_in_usd") or 0)
            if usd < floor:
                newest = max(newest, tr["ts"])
                continue
            cluster = sum(1 for x in trades if abs(x["ts"] - tr["ts"]) <= 12)
            whale = usd >= whale_floor
            text, kb = _card(chain, ca, tr, attrs, emoji or "🟢", tg_url or "", cluster, discord_url, x_url, whale, vip)
            buyer = tr.get("tx_from_address") or tr.get("origin_from_address") or ""
            con.execute(
                "INSERT INTO buy_log(chat_id, ca, usd, ts, buyer, kind) VALUES(?,?,?,?,?,'buy')",
                (chat_id, ca, usd, tr["ts"], buyer),
            )
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
        # Sell alerts — opt-in per project via /sellalerts on.
        if sell_alerts:
            try:
                sells = _trades(net, pool, int(last_ts or 0), "sell")
                for tr in sells:
                    susd = float(tr.get("volume_in_usd") or 0)
                    if susd < floor:
                        newest = max(newest, tr["ts"])
                        continue
                    seller = (tr.get("tx_from_address") or tr.get("origin_from_address") or "").lower()
                    is_dev = bool(dev_wallet) and seller == str(dev_wallet).lower()
                    stext = _sell_card(chain, ca, tr, attrs, is_dev)
                    con.execute(
                        "INSERT INTO buy_log(chat_id, ca, usd, ts, buyer, kind) VALUES(?,?,?,?,?,'sell')",
                        (chat_id, ca, susd, tr["ts"], seller),
                    )
                    try:
                        await context.bot.send_message(chat_id, stext, parse_mode="HTML", disable_web_page_preview=True)
                    except Exception as exc:
                        log.warning("sell post %s %s", chat_id, exc)
                    newest = max(newest, tr["ts"])
            except Exception as exc:
                log.warning("sell scan %s %s: %s", chat_id, ca, exc)
        # Milestone + ATH alerts — once per crossing, off the freshest market cap snapshot.
        try:
            snap = _ds(ca, chain)
            mc_now = float(snap.get("marketCap") or snap.get("fdv") or 0)
            if mc_now > 0:
                hit = _next_milestone(mc_now, float(last_milestone or 0))
                if hit:
                    sym = attrs.get("symbol") or ""
                    try:
                        await context.bot.send_message(
                            chat_id,
                            f"🎉 <b>{_esc(attrs.get('name') or ca[:8])}</b> [${_esc(sym)}] just crossed <b>{_usd(hit)}</b> market cap!\n"
                            f"See it. Ape it. Send it. 🚀",
                            parse_mode="HTML",
                        )
                    except Exception as exc:
                        log.warning("milestone post %s", exc)
                    con.execute("UPDATE watches SET last_milestone=? WHERE chat_id=? AND chain=? AND ca=?", (hit, chat_id, chain, ca))
                if mc_now > float(ath_mcap or 0) and float(ath_mcap or 0) > 0:
                    try:
                        await context.bot.send_message(
                            chat_id,
                            f"🚀 <b>NEW ALL-TIME HIGH</b> — {_esc(attrs.get('name') or ca[:8])} MC {_usd(mc_now)}",
                            parse_mode="HTML",
                        )
                    except Exception as exc:
                        log.warning("ath post %s", exc)
                if mc_now > float(ath_mcap or 0):
                    con.execute("UPDATE watches SET ath_mcap=? WHERE chat_id=? AND chain=? AND ca=?", (mc_now, chat_id, chain, ca))
                # Personal price alerts tied to this chat's paired token.
                fired = con.execute(
                    "SELECT id, user_id, target_mcap FROM price_alerts WHERE chat_id=? AND fired=0 AND target_mcap<=?",
                    (chat_id, mc_now),
                ).fetchall()
                for aid, auid, target in fired:
                    try:
                        await context.bot.send_message(
                            auid,
                            f"🔔 {_esc(attrs.get('name') or ca[:8])} hit your target of {_usd(target)} MC — now at {_usd(mc_now)}.",
                            parse_mode="HTML",
                        )
                    except Exception as exc:
                        log.warning("price alert dm %s: %s", auid, exc)
                    con.execute("UPDATE price_alerts SET fired=1 WHERE id=?", (aid,))
        except Exception as exc:
            log.warning("milestone/ath scan %s %s: %s", chat_id, ca, exc)
        # Dev-wallet watch — flag a meaningful balance drop (likely a sell/transfer out).
        if dev_wallet:
            try:
                bal = _dev_balance(chain, ca, dev_wallet)
                prev = float(dev_last_bal) if dev_last_bal is not None else -1
                if bal is not None:
                    if prev >= 0 and bal < prev * 0.95:
                        pct = 100 * (prev - bal) / prev if prev else 0
                        try:
                            await context.bot.send_message(
                                chat_id,
                                f"⚠️ <b>Dev wallet balance dropped {pct:.0f}%</b>\n"
                                f"<code>{_esc(dev_wallet)}</code>\nWas keeping an eye on this — check recent txns.",
                                parse_mode="HTML",
                            )
                        except Exception as exc:
                            log.warning("dev alert %s", exc)
                    con.execute("UPDATE watches SET dev_last_bal=? WHERE chat_id=? AND chain=? AND ca=?", (bal, chat_id, chain, ca))
            except Exception as exc:
                log.warning("dev watch %s %s: %s", chat_id, ca, exc)
        # Daily recap — once per 24h per chat, a shareable "here's what happened" stat card.
        try:
            frow = con.execute("SELECT last_recap FROM chat_flags WHERE chat_id=?", (chat_id,)).fetchone()
            last_recap = int(frow[0] or 0) if frow else 0
            if now - last_recap >= 86400:
                since = now - 86400
                buys = con.execute(
                    "SELECT COUNT(*), COALESCE(SUM(usd),0) FROM buy_log WHERE chat_id=? AND ca=? AND kind='buy' AND ts>=?",
                    (chat_id, ca, since),
                ).fetchone()
                top = con.execute(
                    "SELECT buyer, SUM(usd) s FROM buy_log WHERE chat_id=? AND ca=? AND kind='buy' AND ts>=? AND buyer<>'' "
                    "GROUP BY buyer ORDER BY s DESC LIMIT 1",
                    (chat_id, ca, since),
                ).fetchone()
                n_buys, vol = int(buys[0] or 0), float(buys[1] or 0)
                if n_buys > 0:
                    top_line = f"\n🏆 Top buyer: {top[0][:6]}…{top[0][-4:]} (${top[1]:,.0f})" if top and top[0] else ""
                    try:
                        await context.bot.send_message(
                            chat_id,
                            f"📊 <b>24h recap — {_esc(attrs.get('name') or ca[:8])}</b>\n"
                            f"{n_buys} buys · ${vol:,.0f} volume{top_line}\n"
                            f"See it. Ape it. Send it. 🚀",
                            parse_mode="HTML",
                        )
                    except Exception as exc:
                        log.warning("recap post %s", exc)
                con.execute(
                    "INSERT INTO chat_flags(chat_id, tape, mute_until, last_recap) VALUES(?,1,0,?) "
                    "ON CONFLICT(chat_id) DO UPDATE SET last_recap=excluded.last_recap",
                    (chat_id, now),
                )
        except Exception as exc:
            log.warning("recap scan %s %s: %s", chat_id, ca, exc)
        if newest != last_ts:
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


_PRICE_CACHE = {"ts": 0.0, "sol": 0.0, "eth": 0.0}


def _get_usd_prices() -> tuple[float, float]:
    """Live SOL/ETH USD prices, cached 5 min. Returns (sol_usd, eth_usd) — 0.0 if the feed is down."""
    now = time.time()
    if now - _PRICE_CACHE["ts"] < 300 and (_PRICE_CACHE["sol"] or _PRICE_CACHE["eth"]):
        return _PRICE_CACHE["sol"], _PRICE_CACHE["eth"]
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price?ids=ethereum,solana&vs_currencies=usd",
            timeout=10,
        )
        d = r.json()
        sol = float((d.get("solana") or {}).get("usd") or 0)
        eth = float((d.get("ethereum") or {}).get("usd") or 0)
        if sol or eth:
            _PRICE_CACHE.update(ts=now, sol=sol or _PRICE_CACHE["sol"], eth=eth or _PRICE_CACHE["eth"])
    except Exception as exc:
        log.warning("price feed %s", exc)
    return _PRICE_CACHE["sol"], _PRICE_CACHE["eth"]


def _tier_label(hours: float) -> str:
    return {24: "24h", 72: "3 days", 168: "7 days"}.get(int(hours), f"{hours:.0f}h")


def _tier_lines(kind: str, chain: str) -> list[str]:
    sol_usd, eth_usd = _get_usd_prices()
    is_sol = chain in ("sol", "solana")
    price = sol_usd if is_sol else eth_usd
    sym = "SOL" if is_sol else "ETH"
    lines = []
    for hours, usd in BOOST_TIERS.get(kind, []):
        amt = f"{usd / price:.4f} {sym}" if price else "(price feed down)"
        lines.append(f"{_tier_label(hours)}  ${usd:.0f}  ·  {amt}")
    return lines


def _prorate_hours(kind: str, usd_paid: float) -> float:
    """How much boost time a (possibly underpaid) amount buys, interpolated linearly between tiers."""
    tiers = sorted(BOOST_TIERS.get(kind, []), key=lambda t: t[1])
    if not tiers or usd_paid < BOOST_MIN_USD:
        return 0.0
    if usd_paid <= tiers[0][1]:
        rate = tiers[0][1] / tiers[0][0]  # usd per hour at the cheapest tier
        return round(usd_paid / rate, 2)
    for i in range(len(tiers) - 1):
        lo_hours, lo_usd = tiers[i]
        hi_hours, hi_usd = tiers[i + 1]
        if usd_paid <= hi_usd:
            frac = (usd_paid - lo_usd) / (hi_usd - lo_usd)
            return round(lo_hours + frac * (hi_hours - lo_hours), 2)
    return float(tiers[-1][0])  # paid at/above the top tier — cap at its duration, no bonus


def _prorate_native(tiers: list[tuple[int, float]], min_amt: float, amount_paid: float) -> float:
    """Same interpolation as _prorate_hours but keyed on a native on-chain amount (e.g. SOL)
    instead of USD — used for the ads tiers, which are SOL-denominated rather than USD-pegged."""
    tiers = sorted(tiers, key=lambda t: t[1])
    if not tiers or amount_paid < min_amt:
        return 0.0
    if amount_paid <= tiers[0][1]:
        rate = tiers[0][1] / tiers[0][0]
        return round(amount_paid / rate, 2)
    for i in range(len(tiers) - 1):
        lo_hours, lo_amt = tiers[i]
        hi_hours, hi_amt = tiers[i + 1]
        if amount_paid <= hi_amt:
            frac = (amount_paid - lo_amt) / (hi_amt - lo_amt)
            return round(lo_hours + frac * (hi_hours - lo_hours), 2)
    return float(tiers[-1][0])


def _verify_sol_tx(tx_hash: str) -> tuple[bool, float, str]:
    """Returns (ok, sol_received_by_treasury, error)."""
    if not TREASURY_SOL:
        return False, 0.0, "Treasury SOL wallet not configured."
    try:
        r = requests.post(
            SOL_RPC,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTransaction",
                "params": [tx_hash, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            },
            timeout=15,
        )
        data = r.json()
    except Exception as exc:
        return False, 0.0, f"Solana RPC unreachable: {exc}"
    result = data.get("result")
    if not result:
        return False, 0.0, "Transaction not found — not confirmed yet? wait a bit and retry."
    meta = result.get("meta") or {}
    if meta.get("err"):
        return False, 0.0, "Transaction failed on-chain."
    try:
        keys = result["transaction"]["message"]["accountKeys"]
        addrs = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
        idx = addrs.index(TREASURY_SOL)
    except (KeyError, ValueError, TypeError):
        return False, 0.0, "Treasury wallet isn't a party to that transaction."
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    if idx >= len(pre) or idx >= len(post):
        return False, 0.0, "Could not read balances for that transaction."
    lamports = post[idx] - pre[idx]
    if lamports <= 0:
        return False, 0.0, "No SOL was received by the treasury wallet in that transaction."
    return True, lamports / 1_000_000_000, ""


def _verify_evm_tx(tx_hash: str, chain: str) -> tuple[bool, float, str]:
    """Returns (ok, native-token received by treasury, error)."""
    rpc = EVM_RPC.get(chain)
    if not rpc:
        return False, 0.0, f"No RPC configured for chain {chain}."
    if not TREASURY_EVM:
        return False, 0.0, "Treasury EVM wallet not configured."
    try:
        r = requests.post(
            rpc,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionByHash", "params": [tx_hash]},
            timeout=15,
        )
        tx = (r.json() or {}).get("result")
    except Exception as exc:
        return False, 0.0, f"RPC unreachable: {exc}"
    if not tx:
        return False, 0.0, "Transaction not found — not confirmed yet? wait a bit and retry."
    to_addr = (tx.get("to") or "").lower()
    if to_addr != TREASURY_EVM.lower():
        return False, 0.0, "That transaction did not pay the treasury wallet."
    try:
        r2 = requests.post(
            rpc,
            json={"jsonrpc": "2.0", "id": 2, "method": "eth_getTransactionReceipt", "params": [tx_hash]},
            timeout=15,
        )
        receipt = (r2.json() or {}).get("result") or {}
    except Exception as exc:
        return False, 0.0, f"RPC unreachable: {exc}"
    if receipt.get("status") != "0x1":
        return False, 0.0, "Transaction failed or isn't confirmed yet."
    try:
        wei = int(tx.get("value") or "0x0", 16)
    except ValueError:
        wei = 0
    if wei <= 0:
        return False, 0.0, "No value was transferred to the treasury wallet."
    return True, wei / 1e18, ""


def _grant_boost(
    kind: str, chat_id: int, cashtag: str, ca: str, url: str, user_id: int,
    chain: str, tx_hash: str, usd_paid: float, hours: float,
) -> tuple[int, int]:
    now = int(time.time())
    ends = now + int(hours * 3600)
    con = _db()
    con.execute(
        "INSERT INTO boosts(kind,chat_id,cashtag,ca,url,user_id,chain,tx_hash,usd_paid,hours_granted,"
        "starts_ts,ends_ts,status,created_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
        (kind, chat_id, cashtag, ca, url, user_id, chain, tx_hash, usd_paid, hours, now, ends, now),
    )
    con.commit()
    con.close()
    return now, ends


def _raid_board_url() -> str:
    # A private chat's RAID_CH is a numeric ID (e.g. -1001234567890) which can never resolve
    # as a t.me/<name> link — that's exactly what produced "this user doesn't seem to exist".
    # Prefer an explicit invite link when one's configured; fall back to Eco Hub rather than
    # ever building a link we know is dead.
    if RAID_INVITE:
        return RAID_INVITE
    ch = (RAID_CH or "").lstrip("@")
    if not ch or ch.lstrip("-").isdigit():
        return HUB
    con = _db()
    row = con.execute("SELECT v FROM kv WHERE k='raid_board_msg'").fetchone()
    con.close()
    base = f"https://t.me/{ch}"
    return f"{base}/{row[0]}" if row and row[0] else base


def _trending_board_url() -> str:
    con = _db()
    row = con.execute("SELECT v FROM kv WHERE k='trending_board_msg'").fetchone()
    con.close()
    base = f"https://t.me/{TRENDING_CH.lstrip('@')}" if TRENDING_CH else "https://t.me/Ferzan_Trending"
    return f"{base}/{row[0]}" if row and row[0] else base


async def _post_trending_board(bot) -> str:
    """Live, auto-updating, pinned board of currently-active paid Trending boosts — the Trending
    equivalent of the raid leaderboard board below. Previously /paid just fired an unformatted
    one-off ping into the channel with no ongoing board at all."""
    now = int(time.time())
    con = _db()
    rows = con.execute(
        "SELECT cashtag, ca, url, ends_ts FROM boosts WHERE kind='trending' AND status='active' AND ends_ts>? "
        "ORDER BY usd_paid DESC, ends_ts DESC LIMIT 10",
        (now,),
    ).fetchall()
    mid = con.execute("SELECT v FROM kv WHERE k='trending_board_msg'").fetchone()
    con.close()
    if not rows:
        text = (
            f"{_icon('TITLE', 0, 'F')} <b>FERZAN TRENDING</b>\n\n"
            "No active boosts right now.\n/trending in the bot's DM or your project chat to list here."
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Boost your token", url=HUB)]])
    else:
        lines = [f"{_icon('TITLE', 0, 'F')} <b>FERZAN TRENDING</b>\n"]
        btn_rows = []
        for tag, ca, url, ends_ts in rows:
            name = _esc(tag or ca or "TOKEN")
            left_min = max(0, (ends_ts - now)) // 60
            left = f"{left_min // 60}h {left_min % 60}m" if left_min >= 60 else f"{left_min}m"
            buy = f"https://t.me/{TRADE}?start=buy_{ca}" if ca else HUB
            lines.append(f"🔥  <b>{name}</b> — {left} left")
            if url:
                lines.append(f"📈 <a href=\"{_esc(url)}\">Chart</a>")
            lines.append(f"⚡ <a href=\"{_esc(buy)}\">Buy</a>\n")
            btn_rows.append([InlineKeyboardButton(f"Buy {(tag or ca or 'token')[:16]}", url=buy)])
        lines.append("<i>See it. Ape it. Send it.</i>")
        text = "\n".join(lines)
        kb = InlineKeyboardMarkup(btn_rows[:8])
    if mid:
        try:
            await bot.edit_message_text(
                chat_id=TRENDING_CH, message_id=int(mid[0]), text=text,
                parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True,
            )
            return ""
        except Exception:
            try:
                await bot.edit_message_caption(
                    chat_id=TRENDING_CH, message_id=int(mid[0]), caption=text, parse_mode="HTML", reply_markup=kb
                )
                return ""
            except Exception as exc:
                log.warning("trending board edit %s", exc)
                try:
                    await bot.delete_message(TRENDING_CH, int(mid[0]))
                except Exception:
                    pass
    try:
        msg = await bot.send_message(
            TRENDING_CH, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True
        )
        con = _db()
        con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('trending_board_msg',?)", (str(msg.message_id),))
        con.commit()
        con.close()
        try:
            await bot.pin_chat_message(TRENDING_CH, msg.message_id, disable_notification=True)
        except Exception:
            pass
        return ""
    except Exception as exc:
        log.warning("trending board post %s", exc)
        return str(exc)


def _boost_chain_buttons(kind: str) -> list:
    return [
        [InlineKeyboardButton("BNB Smart Chain", callback_data=f"bst:{kind}:chain:bsc")],
        [InlineKeyboardButton("Ethereum", callback_data=f"bst:{kind}:chain:eth")],
        [InlineKeyboardButton("Base", callback_data=f"bst:{kind}:chain:base")],
        [InlineKeyboardButton("Arbitrum One", callback_data=f"bst:{kind}:chain:arb")],
        [InlineKeyboardButton("Solana", callback_data=f"bst:{kind}:chain:sol")],
        [InlineKeyboardButton("❌ Cancel", callback_data=f"bst:{kind}:x:0")],
    ]


async def marketing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📣 Ferzan promotion — pick what you want to boost:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🚀 Raid Leaderboard boost", callback_data="mk:go:raid")],
                [InlineKeyboardButton("🔥 Trending boost", callback_data="mk:go:trending")],
                [InlineKeyboardButton("📊 Buy-card button ad", callback_data="mk:go:ads")],
                [InlineKeyboardButton("❌ Cancel", callback_data="mk:go:x")],
            ]
        ),
    )


async def marketing_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    parts = (q.data or "").split(":")
    key = parts[-1]
    if key == "x":
        await q.edit_message_text("Cancelled.")
        return
    if key == "ads":
        await _start_ads_flow(q, context, edit=True)
        return
    if key in ("raid", "trending"):
        label = "Raid Leaderboard boost" if key == "raid" else "Trending boost"
        await q.edit_message_text(
            f"🚀 {label} — pick your token's chain:",
            reply_markup=InlineKeyboardMarkup(_boost_chain_buttons(key)),
        )
        return


async def trending_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🔥 Trending boost — featured on the live board in https://t.me/Ferzan_Trending\n\n"
        "Pick your token's chain:",
        reply_markup=InlineKeyboardMarkup(_boost_chain_buttons("trending")),
    )


async def raidboost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "🚀 Raid Leaderboard boost — featured on the live board in https://t.me/Ferzan_Raid\n\n"
        "Pick your token's chain:",
        reply_markup=InlineKeyboardMarkup(_boost_chain_buttons("raid")),
    )


async def boost_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    parts = (q.data or "").split(":")
    if len(parts) < 3:
        return
    _, kind, step = parts[0], parts[1], parts[2]
    if step == "x":
        context.user_data.pop("boost_pending", None)
        await q.edit_message_text("Cancelled.")
        return
    if step == "chain":
        chain = parts[3] if len(parts) > 3 else "sol"
        context.user_data["boost_pending"] = {"kind": kind, "chain": chain}
        label = "Raid Leaderboard boost" if kind == "raid" else "Trending boost"
        rows = [
            [InlineKeyboardButton(line, callback_data=f"bst:{kind}:tier:{hours}")]
            for (hours, _usd), line in zip(BOOST_TIERS.get(kind, []), _tier_lines(kind, chain))
        ]
        rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"bst:{kind}:x:0")])
        await q.edit_message_text(
            f"🚀 {label} — {chain.upper()}\n\nPick a duration:",
            reply_markup=InlineKeyboardMarkup(rows),
        )
        return
    if step == "tier":
        hours = float(parts[3]) if len(parts) > 3 else 24.0
        pending = context.user_data.get("boost_pending") or {"kind": kind, "chain": "sol"}
        if kind == "ads":
            amt = next((a for h, a in ADS_TIERS_SOL if h == hours), None)
            pending.update(hours=hours, native_amt=amt)
            context.user_data["boost_pending"] = pending
            await q.edit_message_text(
                f"✅ {_tier_label(hours)} · {amt} SOL\n\nNow send the destination URL for the button ad."
            )
            return
        usd = next((u for h, u in BOOST_TIERS.get(kind, []) if h == hours), None)
        pending.update(hours=hours, usd=usd)
        context.user_data["boost_pending"] = pending
        await q.edit_message_text(
            f"✅ {_tier_label(hours)} · ${usd:.0f}\n\nNow send the token contract address / mint you want boosted."
        )
        return
    if step == "renew":
        bid = int(parts[3]) if len(parts) > 3 else 0
        con = _db()
        row = con.execute(
            "SELECT kind, cashtag, ca, url, chain, hours_granted FROM boosts WHERE id=?", (bid,)
        ).fetchone()
        con.close()
        if not row:
            await q.edit_message_text("That boost record is gone — start fresh with /trending, /raidboost, or /ads.")
            return
        bkind, tag, ca, url, chain, hours = row
        if bkind == "ads":
            amt = next((a for h, a in ADS_TIERS_SOL if h == hours), ADS_TIERS_SOL[0][1])
            context.user_data["boost_pending"] = {
                "kind": "ads", "chain": "sol", "hours": hours, "native_amt": amt,
                "ca": "", "cashtag": tag, "url": url, "target_set": True,
            }
            await q.edit_message_text(
                f"🔁 Renew button ad — {tag or url}\nDuration: {_tier_label(hours)} · {amt} SOL\n\n"
                f"Pay to:\n{TREASURY_SOL or 'set FEE_WALLET_SOL in .env'}\n\nThen /paid <txhash>.",
            )
            return
        tiers = BOOST_TIERS.get(bkind, [])
        usd = next((u for h, u in tiers if h == hours), None)
        if usd is None and tiers:
            usd = min(tiers, key=lambda t: abs(t[0] - hours))[1]
        context.user_data["boost_pending"] = {
            "kind": bkind, "chain": chain, "hours": hours, "usd": usd,
            "ca": ca, "cashtag": tag, "url": url, "target_set": True,
        }
        sol_usd, eth_usd = _get_usd_prices()
        is_sol = chain in ("sol", "solana")
        price = sol_usd if is_sol else eth_usd
        sym = "SOL" if is_sol else "ETH"
        amt_disp = f"{usd / price:.4f} {sym}" if price else "(price feed down)"
        wallet = TREASURY_SOL if is_sol else TREASURY_EVM
        label = "Raid Leaderboard boost" if bkind == "raid" else "Trending boost"
        await q.edit_message_text(
            f"🔁 Renew {label} — {tag or ca}\nDuration: {_tier_label(hours)} · ${usd:.0f} ≈ {amt_disp}\n\n"
            f"Pay to:\n{wallet or 'set FEE_WALLET in .env'}\n\nThen /paid <txhash> to activate.",
        )
        return


async def _finalize_boost_target(msg, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> None:
    """Second step of a boost purchase — token CA for raid/trending, destination URL for ads."""
    pending = context.user_data.get("boost_pending") or {}
    kind = pending.get("kind", "trending")
    hours = pending.get("hours") or 24.0

    if kind == "ads":
        url = raw_text.strip()
        if not (url.startswith("http://") or url.startswith("https://") or url.startswith("t.me/")):
            await msg.reply_text("That doesn't look like a URL. Send the full destination link (https://...).")
            return
        native_amt = pending.get("native_amt") or ADS_TIERS_SOL[0][1]
        pending.update(ca="", cashtag=url[:40], url=url, target_set=True)
        context.user_data["boost_pending"] = pending
        await msg.reply_text(
            f"📊 Buy-card button ad — {url}\n"
            f"Duration: {_tier_label(hours)} · {native_amt} SOL\n\n"
            f"Pay to:\n<code>{html.escape(TREASURY_SOL or 'set FEE_WALLET_SOL in .env')}</code>\n\n"
            f"Then send <code>/paid &lt;txhash&gt;</code> to activate it. Underpay and you still get boosted "
            f"time — just prorated to what you actually sent.",
            parse_mode="HTML",
        )
        return

    ca = raw_text
    chain = pending.get("chain", "sol")
    usd = pending.get("usd") or (BOOST_TIERS.get(kind, [(24, 0)])[0][1])
    pair = _ds(ca, chain) or {}
    tag = "$" + ((pair.get("baseToken") or {}).get("symbol") or "")
    if tag == "$":
        tag = ""
    pending.update(ca=ca, cashtag=tag, url=pair.get("url") or "", target_set=True)
    context.user_data["boost_pending"] = pending
    sol_usd, eth_usd = _get_usd_prices()
    is_sol = chain in ("sol", "solana")
    price = sol_usd if is_sol else eth_usd
    sym = "SOL" if is_sol else "ETH"
    amt = f"{usd / price:.4f} {sym}" if price else "(price feed down — contact support before paying)"
    wallet = TREASURY_SOL if is_sol else TREASURY_EVM
    label = "Raid Leaderboard boost" if kind == "raid" else "Trending boost"
    await msg.reply_text(
        f"🚀 {label} — {tag or ca}\n"
        f"Duration: {_tier_label(hours)} · ${usd:.0f} ≈ {amt}\n\n"
        f"Pay to:\n<code>{html.escape(wallet or 'set FEE_WALLET in .env')}</code>\n\n"
        f"Then send <code>/paid &lt;txhash&gt;</code> to activate it. Underpay and you still get boosted "
        f"time — just prorated to what you actually sent.",
        parse_mode="HTML",
    )


async def _start_ads_flow(target, context: ContextTypes.DEFAULT_TYPE, edit: bool) -> None:
    context.user_data["boost_pending"] = {"kind": "ads", "chain": "sol"}
    rows = [
        [InlineKeyboardButton(f"{_tier_label(h)}  {a} SOL", callback_data=f"bst:ads:tier:{h}")]
        for h, a in ADS_TIERS_SOL
    ]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="bst:ads:x:0")])
    text = (
        "📊 BUY-CARD BUTTON AD\n\nYour link sits on Ferzan buy alerts for the term you pay.\n\nPick a duration:"
    )
    kb = InlineKeyboardMarkup(rows)
    if edit:
        await target.edit_message_text(text, reply_markup=kb)
    else:
        await target.reply_text(text, reply_markup=kb)


async def ads_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_ads_flow(update.effective_message, context, edit=False)


async def paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Usage: /paid <txhash>")
        return
    tx = args[0].strip()
    pending = context.user_data.get("boost_pending")
    if not pending or not pending.get("target_set") or not pending.get("hours"):
        extra = " ".join(args[1:])
        log.info("PAID legacy/untracked claim uid=%s tx=%s extra=%s", update.effective_user.id, tx, extra)
        await update.effective_message.reply_text(
            "Got it, but there's no pending boost purchase tied to your account.\n"
            "Run /trending, /raidboost, or /ads first to pick a duration and target, then /paid <txhash>.",
            disable_web_page_preview=True,
        )
        return
    kind = pending["kind"]
    chain = pending.get("chain", "sol")
    is_sol = chain in ("sol", "solana") or kind == "ads"
    ok, amount, err = _verify_sol_tx(tx) if is_sol else _verify_evm_tx(tx, chain)
    if not ok:
        await update.effective_message.reply_text(
            f"❌ Couldn't verify that payment: {err}\nDouble-check the hash, and if it's still confirming, retry in a minute."
        )
        return
    con = _db()
    dup = con.execute("SELECT 1 FROM boosts WHERE tx_hash=?", (tx,)).fetchone()
    con.close()
    if dup:
        await update.effective_message.reply_text("That transaction has already been used for a boost.")
        return
    hours_full = pending.get("hours") or 24.0
    sol_usd, eth_usd = _get_usd_prices()
    if kind == "ads":
        target_native = pending.get("native_amt") or ADS_TIERS_SOL[0][1]
        full_hit = amount >= target_native
        hours_granted = hours_full if full_hit else _prorate_native(ADS_TIERS_SOL, ADS_MIN_SOL, amount)
        usd_paid = amount * sol_usd if sol_usd else 0.0
        note = "" if full_hit else f" (prorated — {amount:.4f} SOL of the {target_native} SOL tier)"
        too_small = hours_granted <= 0
        below_min_text = (
            f"Payment received ({amount:.4f} SOL) but that's below the {ADS_MIN_SOL} SOL minimum to activate "
            f"any boost time. Send the difference and /paid again with the new tx."
        )
    else:
        price = sol_usd if is_sol else eth_usd
        usd_paid = amount * price if price else 0.0
        target_usd = pending.get("usd") or 0.0
        full_hit = usd_paid >= target_usd
        hours_granted = hours_full if full_hit else _prorate_hours(kind, usd_paid)
        note = "" if full_hit else f" (prorated — ${usd_paid:.2f} of the ${target_usd:.0f} tier)"
        too_small = hours_granted <= 0
        below_min_text = (
            f"Payment received (${usd_paid:.2f}) but that's below the ${BOOST_MIN_USD:.0f} minimum to activate "
            f"any boost time. Send the difference and /paid again with the new tx."
        )
    if too_small:
        await update.effective_message.reply_text(below_min_text)
        return
    _starts, ends = _grant_boost(
        kind, update.effective_chat.id, pending.get("cashtag") or "", pending.get("ca") or "",
        pending.get("url") or "", update.effective_user.id, chain, tx, usd_paid, hours_granted,
    )
    context.user_data.pop("boost_pending", None)
    until = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ends))
    await update.effective_message.reply_text(f"✅ Boost active — {hours_granted:.1f}h{note}\nExpires: {until}")
    try:
        if kind == "raid":
            tag = pending.get("cashtag") or ""
            if tag:
                con2 = _db()
                con2.execute(
                    "INSERT OR IGNORE INTO raid_tokens(cashtag, chat_id, ca, pts) VALUES(?,?,?,0)",
                    (tag, update.effective_chat.id, pending.get("ca") or ""),
                )
                con2.commit()
                con2.close()
            await _post_board(context.bot)
        elif kind == "trending":
            await _post_trending_board(context.bot)
        # ads has no board — injecting the paid link into live buy cards is a separate feature,
        # not built yet; the purchase is tracked and verified but the link isn't auto-placed.
    except Exception as exc:
        log.warning("board refresh after paid %s", exc)


async def boosts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    now = int(time.time())
    con = _db()
    rows = con.execute(
        "SELECT id, kind, cashtag, ca, chain, usd_paid, hours_granted, ends_ts FROM boosts "
        "WHERE status='active' AND ends_ts>? ORDER BY kind, ends_ts",
        (now,),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No active boosts right now.")
        return
    lines = ["🚀 Active boosts"]
    for bid, kind, tag, ca, chain, usd_paid, hours, ends_ts in rows:
        left_min = max(0, (ends_ts - now)) // 60
        left = f"{left_min // 60}h {left_min % 60}m" if left_min >= 60 else f"{left_min}m"
        lines.append(f"• #{bid} {kind.upper()} — {tag or ca} ({chain}) — {left} left — paid ${usd_paid:.2f}")
    await update.effective_message.reply_text("\n".join(lines)[:3800])


async def refundboost_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in OWNER_IDS:
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /refundboost <id>  (see /boosts for ids)")
        return
    bid = int(context.args[0])
    con = _db()
    row = con.execute("SELECT kind FROM boosts WHERE id=? AND status='active'", (bid,)).fetchone()
    if not row:
        con.close()
        await update.effective_message.reply_text("No active boost with that id.")
        return
    con.execute("UPDATE boosts SET status='refunded' WHERE id=?", (bid,))
    con.commit()
    con.close()
    kind = row[0]
    await update.effective_message.reply_text(f"Voided boost #{bid} ({kind}).")
    try:
        if kind == "raid":
            await _post_board(context.bot)
        elif kind == "trending":
            await _post_trending_board(context.bot)
    except Exception as exc:
        log.warning("board refresh after refund %s", exc)


async def revenue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in OWNER_IDS:
        return
    now = int(time.time())
    week_ago = now - 7 * 86400
    month_ago = now - 30 * 86400
    con = _db()

    def _sum(since: int, kind: str | None = None):
        if kind:
            return con.execute(
                "SELECT COALESCE(SUM(usd_paid),0), COUNT(*) FROM boosts "
                "WHERE created_ts>=? AND kind=? AND status!='refunded'",
                (since, kind),
            ).fetchone()
        return con.execute(
            "SELECT COALESCE(SUM(usd_paid),0), COUNT(*) FROM boosts WHERE created_ts>=? AND status!='refunded'",
            (since,),
        ).fetchone()

    w_total, w_n = _sum(week_ago)
    m_total, m_n = _sum(month_ago)
    lines = [
        "💰 Revenue (excludes refunded)",
        f"7d: ${w_total:.2f} ({w_n} boosts)",
        f"30d: ${m_total:.2f} ({m_n} boosts)",
    ]
    for kind in ("raid", "trending", "ads"):
        kt, kn = _sum(month_ago, kind)
        lines.append(f"  {kind}: ${kt:.2f} ({kn}) — 30d")
    con.close()
    await update.effective_message.reply_text("\n".join(lines))


async def linkwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    if len(args) < 2 or args[0].lower() not in ("sol", "solana", "evm", "eth", "base", "bsc", "arb"):
        await update.effective_message.reply_text(
            "Usage: /linkwallet sol <address>   or   /linkwallet evm <0xaddress>\n"
            "Links a wallet to your Telegram account for raid-points payouts."
        )
        return
    kind = "sol" if args[0].lower() in ("sol", "solana") else "evm"
    addr = args[1].strip()
    pattern = WALLET_RE["sol"] if kind == "sol" else WALLET_RE["evm"]
    if not pattern.match(addr):
        await update.effective_message.reply_text(
            "That doesn't look like a valid "
            + ("Solana" if kind == "sol" else "EVM")
            + " address. Check it and try again."
        )
        return
    u = update.effective_user
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO user_wallets(user_id, chain, address, updated) VALUES(?,?,?,?)",
        (u.id, kind, addr, int(time.time())),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"✅ {kind.upper()} wallet linked: `{addr}`", parse_mode="Markdown")


async def mywallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute(
        "SELECT chain, address FROM user_wallets WHERE user_id=?", (update.effective_user.id,)
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No wallet linked yet. /linkwallet sol <address>")
        return
    lines = [f"{c.upper()}: `{a}`" for c, a in rows]
    await update.effective_message.reply_text("Your linked wallets\n" + "\n".join(lines), parse_mode="Markdown")


async def unlinkwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    kind = "sol" if (args[0].lower() in ("sol", "solana") if args else False) else ("evm" if args and args[0].lower() == "evm" else None)
    con = _db()
    if kind:
        con.execute("DELETE FROM user_wallets WHERE user_id=? AND chain=?", (update.effective_user.id, kind))
    else:
        con.execute("DELETE FROM user_wallets WHERE user_id=?", (update.effective_user.id,))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Wallet(s) unlinked.")


async def blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_chat_admin(update):
        await update.effective_message.reply_text("Admins only.")
        return
    args = context.args or []
    sub = (args[0].lower() if args else "list")
    con = _db()
    if sub == "list":
        rows = con.execute(
            "SELECT user_id, name FROM raid_blacklist WHERE chat_id=?", (update.effective_chat.id,)
        ).fetchall()
        con.close()
        if not rows:
            await update.effective_message.reply_text("No blacklisted users in this chat.")
            return
        lines = [f"• {n or uid} (`{uid}`)" for uid, n in rows]
        await update.effective_message.reply_text("🚫 Blacklisted from raid points\n" + "\n".join(lines), parse_mode="Markdown")
        return
    target_id = None
    target_name = ""
    reply = update.effective_message.reply_to_message
    if reply and reply.from_user:
        target_id = reply.from_user.id
        target_name = reply.from_user.full_name or reply.from_user.username or str(target_id)
    elif len(args) >= 2 and args[1].isdigit():
        target_id = int(args[1])
        target_name = args[1]
    if not target_id:
        await update.effective_message.reply_text(
            "Usage: /blacklist add <user_id>  (or reply to their message)\n"
            "/blacklist remove <user_id>\n/blacklist list"
        )
        con.close()
        return
    if sub == "add":
        con.execute(
            "INSERT OR REPLACE INTO raid_blacklist(chat_id, user_id, name, added_by, added_ts) VALUES(?,?,?,?,?)",
            (update.effective_chat.id, target_id, target_name, update.effective_user.id, int(time.time())),
        )
        con.commit()
        con.close()
        await update.effective_message.reply_text(f"🚫 {target_name} blacklisted from raid points here.")
    elif sub == "remove":
        con.execute(
            "DELETE FROM raid_blacklist WHERE chat_id=? AND user_id=?", (update.effective_chat.id, target_id)
        )
        con.commit()
        con.close()
        await update.effective_message.reply_text(f"✅ {target_name} removed from blacklist.")
    else:
        con.close()
        await update.effective_message.reply_text("Usage: /blacklist add|remove|list")


async def exportpoints_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_chat_admin(update):
        await update.effective_message.reply_text("Admins only.")
        return
    con = _db()
    rows = con.execute(
        "SELECT user_id, name, pts FROM raid_scores WHERE chat_id=? ORDER BY pts DESC",
        (update.effective_chat.id,),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No raid points logged in this chat yet.")
        return
    import csv
    from io import BytesIO, StringIO

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "name", "points", "sol_wallet", "evm_wallet"])
    con2 = _db()
    for uid, name, pts in rows:
        wr = con2.execute("SELECT chain, address FROM user_wallets WHERE user_id=?", (uid,)).fetchall()
        wmap = {c: a for c, a in wr}
        w.writerow([uid, name, pts, wmap.get("sol", ""), wmap.get("evm", "")])
    con2.close()
    bio = BytesIO(buf.getvalue().encode("utf-8"))
    bio.name = f"raid_points_{update.effective_chat.id}.csv"
    await update.effective_message.reply_document(bio, caption=f"📊 {len(rows)} raiders exported.")


async def addtag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_chat_admin(update):
        await update.effective_message.reply_text("Admins only.")
        return
    args = context.args or []
    if len(args) < 5 or not all(a.isdigit() for a in args[1:5]):
        await update.effective_message.reply_text(
            "Usage: /addtag <name> <likes> <reposts> <replies> <minutes>\n"
            "Example: /addtag quick 5 5 2 30"
        )
        return
    name = args[0].lower()
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO raid_presets(chat_id, name, likes_t, rt_t, re_t, mins) VALUES(?,?,?,?,?,?)",
        (update.effective_chat.id, name, int(args[1]), int(args[2]), int(args[3]), int(args[4])),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(
        f"✅ Preset '{name}' saved — {args[1]} likes / {args[2]} reposts / {args[3]} replies / {args[4]}m\n"
        f"Use it with: /raid <link> {name}"
    )


async def tags_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute(
        "SELECT name, likes_t, rt_t, re_t, mins FROM raid_presets WHERE chat_id=?",
        (update.effective_chat.id,),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No presets saved yet. /addtag <name> <likes> <rt> <re> <mins>")
        return
    lines = [f"• {n} — {lt}👍 {rt}🔁 {re}💬 · {m}m" for n, lt, rt, re, m in rows]
    await update.effective_message.reply_text("🏷 Raid presets\n" + "\n".join(lines))


async def season_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_chat_admin(update):
        await update.effective_message.reply_text("Admins only.")
        return
    con = _db()
    rows = con.execute(
        "SELECT name, pts FROM raid_scores WHERE chat_id=? ORDER BY pts DESC LIMIT 25",
        (update.effective_chat.id,),
    ).fetchall()
    if not rows:
        con.close()
        await update.effective_message.reply_text("No points to close out yet.")
        return
    import json

    snapshot = json.dumps([{"name": n, "pts": p} for n, p in rows])
    con.execute(
        "INSERT INTO raid_seasons(chat_id, ended_ts, snapshot) VALUES(?,?,?)",
        (update.effective_chat.id, int(time.time()), snapshot),
    )
    con.execute("DELETE FROM raid_scores WHERE chat_id=?", (update.effective_chat.id,))
    con.commit()
    con.close()
    top = rows[0]
    lines = [f"{i+1}. {n}  {p}" for i, (n, p) in enumerate(rows[:5])]
    await update.effective_message.reply_text(
        f"🏁 Season closed — 🏆 {top[0]} takes it with {top[1]} pts!\n\n" + "\n".join(lines) +
        "\n\nLeaderboard reset to 0. /lastseason to look this back up."
    )


async def lastseason_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    row = con.execute(
        "SELECT ended_ts, snapshot FROM raid_seasons WHERE chat_id=? ORDER BY id DESC LIMIT 1",
        (update.effective_chat.id,),
    ).fetchone()
    con.close()
    if not row:
        await update.effective_message.reply_text("No past seasons recorded yet. /season to close one out.")
        return
    import json

    ended, snap = row
    data = json.loads(snap or "[]")
    when = time.strftime("%Y-%m-%d", time.localtime(ended))
    lines = [f"{i+1}. {d['name']}  {d['pts']}" for i, d in enumerate(data[:10])]
    await update.effective_message.reply_text(f"📜 Last season (closed {when})\n" + "\n".join(lines))


async def payoutpreview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_chat_admin(update):
        await update.effective_message.reply_text("Admins only.")
        return
    args = context.args or []
    try:
        rate = float(args[0]) if args else 0.0
        assert rate > 0
    except Exception:
        await update.effective_message.reply_text("Usage: /payoutpreview <$ per point>\nExample: /payoutpreview 0.10")
        return
    con = _db()
    rows = con.execute(
        "SELECT name, pts FROM raid_scores WHERE chat_id=? ORDER BY pts DESC LIMIT 25",
        (update.effective_chat.id,),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No raid points logged in this chat yet.")
        return
    total = sum(p for _, p in rows)
    lines = [f"{n}  {p}pt → ${p * rate:,.2f}" for n, p in rows]
    await update.effective_message.reply_text(
        f"💸 Payout preview @ ${rate:.2f}/pt\nTotal owed: ${total * rate:,.2f}\n\n" + "\n".join(lines[:20])
    )


async def exportpayout_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_chat_admin(update):
        await update.effective_message.reply_text("Admins only.")
        return
    args = context.args or []
    try:
        rate = float(args[0]) if args else 0.0
        assert rate > 0
    except Exception:
        await update.effective_message.reply_text("Usage: /exportpayout <$ per point>\nExample: /exportpayout 0.10")
        return
    con = _db()
    rows = con.execute(
        "SELECT user_id, name, pts FROM raid_scores WHERE chat_id=? ORDER BY pts DESC",
        (update.effective_chat.id,),
    ).fetchall()
    if not rows:
        con.close()
        await update.effective_message.reply_text("No raid points logged in this chat yet.")
        return
    import csv
    from io import BytesIO, StringIO

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "name", "points", "rate_usd", "owed_usd", "sol_wallet", "evm_wallet"])
    for uid, name, pts in rows:
        wr = con.execute("SELECT chain, address FROM user_wallets WHERE user_id=?", (uid,)).fetchall()
        wmap = {c: a for c, a in wr}
        w.writerow([uid, name, pts, f"{rate:.4f}", f"{pts * rate:.2f}", wmap.get("sol", ""), wmap.get("evm", "")])
    con.close()
    bio = BytesIO(buf.getvalue().encode("utf-8"))
    bio.name = f"raid_payout_{update.effective_chat.id}.csv"
    total = sum(p for _, _, p in rows) * rate
    await update.effective_message.reply_document(bio, caption=f"💸 Payout sheet @ ${rate:.2f}/pt — total ${total:,.2f}")


async def deltag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_chat_admin(update):
        await update.effective_message.reply_text("Admins only.")
        return
    args = context.args or []
    if not args:
        await update.effective_message.reply_text("Usage: /deltag <name>")
        return
    con = _db()
    con.execute("DELETE FROM raid_presets WHERE chat_id=? AND name=?", (update.effective_chat.id, args[0].lower()))
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Preset '{args[0].lower()}' removed.")


async def linkgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_chat_admin(update):
        await update.effective_message.reply_text("Admins only.")
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text(
            "Usage: /linkgroup <chat_id>\n"
            "Run this in the HOME group for a project. <chat_id> is the sister group's ID "
            "(the bot must already be in it — forward a message from there to @userinfobot to get its ID, "
            "or check /linkedgroups after adding the bot).\n"
            "Once linked, /raid in the home group also posts the same raid card into that group, "
            "and taps there count toward the same leaderboard."
        )
        return
    linked_id = int(args[0])
    title = ""
    try:
        chat = await context.bot.get_chat(linked_id)
        title = chat.title or ""
    except Exception:
        pass
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO raid_links(chat_id, linked_chat_id, linked_title) VALUES(?,?,?)",
        (update.effective_chat.id, linked_id, title),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"✅ Linked {title or linked_id} — future /raid posts will also go there.")


async def unlinkgroup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_chat_admin(update):
        await update.effective_message.reply_text("Admins only.")
        return
    args = context.args or []
    if not args or not args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text("Usage: /unlinkgroup <chat_id>")
        return
    con = _db()
    con.execute(
        "DELETE FROM raid_links WHERE chat_id=? AND linked_chat_id=?",
        (update.effective_chat.id, int(args[0])),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text("Unlinked.")


async def linkedgroups_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute(
        "SELECT linked_chat_id, linked_title FROM raid_links WHERE chat_id=?", (update.effective_chat.id,)
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text(
            f"No sister groups linked here.\nThis chat's ID: `{update.effective_chat.id}`\n"
            "/linkgroup <chat_id> in another group to link it to this one.",
            parse_mode="Markdown",
        )
        return
    lines = [f"• {t or cid} (`{cid}`)" for cid, t in rows]
    await update.effective_message.reply_text(
        f"🔗 Linked sister groups\nThis chat's ID: `{update.effective_chat.id}`\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


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
                BotCommand("marketing", "Promotion options"),
                BotCommand("trending", "Buy a Trending boost"),
                BotCommand("raid", "Launch an X raid"),
                BotCommand("raidstop", "Stop the active raid"),
                BotCommand("raidjoin", "Log a raid point"),
                BotCommand("raidlb", "This chat's raid leaderboard"),
                BotCommand("raidboost", "Buy a Raid Leaderboard boost"),
                BotCommand("raidpin", "Toggle auto-pin for raid posts"),
                BotCommand("addtag", "Save a reusable raid preset"),
                BotCommand("tags", "List raid presets"),
                BotCommand("linkgroup", "Link a sister group to this raid"),
                BotCommand("linkedgroups", "List linked raid groups"),
                BotCommand("blacklist", "Manage the raid-points blacklist"),
                BotCommand("exportpoints", "Export raid leaderboard as CSV"),
                BotCommand("linkwallet", "Link your payout wallet"),
                BotCommand("mywallet", "Show your linked wallet"),
                BotCommand("season", "Close out the raid leaderboard"),
                BotCommand("lastseason", "See last season's winners"),
                BotCommand("payoutpreview", "Preview $ owed per raider"),
                BotCommand("exportpayout", "Export a $ payout CSV"),
                BotCommand("setwhale", "Set the whale-buy $ threshold"),
                BotCommand("sellalerts", "Toggle sell alerts"),
                BotCommand("setdev", "Watch a dev wallet for sells"),
                BotCommand("alert", "DM me at a market cap target"),
                BotCommand("topbuyers", "Top buyers leaderboard"),
                BotCommand("paid", "Activate a boost with your txhash"),
                BotCommand("boosts", "See active boosts"),
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
    app.add_handler(CommandHandler("raidboost", raidboost_cmd))
    app.add_handler(CallbackQueryHandler(boost_cb, pattern=r"^bst:"))
    app.add_handler(CommandHandler("ads", ads_cmd))
    app.add_handler(CommandHandler("paid", paid_cmd))
    app.add_handler(CommandHandler("boosts", boosts_cmd))
    app.add_handler(CommandHandler("refundboost", refundboost_cmd))
    app.add_handler(CommandHandler("revenue", revenue_cmd))
    app.add_handler(CommandHandler("market", market_cmd))
    app.add_handler(CommandHandler("vote", vote_cmd))
    app.add_handler(CommandHandler("raid", raid_cmd))
    app.add_handler(CommandHandler("raidstop", raidstop_cmd))
    app.add_handler(CommandHandler("raidint", raidint_cmd))
    app.add_handler(CommandHandler("raidgoal", raidgoal_cmd))
    app.add_handler(CallbackQueryHandler(raid_cb, pattern=r"^rd:"))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("nextlist", list_raids))
    app.add_handler(CommandHandler("queuelist", list_raids))
    app.add_handler(CommandHandler("raidjoin", raidjoin_cmd))
    app.add_handler(CommandHandler("lb", lb_cmd))
    app.add_handler(CommandHandler("raidlb", lb_cmd))
    app.add_handler(CommandHandler("clb", lb_cmd))
    app.add_handler(CommandHandler("raidevent", raidevent_cmd))
    app.add_handler(CommandHandler("relb", lb_cmd))
    app.add_handler(CommandHandler("linkwallet", linkwallet_cmd))
    app.add_handler(CommandHandler("mywallet", mywallet_cmd))
    app.add_handler(CommandHandler("unlinkwallet", unlinkwallet_cmd))
    app.add_handler(CommandHandler("blacklist", blacklist_cmd))
    app.add_handler(CommandHandler("exportpoints", exportpoints_cmd))
    app.add_handler(CommandHandler("addtag", addtag_cmd))
    app.add_handler(CommandHandler("tags", tags_cmd))
    app.add_handler(CommandHandler("deltag", deltag_cmd))
    app.add_handler(CommandHandler("linkgroup", linkgroup_cmd))
    app.add_handler(CommandHandler("unlinkgroup", unlinkgroup_cmd))
    app.add_handler(CommandHandler("linkedgroups", linkedgroups_cmd))
    app.add_handler(CommandHandler("raidpin", raidpin_cmd))
    app.add_handler(CommandHandler("season", season_cmd))
    app.add_handler(CommandHandler("lastseason", lastseason_cmd))
    app.add_handler(CommandHandler("payoutpreview", payoutpreview_cmd))
    app.add_handler(CommandHandler("exportpayout", exportpayout_cmd))
    app.add_handler(CommandHandler("setwhale", setwhale_cmd))
    app.add_handler(CommandHandler("sellalerts", sellalerts_cmd))
    app.add_handler(CommandHandler("setdev", setdev_cmd))
    app.add_handler(CommandHandler("alert", alert_cmd))
    app.add_handler(CommandHandler("topbuyers", topbuyers_cmd))
    app.job_queue.run_repeating(tick, interval=25, first=8)
    log.info("Ferzan Buy running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
