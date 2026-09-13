"""Ferzan Buy — channel buy alerts. Token: BUYBOT_TOKEN in .env"""
from __future__ import annotations

import html
import logging
import os
import sqlite3
import time
from pathlib import Path

import requests
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv("/opt/ferzan/.env")
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s buybot %(message)s")
log = logging.getLogger("buybot")

DB = Path(os.getenv("BUYBOT_DB", "/opt/ferzan/app/buybot.db"))
TRADE = (os.getenv("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")
LAST_MEDIA: dict = {}
SETGIF_WAIT: set = set()
MIN_USD = float(os.getenv("BUYBOT_MIN_USD") or "15")

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
    con.execute(
        """CREATE TABLE IF NOT EXISTS raids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            url TEXT,
            note TEXT,
            created INTEGER
        )"""
    )
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


def _ds(ca: str) -> dict:
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{ca}", timeout=12)
        pairs = (r.json() or {}).get("pairs") or []
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


def _bar(usd: float) -> str:
    n = 1 if usd < 20 else 3 if usd < 50 else 8 if usd < 150 else 16 if usd < 500 else 24
    return "🟢" * min(n, 24)


def _card(chain: str, ca: str, tr: dict, attrs: dict) -> tuple[str, InlineKeyboardMarkup]:
    usd = float(tr.get("volume_in_usd") or 0)
    got = tr.get("to_token_amount") or tr.get("to_token_output") or ""
    spent = tr.get("from_token_amount") or ""
    buyer = tr.get("tx_from_address") or tr.get("origin_from_address") or ""
    tx = tr.get("tx_hash") or ""
    name = attrs.get("name") or ca[:8]
    mc = attrs.get("fdv_usd") or attrs.get("market_cap_usd") or attrs.get("reserve_in_usd") or ""
    ds = f"https://dexscreener.com/{GT_NET.get(chain, chain)}/{ca}"
    buy = f"https://t.me/{TRADE}?start={ca}"
    scan = {
        "sol": f"https://solscan.io/tx/{tx}",
        "base": f"https://basescan.org/tx/{tx}",
        "eth": f"https://etherscan.io/tx/{tx}",
        "bsc": f"https://bscscan.com/tx/{tx}",
        "arb": f"https://arbiscan.io/tx/{tx}",
    }.get(chain, ds)
    liq = (os.getenv("FERZAN_LIQ_BOT") or "FerzanLiqBot").lstrip("@")
    boost = f"https://t.me/{liq}" if liq else "https://t.me/Ferzan_Chat"
    text = (
        f"<b>{_esc(name)}</b> [{_esc(str(attrs.get('symbol') or name))}] ⚡ Buy!\n"
        f"{_bar(usd)}\n\n"
        f"💵 | {_esc(spent or f'${usd:,.2f}')} (${usd:,.2f})\n"
        f"💼 | Got: {_esc(got)}\n"
        f"👤 | Buyer | Tx\n"
        f"🎯 | Market Cap: {_esc(str(mc)[:20])}\n"
        f"📈 | Dex\n"
        f"<code>{_esc(ca)}</code>"
    )
    rows = [
        [
            InlineKeyboardButton("⚡ Ferzan Buy", url=buy),
            InlineKeyboardButton("📈 Dex", url=ds),
        ],
    ]
    if tx:
        rows.append([InlineKeyboardButton("🔎 Tx", url=scan)])
    rows.append([InlineKeyboardButton("🚀 Boost Rank and Volume", url=boost)])
    kb = InlineKeyboardMarkup(rows)
    return text, kb


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "⚡ Ferzan Buy — channel buy + raid desk\n\n"
        "/add <chain> <CA> [min_usd]  pair token\n"
        "/settings  min size + current CA\n"
        "/stats /price /dex  token card\n"
        "/market  BTC ETH SOL\n"
        "/vote  start a vote in this chat\n"
        "/raid <x.com url>  post an X raid\n"
        "/queue <url>  add raid to queue\n"
        "/next  post next queued raid\n"
        "/nextlist /queuelist  show queue\n"
        "/lb /clb  raid leaderboards\n"
        "/raidevent /relb  event scores\n"
        "/untrack  stop buy alerts\n"
        "/help"
    )


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
        return "video", msg.video.file_id
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


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = _watch(update.effective_chat.id)
    if not row:
        await update.effective_message.reply_text("No token paired. /add base 0xCA 25")
        return
    chain, ca, pool, min_usd = row
    await update.effective_message.reply_text(
        f"⚙️ Settings\nChain: {chain.upper()}\nCA: `{ca}`\nMin buy: ${float(min_usd or 15):.0f}\n"
        "Change min: /add {chain} {ca} 50".replace("{chain}", chain).replace("{ca}", ca),
        parse_mode="Markdown",
    )


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


async def raid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /raid https://x.com/...")
        return
    url = context.args[0]
    note = " ".join(context.args[1:])
    con = _db()
    con.execute(
        "INSERT INTO raids(chat_id, url, note, created) VALUES(?,?,?,?)",
        (update.effective_chat.id, url, note, int(time.time())),
    )
    con.commit()
    con.close()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Open X", url=url)]])
    await update.effective_message.reply_text(
        f"📣 RAID\n{url}\n{note}\nLike · Repost · Comment. Tap /raidjoin to log points.",
        reply_markup=kb,
    )


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


async def untrack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    con.execute("DELETE FROM watches WHERE chat_id=?", (update.effective_chat.id,))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Stopped buy alerts in this chat.")


async def tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = list(con.execute("SELECT chat_id, chain, ca, pool, last_ts, min_usd FROM watches"))
    for chat_id, chain, ca, pool, last_ts, min_usd in rows:
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
            text, kb = _card(chain, ca, tr, attrs)
            try:
                media = _media(chat_id)
                if media and media[0] == "animation":
                    await context.bot.send_animation(chat_id, media[1], caption=text, parse_mode="HTML", reply_markup=kb)
                elif media and media[0] == "video":
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


def main() -> None:
    token = (os.getenv("BUYBOT_TOKEN") or os.getenv("FERZAN_BUY_TOKEN") or "").strip()
    if not token:
        raise SystemExit("Set BUYBOT_TOKEN in /opt/ferzan/.env")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("setgif", setgif_cmd))
    app.add_handler(CommandHandler("cleargif", cleargif_cmd))
    app.add_handler(MessageHandler(
        filters.ANIMATION | filters.VIDEO | filters.PHOTO | filters.Document.ALL,
        remember_media,
    ))
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("add", track))
    app.add_handler(CommandHandler("untrack", untrack))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("dex", dex_cmd))
    app.add_handler(CommandHandler("market", market_cmd))
    app.add_handler(CommandHandler("vote", vote_cmd))
    app.add_handler(CommandHandler("raid", raid_cmd))
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
