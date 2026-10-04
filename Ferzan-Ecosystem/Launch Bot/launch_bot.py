"""
launch_bot.py

The Telegram-facing half of the launch bot. Walks the user through:
  chain -> launch type -> name -> ticker -> supply -> (curve / team options)
-> creates a launch_request in the shared DB -> opens the Mini App with
that request's ID, where the actual wallet connection and signing happen.

Every step has tap-to-pick buttons; typing a custom value still works.
Only chains/modes that can really launch right now are offered.

This process and api.py should run as two separate services -- they share
state only through launch_bot_db.py's SQLite file, so either can be
restarted independently.
"""

import asyncio
import html
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

load_dotenv("/opt/ferzan/.env")
load_dotenv(Path(__file__).resolve().with_name(".env"))
load_dotenv()
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ConversationHandler, filters, ContextTypes,
)

import launch_bot_db as db
import ferzan_perks as fp
import launch_extras as lx
import ton_curve as tcv
import ton_launch as tl
import tron_launch as tron
import tron_liquidity

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

MINI_APP_BASE_URL = os.environ.get("MINI_APP_BASE_URL", "https://yourdomain.com/miniapp")
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
NATIVE = {"ethereum": "ETH", "bsc": "BNB", "base": "ETH", "robinhood": "ETH", "arc": "USDC", "solana": "SOL", "ton": "TON", "tron": "TRX"}
# env-var prefix used by api.py for factory addresses
FACTORY_KEY = {"ethereum": "ETH", "bsc": "BSC", "base": "BASE", "robinhood": "HOOD", "arc": "ARC"}
# the plain factories' fixed launch fee (set in the contract at deploy time)
PLAIN_FEE_TEXT = {"bsc": "Free", "base": "Free", "ethereum": "Free", "robinhood": "Free", "arc": "Free"}

# quick-pick presets
SUPPLY_PRESETS = [("1M", 10**6), ("100M", 10**8), ("1B", 10**9), ("10B", 10**10), ("100B", 10**11), ("1T", 10**12)]
DEVBUY_PRESETS = {
    "solana": ["0.1", "0.5", "1", "2"],
    "bsc": ["0.01", "0.05", "0.1", "0.5"],
    "arc": ["10", "50", "100", "500"],
    "tron": ["50", "100", "500", "1000"],
    "default": ["0.001", "0.005", "0.01", "0.05"],
}
GRAD_PRESETS = {"bsc": ["5", "10", "20"], "arc": ["5000", "10000", "25000"], "tron": ["10000", "25000", "50000"],
                "default": ["1", "2.5", "5"]}
MAXBUY_PRESETS = {"bsc": ["0.1", "0.5", "1"], "arc": ["100", "500", "1000"], "tron": ["500", "1000", "5000"],
                  "default": ["0.01", "0.05", "0.1"]}

(CHOOSING_CHAIN, CHOOSING_MODE, ENTERING_NAME, ENTERING_SYMBOL, ENTERING_SUPPLY, ENTERING_GRAD,
 ENTERING_VETH, ENTERING_VTOKEN, ENTERING_ALLOCS, ENTERING_DEVBUY, ENTERING_WINDOW, CONFIRMING,
 ENTERING_MAXBUY, ENTERING_LOGO, ENTERING_INFO, CHOOSING_TZ, ENTERING_OPEN_AT, ENTERING_LATER) = range(18)
MAX_OPEN_DELAY = 7 * 86400 - 600      # the curve contract allows up to 7 days
MAX_DRAFT_AHEAD = 30 * 86400
MAX_LOGO_BYTES = 5 * 1024 * 1024
CURVE_MODES = {"bonding_curve", "meteora"}

SOL_U64_MAX = 2**64 - 1


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _fmt_int(n: int) -> str:
    return f"{int(n):,}"


def _short_num(n: int) -> str:
    for div, suf in ((10**12, "T"), (10**9, "B"), (10**6, "M"), (10**3, "K")):
        if n >= div and n % div == 0:
            return f"{n // div}{suf}"
    return _fmt_int(n)


def _plain_live(chain: str) -> bool:
    if chain == "solana":
        return True
    if chain == "ton":
        return (os.environ.get("TON_LAUNCH_LIVE") or "").strip() == "1"
    if chain == "tron":
        return tron.live()
    key = FACTORY_KEY.get(chain)
    return bool(key and (os.environ.get(f"FACTORY_{key}_PLAIN") or "").strip())


def ton_curve_live() -> bool:
    """Same three conditions api.py uses (_ton_curve_live), read like tron_launch._setting()
    (service env, /opt/ferzan/.env, Launch Bot/.env)."""
    s = tron._setting
    return s("TON_CURVE_LIVE") == "1" and bool(s("TON_CURVE_MASTER")) and bool(s("TON_KEEPER_ADDRESS"))


def ton_curve_min_grad() -> float:
    """Smallest TON graduation target (TON_CURVE_MIN_GRAD_TON, default 2000)."""
    try:
        v = float(tron._setting("TON_CURVE_MIN_GRAD_TON") or 2000)
    except ValueError:
        return 2000.0
    return v if v > 0 else 2000.0


TON_CURVE_MAX_SUPPLY = 10**9          # whole coins; the API caps TON curve supply here
TON_CURVE_MAX_GRAD = 10_000_000       # TON; the API caps graduation here
TON_CURVE_SUPPLY_PRESETS = [("1M", 10**6), ("10M", 10**7), ("100M", 10**8), ("1B", 10**9)]


def ton_grad_presets() -> list[str]:
    """TON graduation quick-picks, never below the minimum (the smallest allowed target is always offered)."""
    lo = ton_curve_min_grad()
    out = [f"{lo:g}"] + [f"{v}" for v in (2000, 5000, 10000) if v > lo]
    return out[:3]


def _curve_live(chain: str) -> bool:
    if chain == "tron":
        return tron.curve_live()
    if chain == "ton":
        return ton_curve_live()
    if chain == "solana":
        return bool((os.environ.get("METEORA_CONFIG") or "").strip())
    key = FACTORY_KEY.get(chain)
    return bool(key and (os.environ.get(f"FACTORY_{key}_CURVE") or "").strip())


def _live_chains() -> list[str]:
    return [c for c in CHAINS if _plain_live(c) or _curve_live(c)]


def _steps(launch: dict) -> list[str]:
    """The step names for this launch, so 'Step x/y' is always right."""
    mode, chain = launch.get("mode"), launch.get("chain")
    if mode == "meteora":
        return ["type", "name", "symbol", "logo", "info", "devbuy"]
    s = ["type", "name", "symbol", "logo", "info", "supply"]
    if mode == "bonding_curve" and chain == "ton":
        s += ["grad"]  # the TON curve contract has no dev buy, opening window or max buy
    elif mode == "bonding_curve":
        s += ["grad", "devbuy", "window", "maxbuy"] if chain == "tron" else ["grad", "allocs", "devbuy", "window", "maxbuy"]
    elif chain in EVM_CHAINS:
        s += ["allocs"]
    return s


def _hdr(launch: dict, step: str, title: str) -> str:
    st = _steps(launch)
    n = st.index(step) + 1 if step in st else len(st)
    return f"<b>Step {n}/{len(st)} — {title}</b>\n\n"


def _cancel_row() -> list:
    return [InlineKeyboardButton("✖️ Cancel launch", callback_data="lx:cancel")]


def _kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows + [_cancel_row()])


async def _send(update: Update, text: str, rows: list | None = None):
    """Reply to a typed message or a button tap the same way (HTML, safe)."""
    await update.effective_message.reply_text(
        text, parse_mode="HTML", reply_markup=_kb(rows or []), disable_web_page_preview=True
    )


async def _tap(update: Update) -> None:
    q = update.callback_query
    if q:
        await q.answer()
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


def _parse_amount(text: str) -> float | None:
    raw = (text or "").strip().lower().replace(",", "")
    if raw in {"skip", "none", "no", "0", "0.0"}:
        return 0.0
    if not re.fullmatch(r"\d*\.?\d+", raw):
        return None
    return float(raw)


def _parse_supply(text: str) -> int | None:
    raw = (text or "").strip().lower().replace(",", "").replace("_", "").replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kmbt]?)", raw)
    if not m:
        return None
    mult = {"": 1, "k": 10**3, "m": 10**6, "b": 10**9, "t": 10**12}[m.group(2)]
    whole, _, frac = m.group(1).partition(".")
    val = int(whole) * mult + (int(frac) * mult // (10 ** len(frac)) if frac else 0)
    return val if val > 0 else None


def _dur(seconds: int) -> str:
    seconds = int(seconds)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    parts = ([f"{d}d"] if d else []) + ([f"{h}h"] if h else []) + ([f"{m}m"] if m and not d else [])
    return " ".join(parts) or "under a minute"


def _tz_rows(prefix: str) -> list:
    rows, row = [], []
    for label, name in lx.TZ_CHOICES:
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}:{name}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _auto_symbol(name: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", name)
    if len(words) >= 2:
        sym = "".join(w[0] for w in words)
    else:
        sym = (words[0] if words else "TOKEN")[:5]
    return sym.upper()[:10]


# ------------------------------------------------------------------ home --
# ---- creator follows: ferzan-factory.com/creator/<wallet> "Follow" opens /start follow_<wallet> ----
_FOLLOW_ADDR = re.compile(r"0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,48}|[EUk]Q[A-Za-z0-9_-]{46}")


def _follow_conn():
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS creator_follows (user_id INTEGER NOT NULL, wallet TEXT NOT NULL, created_at INTEGER, "
                 "PRIMARY KEY (user_id, wallet))")
    return conn


def _norm_wallet(w: str) -> str:
    w = (w or "").strip()
    return w.lower() if w.lower().startswith("0x") else w


def _short(w: str) -> str:
    return f"{w[:4]}…{w[-4:]}" if len(w) > 12 else w


# ------------------------------------------------------------ holder tiers --
PERKS_START_TEXT = "Thu Oct 15 2026, 4PM ET"      # FERZAN launch: perks switch on then (the mint stays hidden until announced)
_SOL_ADDR = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def _perk_conn():
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS perk_wallets (user_id INTEGER PRIMARY KEY, wallet TEXT NOT NULL, updated_at INTEGER)")
    return conn


def _get_perk_wallet(uid: int) -> str:
    try:
        conn = _perk_conn()
        try:
            row = conn.execute("SELECT wallet FROM perk_wallets WHERE user_id = ?", (int(uid),)).fetchone()
        finally:
            conn.close()
        return row[0] if row else ""
    except Exception:
        return ""


def _set_perk_wallet(uid: int, wallet: str) -> None:
    conn = _perk_conn()
    try:
        if wallet:
            conn.execute("INSERT INTO perk_wallets (user_id, wallet, updated_at) VALUES (?, ?, ?) "
                         "ON CONFLICT(user_id) DO UPDATE SET wallet = excluded.wallet, updated_at = excluded.updated_at",
                         (int(uid), wallet, int(time.time())))
        else:
            conn.execute("DELETE FROM perk_wallets WHERE user_id = ?", (int(uid),))
        conn.commit()
    finally:
        conn.close()


def _pct(bps: int) -> str:
    return "0%" if not bps else f"{bps / 100:.2f}%"


def _tiers_text(own: dict | None = None, wallet: str = "") -> str:
    """The public tier ladder (from ferzan_perks) plus, when given, this user's own tier. Never shows the mint."""
    base = fp.base_bridge_bps()
    _launch_free = (os.environ.get("LAUNCH_FEE_LAMPORTS") or "50000000").strip() == "0"
    lines = ["<b>🏅 FERZAN holder tiers</b>", "",
             f"Hold FERZAN in a Solana wallet and get perks across Ferzan. They start at the FERZAN launch: "
             f"<b>{PERKS_START_TEXT}</b>.", ""]
    for t in fp.ladder():
        off = int(t["launch_fee_off_pct"])
        launch = "free" if off >= 100 else "half price" if off == 50 else f"{off}% off"
        lines.append(f"{_esc(t['badge'])} — {_fmt_int(int(t['min']))}+ FERZAN\n"
                     f"   {'' if _launch_free else f'Launch fee: {launch} · '}Bridge fee: {_pct(int(t['bridge_fee_bps']))} (else {_pct(base)}) · "
                     f"Trade Bot fees: {int(t['trade_fee_discount_pct'])}% off (soon)")
    lines += ["", "<i>Launch-fee perks apply to Solana launches, for the Solana wallet you launch from.</i>"]
    if wallet:
        lines.append("")
        if own is None or not own.get("active"):
            lines.append(f"Your wallet <code>{_esc(_short(wallet))}</code>: perks aren't live yet, so no tier is counted. "
                         "It's checked automatically once FERZAN launches.")
        elif own.get("error"):
            lines.append(f"Your wallet <code>{_esc(_short(wallet))}</code>: couldn't read the balance right now, try again in a minute.")
        elif own.get("tier") and own["tier"] != "none":
            lines.append(f"Your wallet <code>{_esc(_short(wallet))}</code>: {_esc(own.get('badge'))} "
                         f"({own.get('balance', 0):,.0f} FERZAN)")
            if own.get("next_tier"):
                lines.append(f"Next: {_esc(own['next_tier'])} at {_fmt_int(int(own['next_min']))} FERZAN.")
        else:
            lines.append(f"Your wallet <code>{_esc(_short(wallet))}</code>: no tier yet ({own.get('balance', 0):,.0f} FERZAN). "
                         f"Holder starts at {_fmt_int(int(own.get('holder_min') or 0))}.")
        lines.append("Change it with /tiers &lt;solana wallet&gt; · remove with /tiers clear")
    else:
        lines += ["", "See your own tier: <code>/tiers YourSolanaWalletAddress</code> (public address only, never a key)."]
    return "\n".join(lines)


async def _own_perks(wallet: str) -> dict | None:
    if not wallet:
        return None
    try:
        return await asyncio.to_thread(fp.perks, wallet)
    except Exception:
        return None


async def _solana_fee_row(uid: int) -> tuple[str, str]:
    """(fee text for the confirm screen, extra note). Discount = ferzan_perks on the saved Solana wallet."""
    try:
        base = int(os.environ.get("LAUNCH_FEE_LAMPORTS") or "50000000")
    except ValueError:
        base = 50_000_000
    if base == 0:  # launches are free for everyone: only Solana's own rent and network fees remain
        return ("Free + network cost (about 0.03 SOL of Solana rent and fees)", "")
    wallet = _get_perk_wallet(uid)
    if wallet:
        try:
            fee, note = await asyncio.to_thread(fp.launch_fee_lamports, wallet, base)
        except Exception:
            fee, note = base, ""
        if note:
            return (f"{fee / 1e9:g} SOL (was {base / 1e9:g}) + network cost",
                    f"{_esc(note)} for your saved wallet <code>{_esc(_short(wallet))}</code>. The discount applies to the "
                    "wallet you connect, if it holds FERZAN.")
    return (f"{base / 1e9:g} SOL + network cost",
            "FERZAN holders get half off or a free Solana launch: see /tiers.")


async def tiers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    arg = " ".join(context.args or []).strip()
    if arg.lower() in {"clear", "remove", "off"}:
        await asyncio.to_thread(_set_perk_wallet, uid, "")
        await update.effective_message.reply_text("Saved wallet removed.")
        return
    if arg:
        if not _SOL_ADDR.fullmatch(arg):
            await update.effective_message.reply_text("That doesn't look like a Solana address. Usage: /tiers YourSolanaWalletAddress")
            return
        await asyncio.to_thread(_set_perk_wallet, uid, arg)
    wallet = arg or await asyncio.to_thread(_get_perk_wallet, uid)
    own = await _own_perks(wallet)
    await update.effective_message.reply_text(_tiers_text(own, wallet), parse_mode="HTML", disable_web_page_preview=True)


async def go_tiers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    wallet = await asyncio.to_thread(_get_perk_wallet, update.effective_user.id)
    own = await _own_perks(wallet)
    await q.message.reply_text(_tiers_text(own, wallet), parse_mode="HTML", disable_web_page_preview=True)


async def _follow_start(update: Update, wallet: str) -> None:
    uid = update.effective_user.id
    if not _FOLLOW_ADDR.fullmatch(wallet or ""):
        await update.effective_message.reply_text("That creator link looks wrong. Open it again from the creator's page on ferzan-factory.com.")
        return
    conn = _follow_conn()
    n = conn.execute("SELECT COUNT(*) FROM creator_follows WHERE user_id = ?", (uid,)).fetchone()[0]
    if n >= 200:
        conn.close()
        await update.effective_message.reply_text("You follow 200 creators already. Remove some with /following first.")
        return
    conn.execute("INSERT OR IGNORE INTO creator_follows (user_id, wallet, created_at) VALUES (?, ?, ?)", (uid, _norm_wallet(wallet), int(time.time())))
    conn.commit()
    conn.close()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Their launches", url=f"https://ferzan-factory.com/creator/{wallet}")],
        [InlineKeyboardButton("📋 Everyone I follow", callback_data="fol:list")],
    ])
    await update.effective_message.reply_text(
        f"✅ Following creator {_short(wallet)}.\n\nYou'll get a message here the moment they launch a new coin on Ferzan. "
        "Manage or stop alerts any time with /following.", reply_markup=kb)


