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
from telegram.ext import Application, CommandHandler, ContextTypes, filters

load_dotenv("/opt/ferzan/.env")
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s buybot %(message)s")
log = logging.getLogger("buybot")

DB = Path(os.getenv("BUYBOT_DB", "/opt/ferzan/app/buybot.db"))
TRADE = (os.getenv("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")
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
    return con


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
    text = (
        f"⚡ <b>FERZAN BUY</b> · {_esc(name)}\n"
        f"{_bar(usd)}\n\n"
        f"💵 {_esc(f'${usd:,.2f}' if usd else spent)}\n"
        f"🎒 Got {_esc(got)}\n"
        f"👤 <code>{_esc(buyer[:10])}…</code>\n"
        f"🧢 MC {_esc(str(mc)[:16])}\n"
        f"<code>{_esc(ca)}</code>"
    )
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⚡ Buy on Ferzan", url=buy),
                InlineKeyboardButton("📈 Chart", url=ds),
            ],
            [InlineKeyboardButton("🔎 Tx", url=scan)] if tx else [],
        ]
    )
    return text, kb


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Ferzan Buy — channel buy alerts.\n\n"
        "Add this bot to a project channel as admin, then:\n"
        "/track base 0x... [min_usd]\n"
        "/track sol <mint> 25\n"
        "/untrack\n"
        "Buy button opens Ferzan Trade with that CA."
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
    app.add_handler(CommandHandler("track", track))
    app.add_handler(CommandHandler("untrack", untrack))
    app.job_queue.run_repeating(tick, interval=25, first=8)
    log.info("Ferzan Buy running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
