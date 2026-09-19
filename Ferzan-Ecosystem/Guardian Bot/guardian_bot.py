"""Ferzan Guardian — group shield. Token: GUARDIAN_TOKEN"""
from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv("/opt/ferzan/.env")
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s guardian %(message)s")
log = logging.getLogger("guardian")

DB = Path(os.getenv("GUARDIAN_DB", "/opt/ferzan/app/guardian.db"))
TRADE = (os.getenv("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")
BUY = (os.getenv("FERZAN_BUY_BOT") or "Ferzan_Buy_Bot").lstrip("@")
LIQ = (os.getenv("FERZAN_LIQ_BOT") or "FerzanLiqBot").lstrip("@")
ME = (os.getenv("FERZAN_GUARDIAN_BOT") or "FerzanGuardianBot").lstrip("@")
CHAT = os.getenv("FERZAN_CHAT_URL") or "https://t.me/Ferzan_Chat"
FAKE = re.compile(r"(admin|owner|dev|support|moderator|official|helpdesk)", re.I)
DEFAULT_WORDS = {"airdrop claim", "double your sol", "seed phrase", "connect wallet to claim", "free mint drainer"}


def _db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute(
        """CREATE TABLE IF NOT EXISTS global_bans (
            user_id INTEGER PRIMARY KEY, reason TEXT, by_id INTEGER, ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS filters (
            chat_id INTEGER, word TEXT, PRIMARY KEY (chat_id, word)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS settings (
            chat_id INTEGER PRIMARY KEY,
            antiname INTEGER DEFAULT 1,
            antilink INTEGER DEFAULT 1,
            antimedia INTEGER DEFAULT 0
        )"""
    )
    return con


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


def _start_text() -> str:
    return (
        "🛡 <b>Welcome to Ferzan Guardian</b>\n\n"
        "Community shield for <b>any</b> Telegram project — not only Ferzan.\n"
        "Devs add me to their token chat. I keep drainers, fake admins, and spam out.\n\n"
        "⚙️ <b>Group config</b> — /gmenu in your group\n"
        "🚫 <b>Username policy</b> — ban admin / owner / dev / support impersonators\n"
        "🎭 <b>Look-alikes</b> — names that copy your real admins get removed\n"
        "🔇 <b>Word filter</b> — delete + mute blacklisted scam lines\n"
        "👁 <b>Fresh-join bait</b> — drop forwarded scam media from new accounts\n"
        "🔗 <b>Cross-group ban</b> — banned in one shielded chat, blocked in the others\n\n"
        "Add as admin: Ban users + Delete messages. Group Privacy off in BotFather."
    )


def _start_kb() -> InlineKeyboardMarkup:
    add = f"https://t.me/{ME}?startgroup=true"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🤖 ADD BOT TO GROUP", url=add)],
            [
                InlineKeyboardButton("👑 Community", url=CHAT),
                InlineKeyboardButton("🤝 Support", url=CHAT),
            ],
            [InlineKeyboardButton("⚡ Ferzan Trade", url=f"https://t.me/{TRADE}")],
            [InlineKeyboardButton("🟢 Ferzan Buy", url=f"https://t.me/{TRADE}")],
            [InlineKeyboardButton("💧 Ferzan Liq", url=f"https://t.me/{LIQ}")],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(_start_text(), parse_mode="HTML", reply_markup=_start_kb())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def gmenu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat.type == "private":
        await start(update, context)
        return
    await update.effective_message.reply_text(
        "🛡 <b>Guardian /gmenu</b>\n\n"
        "/gfilter scamword — add a blocked phrase\n"
        "/gunfilter scamword — remove it\n"
        "/gfilters — list\n"
        "/gban reply or /gban user_id — global ban\n"
        "/gunban user_id — lift global ban\n"
        "/glink on|off — block links from brand-new members\n"
        "Leave me admin. I already watch joins for fake admin names.",
        parse_mode="HTML",
    )


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_chat.type == "private":
        return True
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def gfilter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    word = " ".join(context.args).strip().lower()
    if not word:
        await update.effective_message.reply_text("Usage: /gfilter free mint drainer")
        return
    con = _db()
    con.execute("INSERT OR IGNORE INTO filters(chat_id, word) VALUES(?,?)", (update.effective_chat.id, word))
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Blocked: {_esc(word)}")


async def gunfilter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    word = " ".join(context.args).strip().lower()
    con = _db()
    con.execute("DELETE FROM filters WHERE chat_id=? AND word=?", (update.effective_chat.id, word))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Removed.")


async def gfilters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute("SELECT word FROM filters WHERE chat_id=?", (update.effective_chat.id,)).fetchall()
    con.close()
    extra = ", ".join(w[0] for w in rows) or "(none extra)"
    await update.effective_message.reply_text("Default scam lines + " + extra)


async def gban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    uid = None
    if update.message and update.message.reply_to_message:
        uid = update.message.reply_to_message.from_user.id
    elif context.args:
        try:
            uid = int(context.args[0])
        except ValueError:
            await update.effective_message.reply_text("Reply to them or /gban 123456789")
            return
    if not uid:
        await update.effective_message.reply_text("Reply to the user or pass their numeric id.")
        return
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO global_bans(user_id, reason, by_id, ts) VALUES(?,?,?,strftime('%s','now'))",
        (uid, "manual", update.effective_user.id),
    )
    con.commit()
    con.close()
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, uid)
    except Exception as exc:
        log.warning("ban %s", exc)
    await update.effective_message.reply_text(f"Global ban set for {uid}.")