def _following_view(uid: int):
    conn = _follow_conn()
    rows = [r[0] for r in conn.execute("SELECT wallet FROM creator_follows WHERE user_id = ? ORDER BY created_at DESC LIMIT 40", (uid,))]
    conn.close()
    if not rows:
        return ("You don't follow any creators yet. Open a creator's page on ferzan-factory.com and tap Follow.", None)
    buttons = [[InlineKeyboardButton(f"❌ Unfollow {_short(w)}", callback_data=f"fol:x:{w}"[:64])] for w in rows]
    return (f"You follow {len(rows)} creator{'s' if len(rows) != 1 else ''}. You get a DM when any of them launches.", InlineKeyboardMarkup(buttons))


async def following_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text, kb = _following_view(update.effective_user.id)
    await update.effective_message.reply_text(text, reply_markup=kb)


async def following_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data or ""
    if data.startswith("fol:x:"):
        conn = _follow_conn()
        conn.execute("DELETE FROM creator_follows WHERE user_id = ? AND wallet = ?", (uid, data[6:]))
        conn.commit()
        conn.close()
    text, kb = _following_view(uid)
    try:
        await q.edit_message_text(text, reply_markup=kb)
    except Exception:
        await q.message.reply_text(text, reply_markup=kb)


# ---- coin alerts: ferzan-factory.com coin page "Alerts" opens /start watch_<chain>_<token> ----
_WATCH_KINDS = {
    "p50": "reaches 50% of its curve", "p90": "reaches 90% of its curve", "grad": "graduates",
    "up50": "is up 50%", "x2": "does a 2x", "dn30": "is down 30%",
}
_WATCH_EVM = {"base", "bsc", "ethereum", "robinhood", "arc"}


def _index_ro():
    import sqlite3
    path = os.environ.get("CURVE_INDEX_DB") or os.path.join(os.path.dirname(db.DB_PATH) or ".", "curve_index.db")
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _watch_conn():
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS watch_coins (id INTEGER PRIMARY KEY AUTOINCREMENT, chain TEXT NOT NULL, token TEXT NOT NULL, "
        "curve TEXT NOT NULL, symbol TEXT, UNIQUE (chain, token));"
        "CREATE TABLE IF NOT EXISTS watch_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, coin_id INTEGER NOT NULL, "
        "kind TEXT NOT NULL, base_price REAL, created_at INTEGER, fired_at INTEGER, UNIQUE (user_id, coin_id, kind));")
    return conn


def _coin_state(chain: str, token: str):
    try:
        c = _index_ro()
        r = c.execute("SELECT * FROM curves WHERE chain = ? AND (token = ? OR lower(token) = lower(?))", (chain, token, token)).fetchone()
        c.close()
        return r
    except Exception:
        return None


def _coin_progress(r) -> float:
    g = int(r["grad_target"] or 0)
    return 100.0 if r["graduated"] else (min(100.0, int(r["real_eth"] or 0) * 100.0 / g) if g else 0.0)


def _coin_link(chain: str, token: str, curve: str) -> str:
    if chain == "solana":
        return f"https://ferzan-factory.com/coin/solana/{token}"
    if chain in _WATCH_EVM:
        return f"https://ferzan-factory.com/coin/{chain}/{curve.lower()}"
    return f"https://t.me/{TRADE}?start=buy_{token}"


async def _watch_start(update: Update, arg: str) -> None:
    chain, _, token = arg.partition("_")
    r = _coin_state(chain, token) if re.fullmatch(r"[a-z]{2,12}", chain) and _FOLLOW_ADDR.fullmatch(token or "") else None
    if not r:
        await update.effective_message.reply_text("Alerts work for coins on a Ferzan curve. I can't find this one; open it again from its page on ferzan-factory.com.")
        return
    conn = _watch_conn()
    conn.execute("INSERT OR IGNORE INTO watch_coins (chain, token, curve, symbol) VALUES (?, ?, ?, ?)", (r["chain"], r["token"], r["curve"], r["symbol"]))
    cid = conn.execute("SELECT id FROM watch_coins WHERE chain = ? AND token = ?", (r["chain"], r["token"])).fetchone()[0]
    conn.commit()
    conn.close()
    prog = _coin_progress(r)
    row1 = [InlineKeyboardButton(label, callback_data=f"wa:{k}:{cid}") for k, label, show in (
        ("p50", "50% of curve", prog < 50 and not r["graduated"]), ("p90", "90% of curve", prog < 90 and not r["graduated"]),
        ("grad", "🎓 Graduates", not r["graduated"])) if show]
    row2 = [InlineKeyboardButton("📈 +50%", callback_data=f"wa:up50:{cid}"), InlineKeyboardButton("🚀 2x", callback_data=f"wa:x2:{cid}"),
            InlineKeyboardButton("📉 −30%", callback_data=f"wa:dn30:{cid}")]
    kb = InlineKeyboardMarkup([x for x in (row1, row2) if x] + [[InlineKeyboardButton("📋 My alerts", callback_data="wl:list")]])
    await update.effective_message.reply_text(
        f"🔔 Alerts for ${r['symbol']}\n{'Graduated' if r['graduated'] else f'{prog:.0f}% of the way to graduation'}.\n\n"
        "Pick when I should message you. Price alerts count from the price right now.", reply_markup=kb)


async def watch_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    parts = (q.data or "").split(":")
    conn = _watch_conn()
    try:
        if parts[0] == "wa" and len(parts) == 3 and parts[1] in _WATCH_KINDS and parts[2].isdigit():
            coin = conn.execute("SELECT chain, token, symbol FROM watch_coins WHERE id = ?", (int(parts[2]),)).fetchone()
            if not coin:
                return
            active = conn.execute("SELECT COUNT(*) FROM watch_alerts WHERE user_id = ? AND fired_at IS NULL", (uid,)).fetchone()[0]
            if active >= 50:
                await q.message.reply_text("You have 50 alerts waiting already. Remove some with /alerts first.")
                return
            r = _coin_state(coin[0], coin[1])
            conn.execute("INSERT OR REPLACE INTO watch_alerts (user_id, coin_id, kind, base_price, created_at, fired_at) VALUES (?, ?, ?, ?, ?, NULL)",
                         (uid, int(parts[2]), parts[1], float(r["price"] or 0) if r else 0.0, int(time.time())))
            conn.commit()
            await q.message.reply_text(f"✅ I'll message you when ${coin[2]} {_WATCH_KINDS[parts[1]]}. See all: /alerts")
            return
        if parts[0] == "wd" and len(parts) == 2 and parts[1].isdigit():
            conn.execute("DELETE FROM watch_alerts WHERE id = ? AND user_id = ?", (int(parts[1]), uid))
            conn.commit()
        text, kb = _alerts_view(conn, uid)
        try:
            await q.edit_message_text(text, reply_markup=kb)
        except Exception:
            await q.message.reply_text(text, reply_markup=kb)
    finally:
        conn.close()


def _alerts_view(conn, uid: int):
    rows = conn.execute("SELECT a.id, a.kind, c.symbol FROM watch_alerts a JOIN watch_coins c ON c.id = a.coin_id "
                        "WHERE a.user_id = ? AND a.fired_at IS NULL ORDER BY a.created_at DESC LIMIT 40", (uid,)).fetchall()
    if not rows:
        return ("No alerts waiting. Open a coin on ferzan-factory.com and tap Alerts.", None)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"❌ ${sym} {_WATCH_KINDS.get(kind, kind)}", callback_data=f"wd:{aid}")] for aid, kind, sym in rows])
    return (f"{len(rows)} alert{'s' if len(rows) != 1 else ''} waiting. Tap one to remove it.", kb)


async def alerts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    conn = _watch_conn()
    try:
        text, kb = _alerts_view(conn, update.effective_user.id)
    finally:
        conn.close()
    await update.effective_message.reply_text(text, reply_markup=kb)


async def _watch_loop(application: Application):
    """Every 30s: fire coin alerts whose condition is met (one message each, then done)."""
    while True:
        await asyncio.sleep(30)
        try:
            conn = _watch_conn()
            rows = conn.execute("SELECT a.id, a.user_id, a.kind, a.base_price, c.chain, c.token, c.curve, c.symbol FROM watch_alerts a "
                                "JOIN watch_coins c ON c.id = a.coin_id WHERE a.fired_at IS NULL LIMIT 5000").fetchall()
            states: dict = {}
            fired = []
            for aid, uid, kind, base, chain, token, curve, sym in rows:
                if (chain, token) not in states:
                    states[(chain, token)] = _coin_state(chain, token)
                r = states[(chain, token)]
                if not r:
                    continue
                prog, price, base = _coin_progress(r), float(r["price"] or 0), float(base or 0)
                hit = {"p50": prog >= 50, "p90": prog >= 90, "grad": bool(r["graduated"]),
                       "up50": base > 0 and price >= base * 1.5, "x2": base > 0 and price >= base * 2,
                       "dn30": base > 0 and 0 < price <= base * 0.7}.get(kind, False)
                if hit:
                    fired.append((aid, uid, kind, chain, token, curve, sym))
            for aid, uid, kind, chain, token, curve, sym in fired[:300]:
                conn.execute("UPDATE watch_alerts SET fired_at = ? WHERE id = ?", (int(time.time()), aid))
                conn.commit()
                icon = {"grad": "🎓", "dn30": "📉", "x2": "🚀", "up50": "📈"}.get(kind, "🔔")
                try:
                    await application.bot.send_message(
                        chat_id=uid, text=f"{icon} ${sym} {_WATCH_KINDS[kind]}.\n{_coin_link(chain, token, curve)}",
                        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚡ Trade it", url=f"https://t.me/{TRADE}?start=buy_{token}")]]))
                except Exception as e:
                    logger.info("alert %s not delivered: %s", aid, str(e)[:80])
                await asyncio.sleep(0.05)
            conn.close()
        except Exception as e:
            logger.warning("watch loop: %s", str(e)[:160])


_MS_STEPS = (("p25", 25.0), ("p50", 50.0), ("p90", 90.0), ("grad", 100.0))
_MS_SYMBOLS = {"p25": "🔥", "p50": "🚀", "p90": "⚡", "grad": "🎓"}
_MS_TAGS = {"solana": "Solana", "base": "Base", "bsc": "BNBChain", "ethereum": "Ethereum", "robinhood": "RobinhoodChain"}


def _ms_caption(kind: str, sym: str) -> str:
    s = _esc(sym)
    return {
        "p25": f"🔥 <b>${s}</b> just hit <b>25%</b> of the way to graduation.",
        "p50": f"🚀 Halfway there! <b>${s}</b> is at <b>50%</b> of the way to graduation.",
        "p90": f"⚡ Almost there: <b>${s}</b> is at <b>90%</b>. Graduation is close.",
        "grad": f"🎓 <b>${s} GRADUATED!</b> Its liquidity now moves to the DEX pool.",
    }[kind]


def _ms_conn():
    import sqlite3
    conn = sqlite3.connect(db.DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS creator_milestones (request_id TEXT NOT NULL, kind TEXT NOT NULL, "
                 "sent_at INTEGER NOT NULL, PRIMARY KEY (request_id, kind))")
    return conn


def _ms_due(progress: float, graduated: bool, sent: set) -> list:
    """Milestones reached and not yet sent, in order. A coin that jumped past several gets one message (the top one)."""
    reached = [k for k, at in _MS_STEPS if (graduated if k == "grad" else progress >= at)]
    return [k for k in reached if k not in sent]


async def _milestone_loop(application: Application):
    """Every 90s: tell a creator when their coin reaches 25 / 50 / 90 percent and when it graduates, with the
    card image and a Share to X button. The first pass after a deploy only records where coins already are, so
    nobody is messaged about an old milestone."""
    if os.environ.get("LAUNCH_MILESTONES", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    import datetime as _dt
    import json
    import requests
    from urllib.parse import quote
    while True:
        await asyncio.sleep(90)
        try:
            conn = _ms_conn()
            first = conn.execute("SELECT 1 FROM creator_milestones WHERE request_id = '__init__'").fetchone() is None
            cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=21)).isoformat()
            rows = conn.execute(
                "SELECT id, chat_id, chain, name, symbol, result_token_address, extra_params FROM launch_requests "
                "WHERE status = 'confirmed' AND chat_id != 0 AND result_token_address != '' AND mode IN ('bonding_curve','meteora') "
                "AND created_at >= ? ORDER BY created_at DESC LIMIT 400", (cutoff,)).fetchall()
            sent_map: dict = {}
            for rid, kind in conn.execute("SELECT request_id, kind FROM creator_milestones").fetchall():
                sent_map.setdefault(rid, set()).add(kind)
            for rid, chat_id, chain, name, sym, token, extra in rows:
                r = _coin_state(chain, token)
                if not r:
                    continue
                prog, grad = _coin_progress(r), bool(r["graduated"])
                due = _ms_due(prog, grad, sent_map.get(rid, set()))
                if not due:
                    continue
                now = int(time.time())
                for k in due:  # record everything reached; only the top one is announced
                    conn.execute("INSERT OR IGNORE INTO creator_milestones VALUES (?, ?, ?)", (rid, k, now))
                conn.commit()
                if first:
                    continue
                top = due[-1]
                png = None
                try:
                    resp = await asyncio.to_thread(lambda: requests.get(f"{LAUNCH_API}/api/og/{chain}/{token}.png?fresh=1", timeout=20))
                    png = resp.content if resp.ok else None
                except Exception:
                    png = None
                base = (os.environ.get("LAUNCH_PUBLIC_URL") or "https://launch.ferzaneco.com").rstrip("/")
                link = f"{base}/api/share/{chain}/{token}"
                said = {"p25": "is 25% of the way to graduating", "p50": "is halfway to graduating", "p90": "is at 90% and about to graduate",
                        "grad": "just graduated"}[top]
                tweet = f"{_MS_SYMBOLS[top]} ${sym} {said} on @ferzaneco Ferzan Factory."
                x = f"https://twitter.com/intent/tweet?text={quote(tweet)}&url={quote(link, safe='')}&hashtags=memecoin,{_MS_TAGS.get(chain, 'crypto')}"
                kb = InlineKeyboardMarkup([
                    [InlineKeyboardButton("𝕏 Share this win", url=x)],
                    [InlineKeyboardButton("📈 Open coin", url=_coin_link(chain, token, str((json.loads(extra or '{}') or {}).get('curve_address') or ''))),
                     InlineKeyboardButton("💰 Claim fees", callback_data="go:claim")],
                ])
                cap = _ms_caption(top, sym)
                try:
                    if png:
                        import io as _io
                        await application.bot.send_photo(chat_id=chat_id, photo=_io.BytesIO(png), caption=cap, parse_mode="HTML", reply_markup=kb)
                    else:
                        await application.bot.send_message(chat_id=chat_id, text=cap, parse_mode="HTML", reply_markup=kb)
                except Exception as e:
                    logger.info("milestone %s not delivered: %s", rid, str(e)[:80])
                await asyncio.sleep(0.1)
            if first:
                conn.execute("INSERT OR IGNORE INTO creator_milestones VALUES ('__init__', 'ran', ?)", (int(time.time()),))
                conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("milestone loop: %s", str(e)[:160])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if context.args and (context.args[0] or "").startswith("watch_"):
        await _watch_start(update, context.args[0][len("watch_"):])
        return
    if context.args and (context.args[0] or "").startswith("follow_"):
        await _follow_start(update, context.args[0][len("follow_"):])
        return
    if context.args:
        raw = (context.args[0] or "").replace("ref_", "").replace("ref", "")
        if raw.isdigit():
            if db.set_referrer(uid, int(raw)):
                await update.effective_message.reply_text("Referral locked. You launch, they earn a cut of curve fees.")
    rows0 = []
    if update.effective_chat and update.effective_chat.type == "private" and MINI_APP_BASE_URL.startswith("https://"):
        rows0 = [[InlineKeyboardButton("📱 Open Launch app", web_app=WebAppInfo(url=f"{MINI_APP_BASE_URL}/app.html"))]]
    kb = InlineKeyboardMarkup(
        rows0 + [
            [InlineKeyboardButton("🚀 Launch a token", callback_data="go:launch")],
            [InlineKeyboardButton("🆕 New launches · 👑 King of the Hill", callback_data="go:feed")],
            [InlineKeyboardButton("📜 My launches", callback_data="go:history"),
             InlineKeyboardButton("💰 Claim fees", callback_data="go:claim")],
            [
                InlineKeyboardButton("⚡ Trade", url=f"https://t.me/{TRADE}"),
                InlineKeyboardButton("💧 Liq", url=f"https://t.me/{LIQ}"),
            ],
            [InlineKeyboardButton("🟢 Buy alerts", url=f"https://t.me/{BUY}"),
             InlineKeyboardButton("🏅 Holder tiers", callback_data="go:tiers")],
            [InlineKeyboardButton("💬 Community", url=CHAT)],
        ]
    )
    live = " · ".join(CHAINS[c].split(" (")[0] for c in _live_chains()) or "—"
    await update.effective_message.reply_photo(
        photo=LAUNCH_BANNER_FILE_ID,
        caption=(
            "🚀 <b>Ferzan Launch</b>\n\n"
            "Create a token from Telegram. You sign in your own wallet — "
            "this bot never holds keys.\n\n"
            f"Live now: {_esc(live)}\n"
            "Solana: plain token or Meteora bonding curve.\n\n"
            "Tap Launch — every step has quick-pick buttons.\n"
            "Schedule a launch for later with ⏰ Launch later (see /drafts).\n"
            "The exact cost is shown before you sign."
        ),
        parse_mode="HTML",
        reply_markup=kb,
    )


# ----------------------------------------------------------------- chain --
def _chain_rows() -> list:
    live = _live_chains()
    rows, row = [], []
    for c in live:
        row.append(InlineKeyboardButton(CHAINS[c].split(" (")[0], callback_data=f"chain:{c}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def _chain_text() -> str:
    soon = [CHAINS[c].split(" (")[0] for c in CHAINS if c not in _live_chains()]
    t = "<b>Which chain do you want to launch on?</b>"
    if soon:
        t += f"\n\n<i>Coming soon: {_esc(' · '.join(soon))}</i>"
    return t


async def go_launch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        await q.message.delete()
    except Exception:
        pass
    context.user_data.pop("launch", None)
    await context.bot.send_message(
        chat_id=q.message.chat_id, text=_chain_text(), parse_mode="HTML", reply_markup=_kb(_chain_rows())
    )
    return CHOOSING_CHAIN


async def launch_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("launch", None)
    await update.message.reply_text(_chain_text(), parse_mode="HTML", reply_markup=_kb(_chain_rows()))
    return CHOOSING_CHAIN


async def chain_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chain = query.data.split(":", 1)[1]
    if chain not in CHAINS:
        return CHOOSING_CHAIN
    context.user_data["launch"] = {"chain": chain, "extra_params": {}}
    launch = context.user_data["launch"]
    rows, lines = [], []
    if chain == "solana":
        rows.append([InlineKeyboardButton("🔸 Plain token", callback_data="mode:plain")])
        lines.append("🔸 <b>Plain token</b> — you mint a fixed supply once. No trading fee, no curve.")
        if _curve_live("solana"):
            rows.append([InlineKeyboardButton("🚀 Meteora bonding curve", callback_data="mode:meteora")])
            lines.append("🚀 <b>Meteora bonding curve</b> — trades on a curve from the first second, "
                         "moves to a full pool when it fills. You earn half of every trading fee.")
    else:
        if _plain_live(chain):
            rows.append([InlineKeyboardButton("🔸 Standard token", callback_data="mode:plain")])
            lines.append("🔸 <b>Standard token</b> — fixed supply, no owner, can never be minted again.")
        if _curve_live(chain):
            rows.append([InlineKeyboardButton("🚀 Bonding curve", callback_data="mode:bonding_curve")])
            lines.append("🚀 <b>Bonding curve</b> — trades on a curve, graduates to a DEX pool. Fee on every trade.")
        else:
            rows.append([InlineKeyboardButton("🚀 Bonding curve — coming soon", callback_data="mode:soon")])
    rows.append([InlineKeyboardButton("↩️ Pick another chain", callback_data="lx:chains")])
    await query.edit_message_text(
        f"<b>Launch type</b>\n\nLaunching on <b>{_esc(CHAINS[chain])}</b>. How should your token work?\n\n"
        + "\n".join(lines),
        parse_mode="HTML",
        reply_markup=_kb(rows),
    )
    return CHOOSING_MODE


async def back_to_chains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data.pop("launch", None)
    await q.edit_message_text(_chain_text(), parse_mode="HTML", reply_markup=_kb(_chain_rows()))
    return CHOOSING_CHAIN


async def mode_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    mode = query.data.split(":", 1)[1]
    if mode == "soon":
        await query.answer("Bonding curves on this chain are coming soon — pick Standard token for now.", show_alert=True)
        return CHOOSING_MODE
    await query.answer()
    launch = context.user_data.get("launch")
    if not launch:
        await query.edit_message_text("That launch expired — tap /launch to start again.")
        return ConversationHandler.END
    launch["mode"] = mode
    if mode == "meteora":
        # the Ferzan Meteora config fixes supply + decimals on-chain
        launch["decimals"] = 6
        launch["total_supply_raw"] = str(10**9 * 10**6)
        launch["supply_display"] = "1,000,000,000 (fixed for Meteora curves)"
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _send(
        update,
        _hdr(launch, "name", "Name") + "What should your token be called?\n"
        "This is the full name people see in wallets and explorers (max 32 characters).\n"
        "Example: <code>My Cool Token</code>\n\n<i>Type it below.</i>",
    )
    return ENTERING_NAME


# ------------------------------------------------------------ name/symbol --
async def name_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data.get("launch")
    if not launch:
        return ConversationHandler.END
    name = " ".join((update.message.text or "").split())
    if not name or len(name) > 32:
        await update.message.reply_text("Name must be 1–32 characters — try again.")
        return ENTERING_NAME
    launch["name"] = name
    sug = _auto_symbol(name)
    await _send(
        update,
        _hdr(launch, "symbol", "Ticker") + "What's the ticker? 2–10 letters or numbers, no spaces.\n"
        f"Tap the suggestion or type your own.",
        [[InlineKeyboardButton(f"Use ${sug}", callback_data=f"sym:{sug}")]],
    )
    return ENTERING_SYMBOL


async def _after_symbol(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    await _send(
        update,
        _hdr(launch, "logo", "Logo") + "Send your token's logo as a picture (square works best).\n"
        "It shows in wallets, explorers, your trade page and the launch announcement.",
        [[InlineKeyboardButton("⏭ Skip - no logo", callback_data="logo:skip")]],
    )
    return ENTERING_LOGO


async def _save_logo(update: Update, context: ContextTypes.DEFAULT_TYPE, tg_file_id: str, size: int, ext: str):
    launch = context.user_data["launch"]
    if size and size > MAX_LOGO_BYTES:
        await update.message.reply_text("That picture is over 5 MB - send a smaller one, or tap Skip.")
        return ENTERING_LOGO
    try:
        f = await context.bot.get_file(tg_file_id)
        path = lx.media_path(ext)
        await f.download_to_drive(custom_path=str(path))
        launch["image_url"] = lx.public_media_url(path, MINI_APP_BASE_URL)
    except Exception:
        logger.exception("logo download failed")
        await update.message.reply_text("Couldn't save that picture - try again, or tap Skip.")
        return ENTERING_LOGO
    await update.message.reply_text("✅ Logo saved.")
    return await _ask_info(update, context)


async def logo_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    ph = update.message.photo[-1]
    return await _save_logo(update, context, ph.file_id, ph.file_size or 0, "jpg")


async def logo_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    doc = update.message.document
    mime = (doc.mime_type or "").lower()
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime)
    if not ext:
        await update.message.reply_text("Send a PNG, JPG, WEBP or GIF picture - or tap Skip.")
        return ENTERING_LOGO
    return await _save_logo(update, context, doc.file_id, doc.file_size or 0, ext)


async def logo_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (update.message.text or "").strip().lower() in {"skip", "no", "none"}:
        return await _ask_info(update, context)
    await update.message.reply_text("Send the logo as a picture, or tap Skip.")
    return ENTERING_LOGO


async def logo_skip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _tap(update)
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    return await _ask_info(update, context)


async def _ask_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    await _send(
        update,
        _hdr(launch, "info", "Description & links") + "Send a short description and any links, all in one message:\n"
        "<code>The first cat coin on BNB\nhttps://mycat.xyz\nx.com/mycat\nt.me/mycatchat</code>\n\n"
        "Website, X and Telegram links are picked out automatically.",
        [[InlineKeyboardButton("⏭ Skip", callback_data="info:skip")]],
    )
    return ENTERING_INFO


async def info_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data.get("launch")
    if not launch:
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if text.lower() not in {"skip", "no", "none"}:
        info = lx.parse_info(text)
        launch["description"] = info["description"]
        for k in ("website", "x", "telegram"):
            if info[k]:
                launch["extra_params"][k] = info[k]
        got = [k for k in ("website", "x", "telegram") if info[k]]
        await update.message.reply_text(
            "✅ Saved" + (f" - links: {', '.join('X' if g == 'x' else g.capitalize() for g in got)}." if got else ".")
        )
    return await _after_info(update, context)


async def info_skip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _tap(update)
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    return await _after_info(update, context)


async def _after_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    if launch["mode"] == "meteora":
        return await _ask_devbuy(update, context)
    if launch.get("chain") == "ton" and launch["mode"] == "bonding_curve":
        rows = [[InlineKeyboardButton(lbl, callback_data=f"sup:{val}") for lbl, val in TON_CURVE_SUPPLY_PRESETS[i:i + 2]]
                for i in (0, 2)]
        limit = "\nTON curves allow up to 1B."
    else:
        rows = [[InlineKeyboardButton(lbl, callback_data=f"sup:{val}") for lbl, val in SUPPLY_PRESETS[i:i + 3]]
                for i in (0, 3)]
        limit = ""
    await _send(
        update,
        _hdr(launch, "supply", "Total supply") + "How many tokens should exist in total?\n"
        "Most launches use <b>1B</b>. Tap one, or type a number like <code>420000000</code> or <code>69m</code>.\n\n"
        "⚡ <b>Quick launch</b> uses 1B supply and the recommended settings for everything else "
        "(no team wallets, no dev buy, trading opens right away, no buy limit). You review it before anything is sent." + limit,
        rows + [[InlineKeyboardButton("⚡ Quick launch - recommended settings", callback_data="sup:quick")]],
    )
    return ENTERING_SUPPLY


async def symbol_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data.get("launch")
    if not launch:
        return ConversationHandler.END
    sym = (update.message.text or "").strip().lstrip("$").upper()
    if not re.fullmatch(r"[A-Z0-9]{2,10}", sym):
        await update.message.reply_text("Ticker must be 2–10 letters/numbers, no spaces — try again.")
        return ENTERING_SYMBOL
    launch["symbol"] = sym
    return await _after_symbol(update, context)


async def symbol_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _tap(update)
    launch = context.user_data.get("launch")
    if not launch:
        return ConversationHandler.END
    launch["symbol"] = update.callback_query.data.split(":", 1)[1]
    return await _after_symbol(update, context)


# ------------------------------------------------------------- time zone --
async def _need_tz(update: Update, context: ContextTypes.DEFAULT_TYPE, nxt: str):
    """Ask for the user's time zone once, then continue with `nxt` ('open' or 'later')."""
    context.user_data["tz_next"] = nxt
    if lx.get_tz(update.effective_user.id):
        return await _tz_continue(update, context)
    await _send(
        update,
        "<b>🌍 Your time zone</b>\n\nPick it once so times like <code>8pm</code> mean <i>your</i> 8pm. "
        "Not listed? Type it, e.g. <code>Asia/Tokyo</code> or <code>America/Phoenix</code>.",
        _tz_rows("tz"),
    )
    return CHOOSING_TZ


async def _tz_continue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nxt = context.user_data.pop("tz_next", "open")
    if nxt == "later":
        return await _ask_later(update, context)
    return await _ask_open_at(update, context)


async def tz_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _tap(update)
    lx.set_tz(update.effective_user.id, update.callback_query.data.split(":", 1)[1])
    return await _tz_continue(update, context)


async def tz_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = lx.valid_tz(update.message.text or "")
    if not tz:
        await update.message.reply_text("I don't know that time zone - tap one of the buttons, or type e.g. Europe/Paris.")
        return CHOOSING_TZ
    lx.set_tz(update.effective_user.id, tz)
    return await _tz_continue(update, context)


async def timezone_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    arg = " ".join(context.args or []).strip()
    if arg:
        tz = lx.valid_tz(arg)
        if not tz:
            await update.effective_message.reply_text("Unknown time zone. Example: /timezone America/New_York")
            return
        lx.set_tz(update.effective_user.id, tz)
        await update.effective_message.reply_text(f"✅ Time zone set to {tz}.")
        return
    cur = lx.get_tz(update.effective_user.id) or "not set"
    await update.effective_message.reply_text(
        f"Your time zone: <b>{_esc(cur)}</b>\nPick a new one, or send /timezone Area/City.",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(_tz_rows("tzset")),
    )


async def tzset_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    tz = q.data.split(":", 1)[1]
    lx.set_tz(update.effective_user.id, tz)
    await q.answer("Saved")
    await q.edit_message_text(f"✅ Time zone set to {tz}.")


# ---------------------------------------------------------------- supply --
async def _set_supply(update: Update, context: ContextTypes.DEFAULT_TYPE, whole: int):
    launch = context.user_data["launch"]
    decimals = {"solana": 6, "ton": 9, "tron": 6}.get(launch["chain"], 18)
    raw = whole * (10 ** decimals)
    if launch["chain"] == "ton" and launch["mode"] == "bonding_curve" and whole > TON_CURVE_MAX_SUPPLY:
        await update.effective_message.reply_text(
            f"TON curves allow at most {_fmt_int(TON_CURVE_MAX_SUPPLY)} tokens. Pick a smaller supply.")
        return ENTERING_SUPPLY
    if launch["chain"] == "solana" and raw > SOL_U64_MAX:
        await update.effective_message.reply_text(
            f"Too big for Solana — max is {_fmt_int(SOL_U64_MAX // 10**6)}. Pick a smaller supply."
        )
        return ENTERING_SUPPLY
    launch["total_supply_raw"] = str(raw)
    launch["decimals"] = decimals
    launch["supply_display"] = f"{_fmt_int(whole)} ({_short_num(whole)})"
    launch["supply_whole"] = whole
    if launch["mode"] == "bonding_curve":
        return await _ask_grad(update, context)
    if launch["chain"] in EVM_CHAINS:
        return await _ask_allocs(update, context)
    return await _show_confirm(update, context)


async def supply_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    whole = _parse_supply(update.message.text)
    if not whole:
        await update.message.reply_text("That doesn't look like a number — try <code>1000000000</code> or <code>1b</code>.",
                                        parse_mode="HTML")
        return ENTERING_SUPPLY
    return await _set_supply(update, context, whole)


async def supply_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _tap(update)
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    val = update.callback_query.data.split(":", 1)[1]
    if val == "quick":
        context.user_data["launch"]["quick"] = True
        val = str(10**9)
    return await _set_supply(update, context, int(val))


# ------------------------------------------------------ curve: graduation --
async def _ask_grad(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    unit = NATIVE.get(launch["chain"], "native")
    pre = ton_grad_presets() if launch["chain"] == "ton" else GRAD_PRESETS.get(launch["chain"], GRAD_PRESETS["default"])
    floor = f"\nMinimum on TON: {ton_curve_min_grad():,.0f} TON." if launch["chain"] == "ton" else ""
    if launch.get("quick"):  # middle preset: the recommended graduation size
        return await _set_grad(update, context, float(pre[1] if len(pre) > 1 else pre[0]))
    await _send(
        update,
        _hdr(launch, "grad", "Graduation") + f"How much {unit} should the curve collect before it moves to a full DEX pool?\n"
        "Smaller = graduates faster. Tap one or type an amount." + floor,
        [[InlineKeyboardButton(f"{p} {unit}", callback_data=f"grad:{p}") for p in pre]],
    )
    return ENTERING_GRAD


async def _set_grad(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float):
    launch = context.user_data["launch"]
    if amount <= 0:
        await update.effective_message.reply_text("Graduation amount must be above 0.")
        return ENTERING_GRAD
    extra = launch["extra_params"]
    if launch["chain"] == "ton":
        lo = ton_curve_min_grad()
        if amount < lo or amount > TON_CURVE_MAX_GRAD:
            await update.effective_message.reply_text(
                f"TON curves graduate between {lo:,.0f} and {TON_CURVE_MAX_GRAD:,} TON (graduation opens a STON.fi pool). "
                "Pick an amount in that range.")
            return ENTERING_GRAD
        extra["graduation_eth_threshold"] = str(int(round(amount * 10**9)))  # nanoTON, as api.py stores it
        extra["graduation_display"] = f"{amount:g} TON"
        extra["dev_buy"], extra["max_buy"], extra["start_minutes"] = "0", "0", "0"  # not in the TON curve contract
        return await _show_confirm(update, context)
    if launch["chain"] == "tron" and amount < tron.curve_min_grad_trx():
        await update.effective_message.reply_text(
            f"Tron curves graduate at {tron.curve_min_grad_trx():,.0f} TRX or more (graduation opens a SunSwap pool, "
            "which costs about 230 TRX of energy). Pick a bigger amount.")
        return ENTERING_GRAD
    extra["graduation_eth_threshold"] = str(int(round(amount * 10 ** (6 if launch["chain"] == "tron" else 18))))
    extra["graduation_display"] = f"{amount:g} {NATIVE.get(launch['chain'], '')}"
    # starting price / curve depth: sensible defaults (not asked any more)
    extra.setdefault("virtual_eth_reserve", str(10**18))
    extra.setdefault("virtual_token_reserve", str(int(launch["total_supply_raw"]) * 80 // 100))
    if launch["chain"] == "tron":  # no team wallets on Tron curves: the coin only moves through the curve until graduation
        return await _ask_devbuy(update, context)
    return await _ask_allocs(update, context)


async def grad_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amt = _parse_amount(update.message.text)
    if amt is None:
        await update.message.reply_text("Type an amount like 5 or 2.5.")
        return ENTERING_GRAD
    return await _set_grad(update, context, amt)


async def grad_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _tap(update)
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    val = update.callback_query.data.split(":", 1)[1]
    amt = _parse_amount(val) if val != "default" else float(GRAD_PRESETS.get(
        context.user_data["launch"]["chain"], GRAD_PRESETS["default"])[-1])
    return await _set_grad(update, context, amt or 0)


# ------------------------------------------------------------ team allocs --
async def _ask_allocs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    if launch.get("quick"):
        launch["extra_params"]["allocs"] = ""
        return await _after_allocs(update, context)
    await _send(
        update,
        _hdr(launch, "allocs", "Team wallets") + "Send part of the supply straight to team wallets at launch?\n"
        "Most launches skip this.\n\n"
        "To add wallets, type <code>address:percent</code>, comma-separated (max 20% per wallet):\n"
        "<code>0xabc…:5, 0xdef…:2.5</code>",
        [[InlineKeyboardButton("⏭ No team wallets", callback_data="allocs:skip")]],
    )
    return ENTERING_ALLOCS


def _parse_allocs_text(text: str) -> tuple[str, str] | tuple[None, str]:
    """-> (normalized 'addr:bps,...', display) or (None, error)."""
    out, disp, total = [], [], 0
    for part in re.split(r"[,;\n]+", text or ""):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(0x[0-9a-fA-F]{40})\s*[: ]\s*(\d+(?:\.\d+)?)\s*%?", part)
        if not m:
            return None, f"Couldn't read <code>{_esc(part[:60])}</code> — use <code>0xAddress:5</code>."
        pct = float(m.group(2))
        bps = int(round(pct * 100))
        if bps <= 0 or bps > 2000:
            return None, "Each wallet can get between 0.01% and 20%."
        total += bps
        out.append(f"{m.group(1)}:{bps}")
        disp.append(f"{m.group(1)[:6]}…{m.group(1)[-4:]} {pct:g}%")
    if not out:
        return None, "No wallets found — tap No team wallets, or type <code>0xAddress:5</code>."
    if total >= 10_000:
        return None, "Team wallets can't take 100% of the supply."
    return ",".join(out), ", ".join(disp)


async def _after_allocs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    ref = db.get_referrer(update.effective_user.id)
    if ref:
        launch["extra_params"]["referrer_id"] = str(ref)
    if launch["mode"] in CURVE_MODES:
        return await _ask_devbuy(update, context)
    return await _show_confirm(update, context)


async def allocs_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data.get("launch")
    if not launch:
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if text.lower() in {"skip", "none", "no", "0"}:
        launch["extra_params"]["allocs"] = ""
        return await _after_allocs(update, context)
    norm, disp = _parse_allocs_text(text)
    if norm is None:
        await update.message.reply_text(disp, parse_mode="HTML")
        return ENTERING_ALLOCS
    launch["extra_params"]["allocs"] = norm
    launch["extra_params"]["allocs_display"] = disp
    return await _after_allocs(update, context)


async def allocs_skip_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _tap(update)
    launch = context.user_data.get("launch")
    if not launch:
        return ConversationHandler.END
    launch["extra_params"]["allocs"] = ""
    return await _after_allocs(update, context)


# ---------------------------------------------------------------- dev buy --
async def _ask_devbuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    if launch.get("quick"):
        return await _set_devbuy(update, context, 0.0)
    unit = NATIVE.get(launch["chain"], "native")
    pre = DEVBUY_PRESETS.get(launch["chain"], DEVBUY_PRESETS["default"])
    note = ""
    if launch["mode"] == "meteora":
        note = ("\n\n<i>Meteora curve: 1B supply, 6 decimals, graduates at about 84 SOL raised. "
                "Trading fee starts at 50% and drops to 1% over the first 60 seconds to stop snipers.</i>")
    await _send(
        update,
        _hdr(launch, "devbuy", "Dev buy") + f"Buy some of your own token in the same transaction as the launch, "
        f"before anyone else can?\nTap an amount in {unit}, type your own, or skip." + note,
        [[InlineKeyboardButton(f"{p} {unit}", callback_data=f"devbuy:{p}") for p in pre],
         [InlineKeyboardButton("⏭ No dev buy", callback_data="devbuy:skip")]],
    )
    return ENTERING_DEVBUY


async def _set_devbuy(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float):
    launch = context.user_data["launch"]
    launch["extra_params"]["dev_buy"] = f"{amount:g}" if amount > 0 else "0"
    if launch["mode"] == "meteora":
        return await _show_confirm(update, context)
    return await _ask_window(update, context)


async def devbuy_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    amt = _parse_amount(update.message.text)
    if amt is None:
        await update.message.reply_text("Type an amount like 0.05, or 0 to skip.")
        return ENTERING_DEVBUY
    return await _set_devbuy(update, context, amt)


async def devbuy_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _tap(update)
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    return await _set_devbuy(update, context, _parse_amount(update.callback_query.data.split(":", 1)[1]) or 0.0)


# ------------------------------------------------- curve: trading window --
async def _ask_window(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    if launch.get("quick"):
        return await _set_window(update, context, 0)
    await _send(
        update,
        _hdr(launch, "window", "Trading opens") + "When should trading open?\n"
        "Your token is created right away, so you can post the contract and build hype - "
        "nobody can buy until this time (your dev buy is the only exception).",
        [[InlineKeyboardButton("⚡ Right away", callback_data="window:0"),
          InlineKeyboardButton("15 min", callback_data="window:15"),
          InlineKeyboardButton("1 hour", callback_data="window:60")],
         [InlineKeyboardButton("6 hours", callback_data="window:360"),
          InlineKeyboardButton("24 hours", callback_data="window:1440"),
          InlineKeyboardButton("📅 Pick a time", callback_data="window:pick")]],
    )
    return ENTERING_WINDOW


async def _set_window(update: Update, context: ContextTypes.DEFAULT_TYPE, mins: int, start_at: int = 0):
    launch = context.user_data["launch"]
    extra = launch["extra_params"]
    mins = max(0, min(int(mins), MAX_OPEN_DELAY // 60))
    extra["start_minutes"] = str(mins)
    extra.pop("start_at", None)
    if start_at:
        extra["start_at"] = str(int(start_at))
        extra["start_display"] = lx.fmt_when(start_at, lx.get_tz(update.effective_user.id))
    elif mins:
        extra["start_display"] = _dur(mins * 60) + " after launch"
    else:
        extra["start_display"] = "right away"
    if launch.get("quick"):
        return await _set_maxbuy(update, context, 0.0)
    unit = NATIVE.get(launch["chain"], "native")
    pre = MAXBUY_PRESETS.get(launch["chain"], MAXBUY_PRESETS["default"])
    await _send(
        update,
        _hdr(launch, "maxbuy", "Max buy per wallet") + f"Limit how much one wallet can buy on the curve (in {unit})?\n"
        "This slows down whales early on.",
        [[InlineKeyboardButton("♾ No limit", callback_data="maxbuy:0")],
         [InlineKeyboardButton(f"{p} {unit}", callback_data=f"maxbuy:{p}") for p in pre]],
    )
    return ENTERING_MAXBUY


async def window_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if re.fullmatch(r"\\d+", text):
        return await _set_window(update, context, int(text))  # plain number = minutes
    return await open_at_entered(update, context)


async def window_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _tap(update)
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    val = update.callback_query.data.split(":", 1)[1]
    if val == "pick":
        return await _need_tz(update, context, "open")
    return await _set_window(update, context, 0 if val == "instant" else int(val))


async def _ask_open_at(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = lx.get_tz(update.effective_user.id) or "UTC"
    await _send(
        update,
        "<b>📅 Pick when trading opens</b>\n\n"
        f"Type a time in <b>{_esc(tz)}</b> time, for example:\n"
        "<code>8pm</code> · <code>tomorrow 9:30am</code> · <code>sat 8pm</code> · <code>9/27 8pm</code> · <code>in 3h</code>\n\n"
        "Up to 7 days ahead. /timezone changes your time zone.",
    )
    return ENTERING_OPEN_AT


async def open_at_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    tz = lx.get_tz(update.effective_user.id)
    if not tz:
        return await _need_tz(update, context, "open")
    when, err = lx.parse_when(update.message.text or "", tz)
    if not when:
        await update.message.reply_text(err)
        return ENTERING_OPEN_AT
    ts = int(when.timestamp())
    if ts - time.time() > MAX_OPEN_DELAY:
        await update.message.reply_text("That's more than 7 days away - curves can open at most 7 days after launch. "
                                        "Tip: use ⏰ Launch later on the review screen to schedule the whole launch.")
        return ENTERING_OPEN_AT
    return await _set_window(update, context, max(1, int((ts - time.time()) // 60)), start_at=ts)


async def _set_maxbuy(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: float):
    context.user_data["launch"]["extra_params"]["max_buy"] = f"{amount:g}" if amount > 0 else "0"
    return await _show_confirm(update, context)


async def maxbuy_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    amt = _parse_amount(update.message.text)
    if amt is None:
        await update.message.reply_text("Type an amount like 0.1, or 0 for no limit.")
        return ENTERING_MAXBUY
    return await _set_maxbuy(update, context, amt)


async def maxbuy_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _tap(update)
    if not context.user_data.get("launch"):
        return ConversationHandler.END
    return await _set_maxbuy(update, context, _parse_amount(update.callback_query.data.split(":", 1)[1]) or 0.0)


# --------------------------------------------------------------- confirm --
async def _show_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launch = context.user_data["launch"]
    extra = launch.get("extra_params") or {}
    chain, mode = launch["chain"], launch["mode"]
    unit = NATIVE.get(chain, "")
    mode_txt = {"plain": "Standard token" if chain != "solana" else "Plain token",
                "meteora": "Meteora bonding curve", "bonding_curve": "Bonding curve"}.get(mode, mode)
    rows = [
        ("Chain", CHAINS[chain]),
        ("Type", mode_txt),
        ("Name", launch["name"]),
        ("Ticker", "$" + launch["symbol"]),
        ("Supply", launch.get("supply_display", launch.get("total_supply_raw"))),
    ]
    if extra.get("graduation_display"):
        rows.append(("Graduates at", extra["graduation_display"]))
    if extra.get("allocs_display"):
        rows.append(("Team wallets", extra["allocs_display"]))
    ton_curve = chain == "ton" and mode == "bonding_curve"
    if ton_curve:
        rows.append(("Dev buy", "none (TON curves have no dev buy yet)"))
    elif mode in CURVE_MODES:
        dev = extra.get("dev_buy") or "0"
        rows.append(("Dev buy", f"{dev} {unit}" if dev != "0" else "none"))
    rows.append(("Logo", "✅ added" if launch.get("image_url") else "none"))
    links = [n for k, n in (("website", "Website"), ("x", "X"), ("telegram", "Telegram")) if extra.get(k)]
    if links or launch.get("description"):
        rows.append(("Info", ", ".join(links + (["description"] if launch.get("description") else []))))
    if ton_curve:
        rows.append(("Trading opens", "about 2 minutes after launch"))
    elif mode == "bonding_curve":
        rows.append(("Trading opens", extra.get("start_display") or "right away"))
        mb = extra.get("max_buy") or "0"
        rows.append(("Max buy", "no limit" if mb == "0" else f"{mb} {unit} per wallet"))
    tb_info = {}
    if ton_curve:
        fee_ton = tl.launch_fee_nano() / 1e9
        contracts = (tcv.CURVE_TON + tl.DEPLOY_TON + tl.ADMIN_TON) / 1e9
        rows.append(("Launch fee", "Free" if fee_ton == 0 else f"{fee_ton:g} TON"))
        rows.append(("Contracts + gas", f"{contracts:g} TON (the excess comes back as change; "
                     f"{tcv.CURVE_TON / 1e9:g} TON stays in the curve as its gas buffer)"))
        rows.append(("You send", f"about {_ton_need_nano('bonding_curve') / 1e9:g} TON, plus a small wallet network fee"))
        tb_info = await tron.run("info", {"uid": update.effective_user.id, "need_nano": _ton_need_nano("bonding_curve")},
                                 timeout=45, script="ton_launch_exec.py")
    elif mode == "plain" and chain == "ton":
        fee_ton = int(os.environ.get("LAUNCH_FEE_NANOTON") or "300000000") / 1e9
        rows.append(("Launch fee", ("Free" if fee_ton == 0 else f"{fee_ton:g} TON") + " + about 0.3 TON for the contract (most comes back)"))
    elif mode == "plain" and chain in PLAIN_FEE_TEXT:
        rows.append(("Launch fee", PLAIN_FEE_TEXT[chain] + " + network gas"))
    perk_note = ""
    if chain == "solana":
        fee_row, perk_note = await _solana_fee_row(update.effective_user.id)
        rows.append(("Launch fee", fee_row))
    tinfo = {}
    if chain == "tron":
        dev_sun = int(float(extra.get("dev_buy") or 0) * 1e6) if mode == "bonding_curve" else 0
        tinfo = await tron.run("info", {"uid": update.effective_user.id, "curve": mode == "bonding_curve",
                                        "dev_buy_sun": dev_sun}, timeout=45)
        fee = tinfo.get("fee_trx")
        rows.append(("Launch fee", ("Free" if fee == 0 else f"{fee:g} TRX" if fee is not None else "the Ferzan fee")
                     + f" + about {tinfo.get('energy_trx', 16)} TRX of Tron network energy"))
        rows.append(("Paid from", "your Ferzan Trade Bot wallet"))
    text = "<b>Review your launch</b>\n\n" + "\n".join(f"{_esc(k)}: <b>{_esc(v)}</b>" for k, v in rows)
    launch_btn = "✅ Launch now"
    if chain == "tron":
        launch_btn = "✅ Launch from my Trade Bot wallet"
        text += "\n\n" + _tron_wallet_text(tinfo)
        text += ("\n\n<b>Two ways to launch:</b> straight from your Trade Bot wallet (button above), or use your own "
                 "TronLink wallet on the website (button below; you'll re-enter the coin details there).")
    elif chain in EVM_TB_CHAINS and mode in ("plain", "bonding_curve"):
        text += ("\n\n<b>Two ways to pay:</b> connect your own wallet and sign it yourself (Ferzan never holds your "
                 f"keys), or launch straight from your Ferzan Trade Bot wallet (open @{_esc(TRADE)} → /wallet to see "
                 "its address and top it up). Nothing is sent until you tap one of the launch buttons.")
    else:
        text += ("\n\nNext you'll connect your wallet and see the exact cost before signing. "
                 "Ferzan never holds your keys.")
    if perk_note:
        text += "\n\n" + perk_note
    if ton_curve:
        text += ("\n\n<b>Two ways to pay:</b> connect Tonkeeper / Telegram Wallet, or launch straight from your "
                 "Ferzan Trade Bot wallet.\n" + _ton_wallet_text(tb_info) +
                 "\n\nNothing is sent until you tap one of the launch buttons.")
    elif chain == "ton" and mode == "plain":
        text += ("\n\n<b>Two ways to pay:</b> connect Tonkeeper / Telegram Wallet, or launch straight from your "
                 f"Ferzan Trade Bot wallet (it needs about {(_ton_need_nano() + 100_000_000) / 1e9:g} TON; "
                 f"your TON address is in @{_esc(TRADE)} → /wallet → TON).")
    first = [InlineKeyboardButton(launch_btn, callback_data="confirm:yes")]
    if chain != "tron":  # Tron launches run from the chat, so there is no reminder flow for them
        first.append(InlineKeyboardButton("⏰ Launch later", callback_data="confirm:later"))
    kb = [first]
    if chain == "tron":  # second way to pay: sign in TronLink on the website (the site already supports it)
        site = (os.environ.get("FERZAN_SITE_URL") or "https://ferzan-factory.com").rstrip("/")
        kind = "curve" if mode == "bonding_curve" else "pool"
        kb.append([InlineKeyboardButton("🔗 Launch with TronLink (website)", url=f"{site}/launch?chain=tron&kind={kind}")])
    if chain == "ton":
        first[0] = InlineKeyboardButton("🔗 Connect a wallet", callback_data="confirm:yes")
        kb.append([InlineKeyboardButton("💼 Launch from my Trade Bot wallet", callback_data="confirm:tb")])
    if chain in EVM_TB_CHAINS and mode in ("plain", "bonding_curve"):
        first[0] = InlineKeyboardButton("🔗 Connect a wallet", callback_data="confirm:yes")
        kb.append([InlineKeyboardButton("💼 Launch from my Trade Bot wallet", callback_data="confirm:tb")])
    await update.effective_message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb + [
            [InlineKeyboardButton("🔄 Start over", callback_data="confirm:restart"),
             InlineKeyboardButton("❌ Cancel", callback_data="confirm:no")],
        ]),
    )
    return CONFIRMING


def _ton_need_nano(mode: str = "plain") -> int:
    """What the launch sends: plain = contract+mint + admin drop + fee; curve = the same plus the curve's gas buffer."""
    base = 300_000_000 if mode != "bonding_curve" else tcv.CURVE_TON + tl.DEPLOY_TON + tl.ADMIN_TON
    return base + tl.launch_fee_nano()


def _ton_wallet_text(info: dict) -> str:
    if info.get("error") == "no_wallet":
        return f"(No Trade Bot wallet yet: open @{_esc(TRADE)} and tap /wallet to make one.)"
    if not info.get("ok"):
        return "(Couldn't read your Trade Bot TON wallet right now; it checks again when you tap.)"
    t = (f"Trade Bot TON wallet: <code>{_esc(info['address'])}</code>\n"
         f"Balance: <b>{info['balance_ton']:,.3f} TON</b> (needs about {info['need_ton']:g} TON; most of the "
         "contract part comes back as change)")
    if not info.get("enough"):
        t += f"\n⚠️ Send at least {info['need_ton'] - info['balance_ton']:,.3f} TON to that address to use it."
    return t


def _tron_wallet_text(info: dict) -> str:
    if info.get("error") == "no_wallet":
        return (f"⚠️ You don't have a Ferzan Trade Bot wallet yet. Open @{_esc(TRADE)}, tap /wallet, "
                "fund the TRON address it shows with TRX, then come back and tap Launch.")
    if not info.get("ok"):
        return "⚠️ Couldn't read your Trade Bot wallet right now. You can still tap Launch; it checks again first."
    t = (f"Your Trade Bot TRON wallet: <code>{_esc(info['address'])}</code>\n"
         f"Balance: <b>{info['balance_trx']:,.2f} TRX</b> (a launch needs about {info['need_trx']:g} TRX)")
    if not info.get("enough"):
        t += (f"\n\n⚠️ Not enough TRX yet. Send at least {info['need_trx'] - info['balance_trx']:,.2f} TRX "
              "to the address above (Tron network), then tap Launch.")
    else:
        t += "\n\nNothing is sent until you tap Launch. The whole supply goes to this wallet."
    return t


async def confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm:no":
        await query.edit_message_text("Cancelled — nothing was created. Tap /launch any time.")
        context.user_data.pop("launch", None)
        return ConversationHandler.END
    if query.data == "confirm:restart":
        context.user_data.pop("launch", None)
        await query.edit_message_text(_chain_text(), parse_mode="HTML", reply_markup=_kb(_chain_rows()))
        return CHOOSING_CHAIN

    launch = context.user_data.get("launch")
    if not launch:
        await query.edit_message_text("That launch expired — tap /launch to start again.")
        return ConversationHandler.END
    if query.data == "confirm:later":
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        return await _need_tz(update, context, "later")
    if launch["chain"] == "tron":
        return await _tron_go(update, context, launch)
    if query.data == "confirm:tb" and launch["chain"] == "ton":
        return await _ton_tb_go(update, context, launch)
    if query.data == "confirm:tb" and launch["chain"] in EVM_TB_CHAINS and launch.get("mode") in ("plain", "bonding_curve"):
        return await _evm_tb_go(update, context, launch)

    text, markup = _make_request(update.effective_user.id, update.effective_chat.id, launch)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    context.user_data.pop("launch", None)
    return ConversationHandler.END


# ------------------------------------------------ Tron: launch from the Trade Bot wallet --
LAUNCH_API = (os.environ.get("LAUNCH_API_URL") or "http://127.0.0.1:8000").rstrip("/")


async def _tron_go(update: Update, context: ContextTypes.DEFAULT_TYPE, launch: dict):
    q = update.callback_query
    uid, chat_id = update.effective_user.id, update.effective_chat.id
    req = db.create_launch_request(
        telegram_user_id=uid, chat_id=chat_id, chain="tron", mode=launch.get("mode") or "plain", name=launch["name"],
        symbol=launch["symbol"], total_supply=launch["total_supply_raw"], decimals=launch["decimals"],
        description=launch.get("description") or "", image_url=launch.get("image_url") or "",
        extra_params=dict(launch.get("extra_params") or {}, source="tradebot_wallet"),
    )
    context.user_data.pop("launch", None)
    await q.edit_message_text(
        f"⏳ Launching <b>{_esc(launch['name'])} (${_esc(launch['symbol'])})</b> on Tron from your Trade Bot "
        "wallet. This takes about a minute. Please don't launch it again.", parse_mode="HTML")
    context.application.create_task(_tron_run(context.bot, req.id, uid, chat_id, launch))
    return ConversationHandler.END


async def _ton_tb_go(update: Update, context: ContextTypes.DEFAULT_TYPE, launch: dict):
    q = update.callback_query
    uid, chat_id = update.effective_user.id, update.effective_chat.id
    req = db.create_launch_request(
        telegram_user_id=uid, chat_id=chat_id, chain="ton", mode=launch.get("mode") or "plain", name=launch["name"],
        symbol=launch["symbol"], total_supply=launch["total_supply_raw"], decimals=launch["decimals"],
        description=launch.get("description") or "", image_url=launch.get("image_url") or "",
        extra_params=dict(launch.get("extra_params") or {}, source="tradebot_wallet"),
    )
    context.user_data.pop("launch", None)
    await q.edit_message_text(
        f"⏳ Launching <b>{_esc(launch['name'])} (${_esc(launch['symbol'])})</b> on TON from your Trade Bot "
        "wallet. This takes 1-2 minutes. Please don't launch it again.", parse_mode="HTML")
    context.application.create_task(_ton_tb_run(context.bot, req.id, uid, chat_id, launch.get("mode") or "plain"))
    return ConversationHandler.END


def _build_tx(req_id: str, wallet: str) -> dict:
    import requests

    try:
        r = requests.post(f"{LAUNCH_API}/api/launch-requests/{req_id}/build-tx", json={"wallet_address": wallet},
                          timeout=90)
        d = r.json() or {}
        return d if r.status_code == 200 else {"error": str(d.get("detail") or r.status_code)[:200]}
    except Exception as e:  # noqa: BLE001
        return {"error": type(e).__name__}


async def _ton_tb_run(bot, req_id: str, uid: int, chat_id: int, mode: str = "plain"):
    say = lambda t: bot.send_message(chat_id=chat_id, text=t, parse_mode="HTML", disable_web_page_preview=True)  # noqa: E731
    helper = "ton_launch_exec.py"
    try:
        info = await tron.run("info", {"uid": uid, "need_nano": _ton_need_nano(mode)}, timeout=100, script=helper)
        if not info.get("ok"):
            db.update_status(req_id, "failed", error_message=str(info.get("error"))[:200])
            await say("❌ Couldn't use your Trade Bot TON wallet. " + _ton_wallet_text(info) + "\nNothing was sent.")
            return
        if not info.get("enough"):
            db.update_status(req_id, "failed", error_message="low balance")
            await say("❌ Not launched. " + _ton_wallet_text(info) + "\nNothing was sent.")
            return
        built = await asyncio.to_thread(_build_tx, req_id, info["address"])
        if not built.get("messages"):
            await say("❌ Couldn't prepare the TON launch (" + _esc(built.get("error", "no messages")) + "). Nothing was sent.")
            return
        res = await tron.run("launch", {"uid": uid, "request_id": req_id, "messages": built["messages"]},
                             timeout=420, script=helper)
        txid = res.get("txid") or ""
        if res.get("ok") or res.get("pending"):
            if txid:
                db.update_status(req_id, "submitted", tx_hash=txid)
            await say("✅ Your TON launch was sent. Confirming it on-chain now (up to a few minutes). "
                      "Please don't launch it again.")
            why, t0 = "", time.monotonic()
            while time.monotonic() - t0 < 360:  # the API proves the coin on-chain (supply + no admin), then posts the card
                ok, why = await asyncio.to_thread(_tron_complete, req_id, txid)
                if ok:
                    return
                await asyncio.sleep(20)
            logger.warning("ton tb launch %s not recorded: %s", req_id, why)
            await say("⏳ Your TON launch was sent but isn't confirmed yet. Check your Trade Bot wallet on "
                      f"https://tonviewer.com/{_esc(info['address'])} and please don't launch it again.")
            return
        db.update_status(req_id, "failed", error_message=str(res.get("error"))[:200])
        if res.get("error") == "low_balance":
            await say("❌ Not launched: not enough TON in your Trade Bot wallet. Nothing was sent.")
        else:
            await say(f"❌ The TON launch didn't go through ({_esc(str(res.get('error'))[:150])}). Check "
                      f"https://tonviewer.com/{_esc(info['address'])} before trying again.")
    except Exception:
        logger.exception("ton tb launch %s crashed", req_id)
        await say("❌ Something went wrong with the TON launch. Check /history before trying again.")


# ------------------------------------------------ EVM: launch from the Trade Bot wallet --
EVM_TB_CHAINS = {"base", "bsc", "ethereum", "robinhood"}  # Arc is not wired to the Trade Bot wallet yet
_EVM_EXPLORER = {"base": "https://basescan.org", "bsc": "https://bscscan.com", "ethereum": "https://etherscan.io",
                 "robinhood": "https://explorer.robinhood.com"}
_EVM_NAME = {"base": "Base", "bsc": "BNB Chain", "ethereum": "Ethereum", "robinhood": "Robinhood Chain"}


async def _evm_tb_go(update: Update, context: ContextTypes.DEFAULT_TYPE, launch: dict):
    q = update.callback_query
    uid, chat_id = update.effective_user.id, update.effective_chat.id
    chain = launch["chain"]
    req = db.create_launch_request(
        telegram_user_id=uid, chat_id=chat_id, chain=chain, mode=launch.get("mode") or "plain", name=launch["name"],
        symbol=launch["symbol"], total_supply=launch["total_supply_raw"], decimals=launch["decimals"],
        description=launch.get("description") or "", image_url=launch.get("image_url") or "",
        extra_params=dict(launch.get("extra_params") or {}, source="tradebot_wallet"),
    )
    context.user_data.pop("launch", None)
    await q.edit_message_text(
        f"⏳ Launching <b>{_esc(launch['name'])} (${_esc(launch['symbol'])})</b> on {_esc(_EVM_NAME.get(chain, chain))} "
        "from your Trade Bot wallet. This takes about a minute. Please don't launch it again.", parse_mode="HTML")
    context.application.create_task(_evm_tb_run(context.bot, req.id, uid, chat_id, chain))
    return ConversationHandler.END


async def _evm_tb_run(bot, req_id: str, uid: int, chat_id: int, chain: str):
    say = lambda t: bot.send_message(chat_id=chat_id, text=t, parse_mode="HTML", disable_web_page_preview=True)  # noqa: E731
    helper, scan = "evm_launch_exec.py", _EVM_EXPLORER.get(chain, "")
    addr = ""
    try:
        info = await tron.run("info", {"uid": uid, "chain": chain}, timeout=60, script=helper)
        addr = info.get("address") or ""
        if not info.get("ok"):
            db.update_status(req_id, "failed", error_message=str(info.get("error"))[:200])
            if info.get("error") == "no_wallet":
                await say(f"❌ You don't have a Ferzan Trade Bot wallet yet. Open @{_esc(TRADE)}, tap /wallet, then try again. Nothing was sent.")
            else:
                await say(f"❌ Couldn't use your Trade Bot wallet ({_esc(str(info.get('error'))[:120])}). Nothing was sent.")
            return
        built = await asyncio.to_thread(_build_tx, req_id, addr)
        tx = built.get("unsigned_transaction")
        if not isinstance(tx, dict) or not built.get("factory"):
            db.update_status(req_id, "failed", error_message=str(built.get("error") or "no transaction")[:200])
            await say("❌ Couldn't prepare the launch (" + _esc(str(built.get("error") or "no transaction")[:200]) +
                      "). Nothing was sent.")
            return
        short = {"ethereum": "ETH", "bsc": "BSC", "base": "BASE", "robinhood": "HOOD"}[chain]
        allowed = [v for v in ((os.environ.get(f"FACTORY_{short}_PLAIN") or "").strip(),
                               (os.environ.get(f"FACTORY_{short}_CURVE") or "").strip()) if v]
        largs = {"uid": uid, "chain": chain, "request_id": req_id, "factory": built["factory"], "tx": tx,
                 "allowed_factories": allowed}  # pinned from the Launch Bot's own settings, not just the build reply
        res = await tron.run("launch", largs, timeout=240, script=helper)
        txh = res.get("txhash") or ""
        t1 = time.monotonic()
        warned = False
        while res.get("pending") and txh and time.monotonic() - t1 < 1800:
            # sent but not mined yet: ask the helper again (it never re-sends; it only re-reads the receipt)
            if not warned and time.monotonic() - t1 > 240:
                warned = True
                await say("⏳ The network is slow. Your launch is still waiting to be confirmed. I'll keep watching it; "
                          "please don't launch it again.")
            await asyncio.sleep(10)
            res = await tron.run("launch", largs, timeout=60, script=helper)
            txh = res.get("txhash") or txh
        if res.get("ok"):
            db.update_status(req_id, "submitted", tx_hash=txh)
            await say("✅ Your launch was sent. Confirming it on-chain now (up to a few minutes). Please don't launch it again.")
            why, t0 = "", time.monotonic()
            while time.monotonic() - t0 < 360:  # the API proves the launch on-chain, then posts the card
                ok, why = await asyncio.to_thread(_tron_complete, req_id, txh)
                if ok:
                    return
                await asyncio.sleep(20)
            logger.warning("evm tb launch %s not recorded: %s", req_id, why)
            await say("⏳ Your launch was sent but isn't confirmed yet. Check your Trade Bot wallet on "
                      f"{_esc(scan)}/address/{_esc(addr)} and please don't launch it again.")
            return
        if txh and not res.get("ok") and "reverted" not in str(res.get("error") or ""):
            # a tx hash exists and the chain has not said it failed: it may still mine, so never call it a failure
            db.update_status(req_id, "submitted", tx_hash=txh)
            await say("⏳ Your launch was sent but the network hasn't confirmed it yet. See "
                      f"{_esc(scan)}/tx/{_esc(txh)} and please don't launch it again.")
            return
        if res.get("pending"):  # the helper outlived its timeout: it may have sent, so never call it a clean failure
            await say("⏳ The launch is still being sent and I can't tell yet whether it went through. Check your Trade Bot "
                      f"wallet on {_esc(scan)}/address/{_esc(addr)} and please don't launch it again.")
            return
        if res.get("maybe_sent"):  # the network's answer was unclear: never say "nothing was sent"
            db.update_status(req_id, "submitted", tx_hash=txh)
            await say("⚠️ I'm not sure whether your launch went out. Check your Trade Bot wallet on "
                      f"{_esc(scan)}/address/{_esc(addr)} and please don't launch it again until you've looked.")
            return
        db.update_status(req_id, "failed", error_message=str(res.get("error"))[:200])
        if res.get("error") == "low_balance":
            sym = res.get("symbol") or "ETH"
            await say(f"❌ Not launched: your Trade Bot wallet has {float(res.get('balance') or 0):.5g} {_esc(sym)} but this "
                      f"launch needs about {float(res.get('need') or 0):.5g} {_esc(sym)}. Top it up and try again. Nothing was sent.")
        elif txh:
            await say(f"❌ The launch didn't go through ({_esc(str(res.get('error'))[:150])}). See "
                      f"{_esc(scan)}/tx/{_esc(txh)} before trying again.")
        else:
            await say(f"❌ Not launched ({_esc(str(res.get('error'))[:150])}). Check "
                      f"{_esc(scan)}/address/{_esc(addr)} before trying again.")
    except Exception:
        logger.exception("evm tb launch %s crashed", req_id)
        await say("❌ Something went wrong with the launch. Check /history and your Trade Bot wallet before trying again.")


def _tron_complete(req_id: str, txid: str) -> tuple[bool, str]:
    import requests

    try:
        r = requests.post(f"{LAUNCH_API}/api/launch-requests/{req_id}/complete", json={"tx_hash": txid}, timeout=120)
        if r.status_code == 200:
            return True, ""
        return False, str((r.json() or {}).get("detail") or r.status_code)[:200]
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


async def _tron_run(bot, req_id: str, uid: int, chat_id: int, launch: dict):
    say = lambda t: bot.send_message(chat_id=chat_id, text=t, parse_mode="HTML", disable_web_page_preview=True)  # noqa: E731
    try:
        if launch.get("mode") == "bonding_curve":
            ex = launch.get("extra_params") or {}
            start = int(ex.get("start_at") or 0) or (int(time.time()) + int(ex.get("start_minutes") or 0) * 60
                                                     if int(ex.get("start_minutes") or 0) else 0)
            res = await tron.run("curve", {
                "uid": uid, "request_id": req_id, "name": launch["name"], "symbol": launch["symbol"],
                "supply_raw": launch["total_supply_raw"], "grad_sun": ex.get("graduation_eth_threshold"),
                "start_time": start, "max_buy_sun": int(float(ex.get("max_buy") or 0) * 1e6),
                "dev_buy_sun": int(float(ex.get("dev_buy") or 0) * 1e6)})
        else:
            res = await tron.run("launch", {"uid": uid, "request_id": req_id, "name": launch["name"],
                                            "symbol": launch["symbol"], "supply_raw": launch["total_supply_raw"]})
        txid, addr = res.get("txid") or "", res.get("address") or ""
        if txid and addr:
            db.update_status(req_id, "submitted", wallet_address=addr, tx_hash=txid)
        if res.get("ok") or (txid and res.get("pending")):
            for attempt in range(6):  # the API re-checks the launch on-chain, then posts the launch card
                ok, why = await asyncio.to_thread(_tron_complete, req_id, txid)
                if ok:
                    if launch.get("mode") != "bonding_curve":
                        try:  # a plain coin has no pool until its creator adds one: offer it right away
                            await bot.send_message(
                                chat_id=chat_id, parse_mode="HTML",
                                text=("💧 <b>One more step: open trading.</b>\nYour coin is live but has no pool yet, so nobody "
                                      "can buy or sell it. Add a SunSwap pool from your Trade Bot wallet (you pick how much "
                                      "TRX goes in; you can lock the liquidity forever)."),
                                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💧 Open the pool now", callback_data=f"lp:go:{req_id}")]]))
                        except Exception:  # noqa: BLE001
                            logger.exception("pool prompt failed for %s", req_id)
                    return
                await asyncio.sleep(30)
            logger.warning("tron launch %s not recorded: %s", req_id, why)
            await say(f"Your Tron launch was sent but isn't confirmed yet. Check it here and don't launch again:\n"
                      f"https://tronscan.org/#/transaction/{_esc(txid)}")
            return
        err = res.get("error") or "unknown error"
        if res.get("pending"):  # the helper is still waiting on Tron: it may have sent, so never say "nothing sent"
            await say("⏳ Tron is slow right now. Your launch may still land: check /history in a few minutes "
                      "and please don't launch it again.")
            return
        db.update_status(req_id, "failed", error_message=str(err)[:200])
        if err == "low_balance":
            await say(f"❌ Not launched: your Trade Bot TRON wallet has {res.get('balance_trx', 0):,.2f} TRX and a launch "
                      f"needs about {res.get('need_trx', 0):g} TRX.\nSend TRX (Tron network) to "
                      f"<code>{_esc(res.get('address', ''))}</code>, then /launch again. Nothing was spent.")
        elif err == "no_wallet":
            await say(f"❌ Not launched: you don't have a Ferzan Trade Bot wallet yet. Open @{_esc(TRADE)}, tap /wallet "
                      "and fund its TRON address, then /launch again.")
        else:
            extra = f"\nTransaction: https://tronscan.org/#/transaction/{_esc(txid)}" if txid else " Nothing was sent."
            await say(f"❌ The Tron launch didn't go through ({_esc(str(err)[:150])}).{extra}")
    except Exception:
        logger.exception("tron launch %s crashed", req_id)
        await say("❌ Something went wrong with the Tron launch. Check /history before trying again.")


def _make_request(user_id: int, chat_id: int, launch: dict, note: str = ""):
    """Create the launch request -> (message html, keyboard) with the wallet button."""
    extra = {k: v for k, v in (launch.get("extra_params") or {}).items()}
    if extra.get("start_at") and int(extra["start_at"]) <= time.time() + 60:
        extra.pop("start_at", None)  # opening time already passed -> opens at launch
        extra["start_minutes"] = "0"
        extra["start_display"] = "right away"
        note += "\nThe trading-open time you picked has passed, so trading opens right at launch."
    req = db.create_launch_request(
        telegram_user_id=user_id,
        chat_id=chat_id,
        chain=launch["chain"],
        mode=launch["mode"],
        name=launch["name"],
        symbol=launch["symbol"],
        total_supply=launch["total_supply_raw"],
        decimals=launch["decimals"],
        description=launch.get("description") or "",
        image_url=launch.get("image_url") or "",
        extra_params=extra,
    )
    live = MINI_APP_BASE_URL.startswith("https://") and "yourdomain.com" not in MINI_APP_BASE_URL
    if not live:
        return (f"✅ Request saved: {_esc(req.name)} ({_esc(req.symbol)}) on {_esc(req.chain)}\n"
                f"ID: <code>{_esc(req.id)}</code>\n\nWallet signing is not live yet (no HTTPS Mini App)."), None
    page = {"solana": "solana.html", "ton": "ton.html"}.get(launch["chain"], "evm.html")
    mini_app_url = f"{MINI_APP_BASE_URL}/{page}?request_id={req.id}"
    text = ((f"<b>{_esc(launch['name'])} (${_esc(launch['symbol'])})</b>\n" if note else "")
            + "Tap below to connect your wallet and review the exact transaction before signing. "
            "Nothing is sent until you approve it in your own wallet." + _esc(note))
    return text, InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔗 Connect Wallet & Launch", web_app=WebAppInfo(url=mini_app_url))]]
    )


# -------------------------------------------------------- launch later --
LATER_PRESETS = [("In 1 hour", 60), ("In 6 hours", 360), ("Tomorrow, same time", 1440)]


async def _ask_later(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = lx.get_tz(update.effective_user.id) or "UTC"
    await _send(
        update,
        "<b>⏰ Launch later</b>\n\nWhen should I remind you to launch? I'll message you at that time "
        "with a button to sign in your wallet (the bot never signs for you).\n\n"
        f"Tap one, or type a time in <b>{_esc(tz)}</b> time: <code>8pm</code> · <code>sat 7pm</code> · "
        "<code>10/3 6pm</code> · <code>in 3h</code>",
        [[InlineKeyboardButton(lbl, callback_data=f"later:{m}") for lbl, m in LATER_PRESETS[:2]],
         [InlineKeyboardButton(LATER_PRESETS[2][0], callback_data=f"later:{LATER_PRESETS[2][1]}")]],
    )
    return ENTERING_LATER


async def _save_later(update: Update, context: ContextTypes.DEFAULT_TYPE, run_at: int):
    launch = context.user_data.get("launch")
    if not launch:
        return ConversationHandler.END
    now = time.time()
    if run_at < now + 120:
        await update.effective_message.reply_text("Pick a time at least 2 minutes from now.")
        return ENTERING_LATER
    if run_at > now + MAX_DRAFT_AHEAD:
        await update.effective_message.reply_text("You can schedule up to 30 days ahead.")
        return ENTERING_LATER
    extra = launch.get("extra_params") or {}
    note = ""
    if extra.get("start_at") and int(extra["start_at"]) < run_at:
        note = ("\n\n⚠️ The trading-open time you picked is before this launch time, "
                "so trading will open right at launch.")
    did = lx.save_draft(update.effective_user.id, update.effective_chat.id, launch, run_at)
    tz = lx.get_tz(update.effective_user.id)
    await update.effective_message.reply_text(
        f"✅ <b>Scheduled.</b> {_esc(launch['name'])} (${_esc(launch['symbol'])})\n"
        f"I'll message you <b>{_esc(lx.fmt_when(run_at, tz))}</b> with a button to launch.{note}\n\n"
        "See or change it any time with /drafts.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📋 My drafts", callback_data="dr:list")]]),
    )
    context.user_data.pop("launch", None)
    logger.info("draft %s scheduled for %s", did, run_at)
    return ConversationHandler.END


async def later_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _tap(update)
    mins = int(update.callback_query.data.split(":", 1)[1])
    return await _save_later(update, context, int(time.time()) + mins * 60)


async def later_entered(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tz = lx.get_tz(update.effective_user.id) or "UTC"
    when, err = lx.parse_when(update.message.text or "", tz)
    if not when:
        await update.message.reply_text(err)
        return ENTERING_LATER
    return await _save_later(update, context, int(when.timestamp()))


# ---------------------------------------------------------------- drafts --
def _draft_line(d: dict, tz: str | None) -> str:
    p = d["payload"]
    kind = {"plain": "standard", "meteora": "Meteora curve", "bonding_curve": "curve"}.get(p.get("mode"), p.get("mode"))
    return (f"<b>{_esc(p.get('name'))} (${_esc(p.get('symbol'))})</b> · {_esc(CHAINS.get(p.get('chain'), p.get('chain')))}"
            f" {_esc(kind)}\n⏰ {_esc(lx.fmt_when(d['run_at'], tz))}"
            + ("  <i>(reminder sent)</i>" if d["status"] == "reminded" else ""))


def _draft_kb(did: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Launch now", callback_data=f"dr:go:{did}"),
         InlineKeyboardButton("🕐 Change time", callback_data=f"dr:time:{did}")],
        [InlineKeyboardButton("🗑 Delete", callback_data=f"dr:del:{did}")],
    ])


async def drafts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    drafts = lx.list_drafts(uid)
    msg = update.effective_message
    if not drafts:
        await msg.reply_text("No scheduled launches. On the review screen tap ⏰ Launch later to schedule one.")
        return
    tz = lx.get_tz(uid)
    await msg.reply_text(f"<b>Your scheduled launches ({len(drafts)})</b>", parse_mode="HTML")
    for d in drafts[:10]:
        await msg.reply_text(_draft_line(d, tz), parse_mode="HTML", reply_markup=_draft_kb(d["id"]))


async def drafts_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    action = parts[1]
    if action == "list":
        await q.answer()
        return await drafts_cmd(update, context)
    did = parts[2] if len(parts) > 2 else ""
    d = lx.get_draft(did)
    if not d or d["user_id"] != update.effective_user.id or d["status"] not in ("scheduled", "reminded"):
        await q.answer("That draft is gone.", show_alert=True)
        return
    tz = lx.get_tz(update.effective_user.id)
    if action == "go":
        await q.answer()
        lx.set_draft_status(did, "done")
        text, markup = _make_request(update.effective_user.id, update.effective_chat.id, d["payload"], note=" ")
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    elif action == "del":
        lx.set_draft_status(did, "deleted")
        await q.answer("Deleted")
        await q.edit_message_text(f"🗑 Deleted: {_esc(d['payload'].get('name'))}", parse_mode="HTML")
    elif action == "snz":
        lx.reschedule_draft(did, int(time.time()) + 3600)
        await q.answer("I'll remind you in 1 hour")
        await q.edit_message_text(_draft_line(lx.get_draft(did), tz), parse_mode="HTML", reply_markup=_draft_kb(did))
    elif action == "time":
        await q.answer()
        context.user_data["resched"] = did
        await q.message.reply_text(
            f"New time for <b>{_esc(d['payload'].get('name'))}</b>? Tap one or type a time "
            f"({_esc(tz or 'UTC')}): <code>8pm</code> · <code>sat 7pm</code> · <code>in 3h</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("In 1 hour", callback_data=f"dr:rs:{did}:60"),
                 InlineKeyboardButton("In 6 hours", callback_data=f"dr:rs:{did}:360")],
                [InlineKeyboardButton("Tomorrow, same time", callback_data=f"dr:rs:{did}:1440")],
            ]),
        )
    elif action == "rs":
        mins = int(parts[3])
        context.user_data.pop("resched", None)
        lx.reschedule_draft(did, int(time.time()) + mins * 60)
        await q.answer("Rescheduled")
        await q.edit_message_text("✅ " + _draft_line(lx.get_draft(did), tz), parse_mode="HTML",
                                  reply_markup=_draft_kb(did))


async def resched_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    did = context.user_data.get("resched")
    if not did:
        return
    d = lx.get_draft(did)
    if not d or d["user_id"] != update.effective_user.id:
        context.user_data.pop("resched", None)
        return
    tz = lx.get_tz(update.effective_user.id) or "UTC"
    when, err = lx.parse_when(update.message.text or "", tz)
    if not when:
        await update.message.reply_text(err)
        return
    ts = int(when.timestamp())
    if ts < time.time() + 120 or ts > time.time() + MAX_DRAFT_AHEAD:
        await update.message.reply_text("Pick a time between 2 minutes and 30 days from now.")
        return
    context.user_data.pop("resched", None)
    lx.reschedule_draft(did, ts)
    await update.message.reply_text("✅ " + _draft_line(lx.get_draft(did), tz), parse_mode="HTML",
                                    reply_markup=_draft_kb(did))


async def _draft_loop(application: Application):
    """Every 30s: send 'time to launch' reminders for drafts that are due."""
    while True:
        try:
            for d in lx.claim_due_drafts():
                p = d["payload"]
                try:
                    await application.bot.send_message(
                        chat_id=d["chat_id"],
                        text=(f"🚀 <b>Time to launch {_esc(p.get('name'))} (${_esc(p.get('symbol'))})!</b>\n"
                              f"{_esc(CHAINS.get(p.get('chain'), p.get('chain')))} · tap Launch now, then sign in your wallet."),
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("🚀 Launch now", callback_data=f"dr:go:{d['id']}")],
                            [InlineKeyboardButton("⏰ Remind me in 1 hour", callback_data=f"dr:snz:{d['id']}"),
                             InlineKeyboardButton("🗑 Delete", callback_data=f"dr:del:{d['id']}")],
                        ]),
                    )
                except Exception:
                    logger.exception("draft reminder failed %s", d["id"])
        except Exception:
            logger.exception("draft loop error")
        await asyncio.sleep(30)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("launch", None)
    await update.effective_message.reply_text("Cancelled — nothing was created. Tap /launch any time.")
    return ConversationHandler.END


async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Cancelled")
    context.user_data.pop("launch", None)
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await q.message.reply_text("Cancelled — nothing was created. Tap /launch any time.")
    return ConversationHandler.END


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    launches = db.get_user_launch_history(update.effective_user.id)
    target = update.effective_message
    if not launches:
        await target.reply_text("No launches yet — try /launch.")
        return
    lines = ["<b>Your launches:</b>"]
    for row in launches:
        status_emoji = {"confirmed": "✅", "failed": "❌", "pending": "⏳", "built": "⏳"}.get(row.status, "•")
        lines.append(f"{status_emoji} {_esc(row.name)} ({_esc(row.symbol)}) on {_esc(CHAINS.get(row.chain, row.chain))} — {_esc(row.status)}")
    await target.reply_text("\n".join(lines), parse_mode="HTML")


def _admin_ids() -> set:
    raw = (os.environ.get("FERZAN_ADMIN_IDS") or "") + "," + (os.environ.get("ADMIN_TELEGRAM_ID") or "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}


def _page_button(label: str, url: str, private: bool) -> InlineKeyboardButton:
    # Mini App buttons only work in private chats; groups get a normal link.
    return InlineKeyboardButton(label, web_app=WebAppInfo(url=url)) if private else InlineKeyboardButton(label, url=url)


async def launchday_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admins only, and silent for everyone else: one screen of what is ready for launch day."""
    if not update.effective_user or update.effective_user.id not in _admin_ids():
        return
    import launch_day

    text = await asyncio.to_thread(launch_day.report)
    await update.effective_message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def claim_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    private = bool(chat and chat.type == "private")
    rows = [[_page_button("💰 Claim my Solana fees", f"{MINI_APP_BASE_URL}/claim.html?role=creator", private)]]
    if update.effective_user and update.effective_user.id in _admin_ids():
        rows.append([_page_button("🏦 Claim platform fees", f"{MINI_APP_BASE_URL}/claim.html?role=partner", private)])
    await update.effective_message.reply_text(
        "💰 <b>Your trading fees</b>\n\n"
        "<b>Solana (Meteora) tokens:</b> you earn half of the 1% fee on every trade. "
        "It collects in the pool until you claim it — tap below and sign with the wallet you launched from.\n\n"
        "<b>BNB and Base curve tokens:</b> your half is sent to your wallet automatically on every trade. "
        "Nothing to claim.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def feed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    private = bool(chat and chat.type == "private")
    base = f"{MINI_APP_BASE_URL}/launches.html"
    rows = [
        [_page_button("🆕 New launches", f"{base}?sort=new", private)],
        [_page_button("👑 King of the Hill", f"{base}?sort=koth", private),
         _page_button("📈 Top volume", f"{base}?sort=volume", private)],
        [_page_button("🔥 Trending now", f"{base}?sort=trending", private)],
        [_page_button("🏆 Top creators", f"{MINI_APP_BASE_URL}/leaderboard.html", private)],
    ]
    await update.effective_message.reply_text(
        "🚀 <b>Ferzan launches</b>\n\n"
        "🆕 <b>New</b>: the latest tokens on every chain.\n"
        "👑 <b>King of the Hill</b>: the curves closest to graduating.\n"
        "📈 <b>Top volume</b>: most traded in the last 24 hours.\n"
        "🔥 <b>Trending</b>: the most action in the last hour.\n\n"
        "Every token shows its creator's track record.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def revenue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id not in _admin_ids():
        return  # admins only; stay silent for everyone else
    msg = await update.effective_message.reply_text("⏳ Adding up platform revenue…")

    def fetch():
        import json as _json
        import urllib.request as _ur
        req = _ur.Request("http://127.0.0.1:8000/internal/revenue",
                          headers={"X-Ferzan-Internal": (os.environ.get("INTERNAL_API_TOKEN") or "").strip()})
        with _ur.urlopen(req, timeout=120) as r:
            return _json.loads(r.read())

    try:
        d = await asyncio.to_thread(fetch)
    except Exception as e:
        await msg.edit_text(f"Couldn't load revenue: {e}")
        return
    usd = lambda v: f"${v:,.2f}" if v < 1000 else f"${v:,.0f}"
    t, s = d["totals_usd"], d["by_source_usd"]
    sym = {"bsc": "BNB", "base": "ETH", "ethereum": "ETH", "robinhood": "ETH", "solana": "SOL"}
    name = {"bsc": "BNB", "base": "Base", "ethereum": "Ethereum", "robinhood": "Robinhood", "solana": "Solana"}
    lines = [
        "💰 <b>Ferzan platform revenue</b>", "",
        f"24h <b>{usd(t['24h'])}</b> · 7d <b>{usd(t['7d'])}</b> · 30d <b>{usd(t['30d'])}</b>",
        f"All-time <b>{usd(t['all'])}</b>", "",
        "<b>Last 30 days by source</b>",
        f"📈 Curve trading fees: {usd(s['30d']['curve'])}",
        f"🚀 Launch fees: {usd(s['30d']['launch'])} ({d['launches_30d']} launches)",
        f"⚡ Trade Bot ({d['desk_fee_bps'] / 100:.2f}% of {usd(d['desk_volume_usd']['30d'])} volume): {usd(s['30d']['desk'])}",
    ]
    bc = d.get("by_chain_30d_native") or {}
    if bc:
        lines += ["", "<b>30 days by chain</b> (curve + launch fees)"]
        for ch, v in sorted(bc.items(), key=lambda kv: -(kv[1]["curve"] + kv[1]["launch"]) * d["native_usd"].get(kv[0], 0)):
            tot = v["curve"] + v["launch"]
            lines.append(f"• {name.get(ch, ch)}: {tot:.4g} {sym.get(ch, '')} ({usd(tot * d['native_usd'].get(ch, 0))})")
    if d.get("unclaimed_sol") is not None:
        lines += ["", f"🪐 Solana trading fees waiting to be claimed: <b>{d['unclaimed_sol']:.4f} SOL</b> ({usd(d['unclaimed_sol_usd'])})"]
    bal = {k: v for k, v in (d.get("treasury_balances") or {}).items() if v is not None}
    if bal:
        lines += ["", "<b>Treasury wallets hold</b>"]
        lines.append(" · ".join(f"{name.get(k, k)} {v:.4g} {sym.get(k, '')}" for k, v in bal.items()))
    if d.get("estimated"):
        lines += ["", "<i>Curve fees for trades before this update are estimated from the 1% fee rule; new trades are exact.</i>"]
    kb = InlineKeyboardMarkup([[_page_button("🏦 Claim Solana platform fees", f"{MINI_APP_BASE_URL}/claim.html?role=partner",
                                             bool(update.effective_chat and update.effective_chat.type == "private"))]])
    await msg.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    private = bool(chat and chat.type == "private")
    await update.effective_message.reply_text(
        "🏆 <b>Top creators</b>\n\nRanked by tokens graduated, then trading volume on their curves. "
        "Launch, build a community, graduate - and climb.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[_page_button("🏆 Open the leaderboard", f"{MINI_APP_BASE_URL}/leaderboard.html", private)]]),
    )


async def go_feed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await feed_cmd(update, context)


async def go_claim(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await claim_cmd(update, context)


async def go_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await history(update, context)


async def refer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    me = (context.bot.username or os.environ.get("LAUNCHBOT_USERNAME") or "Ferzan_Launch_Bot").lstrip("@")
    uid = update.effective_user.id

    def fetch():
        import json as _json
        import urllib.request as _ur
        req = _ur.Request(f"http://127.0.0.1:8000/internal/referral-stats/{uid}",
                          headers={"X-Ferzan-Internal": (os.environ.get("INTERNAL_API_TOKEN") or "").strip()})
        with _ur.urlopen(req, timeout=20) as r:
            return _json.loads(r.read())

    try:
        st = await asyncio.to_thread(fetch)
    except Exception:
        st = {}
    wallet = st.get("wallet") or ""
    lines = [
        "🤝 <b>Refer & earn</b>", "",
        f"Your link: https://t.me/{me}?start=ref_{uid}", "",
        "Everyone who starts Ferzan through your link is yours for good. Whenever they buy a Ferzan "
        "curve token with @Ferzan_Trade_Bot, the contract sends <b>10% of the trading fee straight to your wallet</b> "
        "— instantly, on-chain, no claiming.", "",
        "On a token's trade page, connect your wallet and tap <b>Share & earn</b> to get a link that pays you "
        "for anyone who buys through it.", "",
    ]
    if wallet:
        lines.append(f"💳 Payout wallet: <code>{html.escape(wallet)}</code> (change: /referwallet 0x…)")
    else:
        lines.append("💳 No payout wallet yet — set one: <code>/referwallet 0xYourAddress</code>")
    if st:
        lines += ["", f"👥 People you referred: <b>{st.get('referred', 0)}</b> · their launches: <b>{st.get('referred_launches', 0)}</b>"]
        ch = st.get("chains") or {}
        if ch:
            lines.append(f"💰 Earned so far: <b>${st.get('earned_usd', 0):,.2f}</b>")
            for k, v in ch.items():
                lines.append(f"• {k}: {v['earned']:.5g} {v['sym']} from {v['trades']} trades")
        else:
            lines.append("💰 No referral trades yet.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)


async def referwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw = " ".join(context.args or []).strip()
    if not (raw.startswith("0x") and len(raw) == 42):
        await update.effective_message.reply_text("Usage: /referwallet 0xYourEvmAddress")
        return
    db.set_payout_wallet(update.effective_user.id, raw)
    await update.effective_message.reply_text(f"Payout wallet set:\n`{raw}`", parse_mode="Markdown")


async def lplock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🔒 *LP lock helper*\n\n"
        "Meteora/curve mode: pool creation and LP burn are currently *manual* — "
        "after minting, create your pool via the Meteora dashboard, send the LP "
        "tokens to `0x000000000000000000000000000000000000dead`, then post the burn tx.\n\n"
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
    lx.init_tables()
    app = Application.builder().token(token).build()

    text = filters.TEXT & ~filters.COMMAND
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("launch", launch_start),
            CallbackQueryHandler(go_launch, pattern="^go:launch$"),
        ],
        states={
            CHOOSING_CHAIN: [CallbackQueryHandler(chain_chosen, pattern="^chain:")],
            CHOOSING_MODE: [CallbackQueryHandler(mode_chosen, pattern="^mode:"),
                            CallbackQueryHandler(back_to_chains, pattern="^lx:chains$")],
            ENTERING_NAME: [MessageHandler(text, name_entered)],
            ENTERING_SYMBOL: [MessageHandler(text, symbol_entered), CallbackQueryHandler(symbol_cb, pattern="^sym:")],
            ENTERING_LOGO: [MessageHandler(filters.PHOTO, logo_photo),
                            MessageHandler(filters.Document.IMAGE, logo_document),
                            MessageHandler(text, logo_text), CallbackQueryHandler(logo_skip_cb, pattern="^logo:")],
            ENTERING_INFO: [MessageHandler(text, info_entered), CallbackQueryHandler(info_skip_cb, pattern="^info:")],
            CHOOSING_TZ: [CallbackQueryHandler(tz_cb, pattern="^tz:"), MessageHandler(text, tz_entered)],
            ENTERING_OPEN_AT: [MessageHandler(text, open_at_entered)],
            ENTERING_LATER: [CallbackQueryHandler(later_cb, pattern="^later:"), MessageHandler(text, later_entered)],
            ENTERING_SUPPLY: [MessageHandler(text, supply_entered), CallbackQueryHandler(supply_cb, pattern="^sup:")],
            ENTERING_GRAD: [MessageHandler(text, grad_entered), CallbackQueryHandler(grad_cb, pattern="^grad:")],
            ENTERING_ALLOCS: [MessageHandler(text, allocs_entered), CallbackQueryHandler(allocs_skip_cb, pattern="^allocs:")],
            ENTERING_DEVBUY: [MessageHandler(text, devbuy_entered), CallbackQueryHandler(devbuy_cb, pattern="^devbuy:")],
            ENTERING_WINDOW: [MessageHandler(text, window_entered), CallbackQueryHandler(window_cb, pattern="^window:")],
            ENTERING_MAXBUY: [MessageHandler(text, maxbuy_entered), CallbackQueryHandler(maxbuy_cb, pattern="^maxbuy:")],
            CONFIRMING: [CallbackQueryHandler(confirmed, pattern="^confirm:")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel_cb, pattern="^lx:cancel$"),
            CommandHandler("launch", launch_start),
            CallbackQueryHandler(go_launch, pattern="^go:launch$"),
        ],
        allow_reentry=True,
        conversation_timeout=30 * 60,
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(conv)
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("following", following_cmd))
    app.add_handler(CommandHandler("tiers", tiers_cmd))
    app.add_handler(CallbackQueryHandler(go_tiers, pattern="^go:tiers$"))
    app.add_handler(CommandHandler("alerts", alerts_cmd))
    app.add_handler(CallbackQueryHandler(watch_cb, pattern="^w[adl]:"))
    app.add_handler(CallbackQueryHandler(following_cb, pattern="^fol:"))
    app.add_handler(CommandHandler("refer", refer_cmd))
    app.add_handler(CommandHandler("referwallet", referwallet_cmd))
    app.add_handler(CommandHandler("lplock", lplock_cmd))
    app.add_handler(CallbackQueryHandler(go_history, pattern="^go:history$"))
    app.add_handler(CommandHandler("drafts", drafts_cmd))
    app.add_handler(CommandHandler("claim", claim_cmd))
    app.add_handler(CommandHandler("launchday", launchday_cmd))
    app.add_handler(CommandHandler("new", feed_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("revenue", revenue_cmd))
    app.add_handler(CallbackQueryHandler(go_feed, pattern="^go:feed$"))
    app.add_handler(CallbackQueryHandler(go_claim, pattern="^go:claim$"))
    app.add_handler(CommandHandler("timezone", timezone_cmd))
    app.add_handler(CallbackQueryHandler(drafts_cb, pattern="^dr:"))
    app.add_handler(CallbackQueryHandler(tzset_cb, pattern="^tzset:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, resched_text), group=1)
    tron_liquidity.register(app)

    async def _post(application):
        await application.bot.set_my_commands(
            [
                BotCommand("start", "Ferzan Launch home"),
                BotCommand("launch", "Launch a token"),
                BotCommand("new", "New launches & King of the Hill"),
                BotCommand("top", "Top creators leaderboard"),
                BotCommand("history", "Your launches"),
                BotCommand("following", "Creators you follow"),
                BotCommand("alerts", "Your coin alerts"),
                BotCommand("tiers", "FERZAN holder tiers and perks"),
                BotCommand("drafts", "Scheduled launches"),
                BotCommand("claim", "Claim your trading fees"),
                BotCommand("timezone", "Set your time zone"),
                BotCommand("refer", "Your referral link"),
                BotCommand("referwallet", "Set referral payout wallet"),
                BotCommand("lplock", "Burn / lock LP helper"),
                BotCommand("liquidity", "Add a SunSwap pool for your Tron coin"),
                BotCommand("cancel", "Cancel launch"),
            ]
        )

    async def _post_all(application):
        await _post(application)
        if MINI_APP_BASE_URL.startswith("https://"):
            try:
                from telegram import MenuButtonWebApp
                try:
                    me = await application.bot.get_me()
                except Exception:
                    me = None
                # the menu button opens a chooser first: the app, or classic chat mode
                menu_url = f"{MINI_APP_BASE_URL}/app.html?from=menu&bot={getattr(me, 'username', '') or ''}"
                await application.bot.set_chat_menu_button(
                    menu_button=MenuButtonWebApp(text="🚀 Launch", web_app=WebAppInfo(url=menu_url)))
            except Exception as e:
                logger.info("menu button not set: %s", str(e)[:80])
        application.create_task(_draft_loop(application))
        application.create_task(_watch_loop(application))
        application.create_task(_milestone_loop(application))

    app.post_init = _post_all
    logger.info("Ferzan Launch starting...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