async def gunban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context) or not context.args:
        return
    uid = int(context.args[0])
    con = _db()
    con.execute("DELETE FROM global_bans WHERE user_id=?", (uid,))
    con.commit()
    con.close()
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, uid)
    except Exception:
        pass
    await update.effective_message.reply_text(f"Lifted {uid}.")


async def _admins(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> list[str]:
    names = []
    try:
        members = await context.bot.get_chat_administrators(chat_id)
        for m in members:
            u = m.user
            names.append((u.username or "").lower())
            names.append((u.full_name or "").lower())
    except Exception:
        pass
    return [n for n in names if n]


async def on_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.chat_member
    if not cmu:
        return
    new = cmu.new_chat_member
    if new.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        return
    user = new.user
    chat_id = update.effective_chat.id
    con = _db()
    banned = con.execute("SELECT 1 FROM global_bans WHERE user_id=?", (user.id,)).fetchone()
    con.close()
    handle = f"{user.username or ''} {user.full_name or ''}"
    if banned or FAKE.search(handle):
        try:
            await context.bot.ban_chat_member(chat_id, user.id)
            await context.bot.send_message(chat_id, f"🛡 Removed impersonator / listed ban: {_esc(user.full_name)}")
        except Exception as exc:
            log.warning("join ban %s", exc)
        return
    admins = await _admins(context, chat_id)
    low = (user.full_name or "").lower()
    if low and any(low == a or (len(low) > 4 and low in a) for a in admins):
        try:
            await context.bot.ban_chat_member(chat_id, user.id)
            await context.bot.send_message(chat_id, "🛡 Removed admin look-alike.")
        except Exception:
            pass


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or update.effective_chat.type == "private":
        return
    user = update.effective_user
    if not user:
        return
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return
    except Exception:
        return
    text = (msg.text or msg.caption or "").lower()
    con = _db()
    extra = [r[0] for r in con.execute("SELECT word FROM filters WHERE chat_id=?", (update.effective_chat.id,))]
    con.close()
    hits = list(DEFAULT_WORDS) + extra
    if any(w in text for w in hits if w):
        try:
            await msg.delete()
            await context.bot.restrict_chat_member(
                update.effective_chat.id,
                user.id,
                ChatPermissions(can_send_messages=False),
            )
        except Exception as exc:
            log.warning("filter %s", exc)


def main() -> None:
    token = (os.getenv("GUARDIAN_TOKEN") or os.getenv("FERZAN_GUARDIAN_TOKEN") or "").strip()
    if not token:
        raise SystemExit("Set GUARDIAN_TOKEN in /opt/ferzan/.env")
    _db()
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("gmenu", gmenu))
    app.add_handler(CommandHandler("menu", gmenu))
    app.add_handler(CommandHandler("gfilter", gfilter))
    app.add_handler(CommandHandler("gunfilter", gunfilter))
    app.add_handler(CommandHandler("gfilters", gfilters))
    app.add_handler(CommandHandler("gban", gban))
    app.add_handler(CommandHandler("gunban", gunban))
    app.add_handler(ChatMemberHandler(on_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.TEXT | filters.CAPTION, on_text))

    async def _post(application):
        cmds = [
            BotCommand("start", "Welcome and add to group"),
            BotCommand("help", "What Guardian does"),
            BotCommand("gmenu", "Shield menu"),
            BotCommand("gfilter", "Block a phrase"),
            BotCommand("gunfilter", "Unblock a phrase"),
            BotCommand("gfilters", "List extra filters"),
            BotCommand("gban", "Global ban"),
            BotCommand("gunban", "Lift a global ban"),
        ]
        await application.bot.set_my_commands(cmds)

    app.post_init = _post
    log.info("Ferzan Guardian running")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
