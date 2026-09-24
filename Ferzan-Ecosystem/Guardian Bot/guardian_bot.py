"""Ferzan Guardian — group shield. Token: GUARDIAN_TOKEN"""
from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
DEFAULT_WELCOME = "👋 Welcome {name} to {chat}!\n\nCheck the pinned message for rules and links. Glad to have you."
CAPTCHA_TIMEOUT_DEFAULT = 300  # seconds
CAPTCHA_TIMEOUT_RANGE = (30, 3600)


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
    # migration: welcome-message + captcha columns on the existing settings table
    scols = {r[1] for r in con.execute("PRAGMA table_info(settings)").fetchall()}
    if "welcome_on" not in scols:
        con.execute("ALTER TABLE settings ADD COLUMN welcome_on INTEGER DEFAULT 1")
    if "welcome_text" not in scols:
        con.execute("ALTER TABLE settings ADD COLUMN welcome_text TEXT")
    if "captcha_on" not in scols:
        con.execute("ALTER TABLE settings ADD COLUMN captcha_on INTEGER DEFAULT 0")
    if "captcha_timeout" not in scols:
        con.execute(f"ALTER TABLE settings ADD COLUMN captcha_timeout INTEGER DEFAULT {CAPTCHA_TIMEOUT_DEFAULT}")
    if "clean_service" not in scols:
        con.execute("ALTER TABLE settings ADD COLUMN clean_service INTEGER DEFAULT 1")
    con.execute(
        """CREATE TABLE IF NOT EXISTS pending_captcha (
            chat_id INTEGER, user_id INTEGER, message_id INTEGER, deadline INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.commit()
    return con


def _get_settings(con: sqlite3.Connection, chat_id: int) -> dict:
    con.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
    con.commit()
    row = con.execute(
        "SELECT welcome_on, welcome_text, captcha_on, captcha_timeout, clean_service FROM settings WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    return {
        "welcome_on": bool(row[0]) if row and row[0] is not None else True,
        "welcome_text": (row[1] if row else None) or DEFAULT_WELCOME,
        "captcha_on": bool(row[2]) if row else False,
        "captcha_timeout": int(row[3]) if row and row[3] else CAPTCHA_TIMEOUT_DEFAULT,
        "clean_service": bool(row[4]) if row and row[4] is not None else True,
    }


def _render_welcome(template: str, user, chat) -> str:
    name = _esc(user.full_name or (f"@{user.username}" if user.username else "there"))
    title = _esc(getattr(chat, "title", None) or "the group")
    try:
        return template.format(name=name, chat=title)
    except (KeyError, IndexError, ValueError):
        return DEFAULT_WELCOME.format(name=name, chat=title)


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
    con = _db()
    s = _get_settings(con, update.effective_chat.id)
    con.close()
    await update.effective_message.reply_text(
        "🛡 <b>Guardian /gmenu</b>\n\n"
        "/gfilter scamword — add a blocked phrase\n"
        "/gunfilter scamword — remove it\n"
        "/gfilters — list\n"
        "/gban reply or /gban user_id — global ban\n"
        "/gunban user_id — lift global ban\n\n"
        f"/welcome on|off — greet new members ({'ON' if s['welcome_on'] else 'OFF'})\n"
        "/setwelcome text — customize it ({name} and {chat} are filled in)\n"
        "/testwelcome — preview it now\n"
        f"/captcha on|off [seconds] — tap-to-verify before a new member can chat "
        f"({'ON, ' + str(s['captcha_timeout']) + 's' if s['captcha_on'] else 'OFF'})\n\n"
        f"/cleanservice on|off — auto-delete \"joined/left\" messages ({'ON' if s['clean_service'] else 'OFF'})\n"
        "/purge — reply to a message, then /purge to wipe from there to now (max 200)\n\n"
        "Leave me admin. I already watch joins for fake admin names.",
        parse_mode="HTML",
    )


def _is_anon_admin(update: Update) -> bool:
    """True when this message was posted as the group's "Remain Anonymous"
    admin identity. Telegram delivers those with sender_chat == the group
    itself and effective_user as a GroupAnonymousBot placeholder that isn't
    in the member list -- only an admin can send that way, so it's a valid
    admin signal on its own, no member lookup needed (or possible)."""
    msg = update.effective_message
    sender_chat = getattr(msg, "sender_chat", None) if msg else None
    chat = update.effective_chat
    return bool(sender_chat and chat and sender_chat.id == chat.id)


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_chat.type == "private" or _is_anon_admin(update):
        return True
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Same admin check, but never silent: replies with what Telegram
    actually reported so a "nothing happened" report is self-diagnosing."""
    if update.effective_chat.type == "private" or _is_anon_admin(update):
        return True
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    except TelegramError as exc:
        await update.effective_message.reply_text(f"Couldn't check your admin status: {exc}")
        return False
    if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        return True
    await update.effective_message.reply_text(
        f"Admins only for that one — Telegram has you as \"{member.status}\" in this chat, not admin.\n"
        "If you're posting as \"Remain Anonymous\", that should already work — if it still doesn't, "
        "double-check Guardian can see the group's admin list (re-add it as admin)."
    )
    return False


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


async def welcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        await update.effective_message.reply_text("Usage: /welcome on  or  /welcome off")
        return
    con = _db()
    con.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (update.effective_chat.id,))
    con.execute("UPDATE settings SET welcome_on=? WHERE chat_id=?", (1 if arg == "on" else 0, update.effective_chat.id))
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"👋 Welcome message: {'ON' if arg == 'on' else 'OFF'}.")


async def setwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    text = update.effective_message.text or ""
    body = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
    if not body:
        await update.effective_message.reply_text(
            "Usage: /setwelcome 👋 Welcome {name} to {chat}! Read the pinned rules.\n\n"
            "{name} and {chat} get filled in automatically. /setwelcome default resets it."
        )
        return
    con = _db()
    con.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (update.effective_chat.id,))
    if body.lower() == "default":
        con.execute("UPDATE settings SET welcome_text=NULL WHERE chat_id=?", (update.effective_chat.id,))
        con.commit()
        con.close()
        await update.effective_message.reply_text("Reset to the default welcome message.")
        return
    con.execute("UPDATE settings SET welcome_text=? WHERE chat_id=?", (body, update.effective_chat.id))
    con.commit()
    con.close()
    preview = _render_welcome(body, update.effective_user, update.effective_chat)
    await update.effective_message.reply_text(f"Saved. Preview:\n\n{preview}")


async def testwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    con = _db()
    s = _get_settings(con, update.effective_chat.id)
    con.close()
    await update.effective_message.reply_text(_render_welcome(s["welcome_text"], update.effective_user, update.effective_chat))


async def captcha_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    args = context.args or []
    arg = args[0].lower() if args else ""
    if arg not in ("on", "off"):
        await update.effective_message.reply_text(
            "Usage: /captcha on [seconds]  or  /captcha off\n"
            "New members are muted with a \"tap to verify\" button until they tap it "
            f"(default {CAPTCHA_TIMEOUT_DEFAULT}s, then they're removed)."
        )
        return
    timeout = CAPTCHA_TIMEOUT_DEFAULT
    if arg == "on" and len(args) > 1:
        try:
            timeout = max(CAPTCHA_TIMEOUT_RANGE[0], min(CAPTCHA_TIMEOUT_RANGE[1], int(args[1])))
        except ValueError:
            await update.effective_message.reply_text("Seconds must be a number.")
            return
    con = _db()
    con.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (update.effective_chat.id,))
    if arg == "on":
        con.execute(
            "UPDATE settings SET captcha_on=1, captcha_timeout=? WHERE chat_id=?",
            (timeout, update.effective_chat.id),
        )
    else:
        con.execute("UPDATE settings SET captcha_on=0 WHERE chat_id=?", (update.effective_chat.id,))
    con.commit()
    con.close()
    if arg == "on":
        await update.effective_message.reply_text(
            f"🔐 Captcha ON — new members must tap to verify within {timeout}s or they're removed."
        )
    else:
        await update.effective_message.reply_text("🔐 Captcha OFF.")


async def cleanservice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    arg = (context.args[0].lower() if context.args else "")
    if arg not in ("on", "off"):
        con = _db()
        s = _get_settings(con, update.effective_chat.id)
        con.close()
        await update.effective_message.reply_text(
            "Usage: /cleanservice on  or  /cleanservice off\n"
            "Auto-deletes Telegram's own \"X joined/left the group\" messages "
            f"(currently {'ON' if s['clean_service'] else 'OFF'})."
        )
        return
    con = _db()
    con.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (update.effective_chat.id,))
    con.execute(
        "UPDATE settings SET clean_service=? WHERE chat_id=?", (1 if arg == "on" else 0, update.effective_chat.id)
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"🧹 Join/leave service messages: {'auto-deleted' if arg == 'on' else 'left alone'}.")


async def on_service_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    con = _db()
    s = _get_settings(con, update.effective_chat.id)
    con.close()
    if s["clean_service"]:
        try:
            await msg.delete()
        except TelegramError:
            pass


PURGE_MAX = 200


async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("Reply to the message you want to purge FROM (the oldest one to delete), then send /purge.")
        return
    start_id = msg.reply_to_message.message_id
    end_id = msg.message_id
    if end_id <= start_id:
        await msg.reply_text("Nothing to purge there.")
        return
    span = end_id - start_id + 1
    if span > PURGE_MAX:
        await msg.reply_text(f"That's {span} messages — /purge handles at most {PURGE_MAX} at a time. Reply closer to now and run it again (it chains fine).")
        return
    chat_id = update.effective_chat.id
    deleted = 0
    for mid in range(start_id, end_id + 1):
        try:
            await context.bot.delete_message(chat_id, mid)
            deleted += 1
        except TelegramError:
            pass  # already gone, too old, or not a deletable message type -- skip it
    note = await context.bot.send_message(chat_id, f"🧹 Purged {deleted} message(s).")
    context.job_queue.run_once(
        _purge_note_cleanup, 8, data={"chat_id": chat_id, "message_id": note.message_id}, name=f"purgenote:{chat_id}:{note.message_id}"
    )


async def _purge_note_cleanup(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data or {}
    try:
        await context.bot.delete_message(d["chat_id"], d["message_id"])
    except TelegramError:
        pass


async def _start_captcha(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user, timeout: int) -> None:
    try:
        await context.bot.restrict_chat_member(chat_id, user.id, ChatPermissions(can_send_messages=False))
    except TelegramError as exc:
        log.warning("captcha mute failed for %s in %s: %s", user.id, chat_id, exc)
        return
    name = _esc(user.full_name or (f"@{user.username}" if user.username else "there"))
    try:
        msg = await context.bot.send_message(
            chat_id,
            f"🔐 {name}, tap below within {timeout}s to verify you're human and unlock chat.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ I'm human", callback_data=f"cap:{user.id}")]]
            ),
        )
    except TelegramError as exc:
        log.warning("captcha post failed in %s: %s", chat_id, exc)
        return
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO pending_captcha(chat_id, user_id, message_id, deadline) VALUES(?,?,?,?)",
        (chat_id, user.id, msg.message_id, int(time.time()) + timeout),
    )
    con.commit()
    con.close()
    context.job_queue.run_once(
        _captcha_timeout_job,
        timeout,
        data={"chat_id": chat_id, "user_id": user.id, "message_id": msg.message_id},
        name=f"captcha:{chat_id}:{user.id}",
    )


async def _unmute(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    perms = None
    try:
        chat = await context.bot.get_chat(chat_id)
        perms = chat.permissions
    except TelegramError:
        pass
    if not perms:
        perms = ChatPermissions(
            can_send_messages=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True, can_invite_users=True,
        )
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, perms)
    except TelegramError as exc:
        log.warning("unmute failed for %s in %s: %s", user_id, chat_id, exc)


async def captcha_verify_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("cap:"):
        return
    try:
        target_id = int(q.data.split(":", 1)[1])
    except ValueError:
        await q.answer()
        return
    if q.from_user.id != target_id:
        await q.answer("That button isn't for you.", show_alert=True)
        return
    chat_id = q.message.chat_id
    con = _db()
    row = con.execute(
        "SELECT 1 FROM pending_captcha WHERE chat_id=? AND user_id=?", (chat_id, target_id)
    ).fetchone()
    if not row:
        con.close()
        await q.answer("Already handled.")
        return
    con.execute("DELETE FROM pending_captcha WHERE chat_id=? AND user_id=?", (chat_id, target_id))
    s = _get_settings(con, chat_id)
    con.commit()
    con.close()
    for job in context.job_queue.get_jobs_by_name(f"captcha:{chat_id}:{target_id}"):
        job.schedule_removal()
    await _unmute(context, chat_id, target_id)
    await q.answer("Verified ✅")
    try:
        await q.message.delete()
    except TelegramError:
        pass
    if s["welcome_on"]:
        await context.bot.send_message(chat_id, _render_welcome(s["welcome_text"], q.from_user, q.message.chat))


async def _captcha_timeout_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    d = context.job.data or {}
    chat_id, user_id, message_id = d.get("chat_id"), d.get("user_id"), d.get("message_id")
    if not chat_id or not user_id:
        return
    con = _db()
    row = con.execute("SELECT 1 FROM pending_captcha WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    if not row:
        con.close()
        return  # already verified
    con.execute("DELETE FROM pending_captcha WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    con.commit()
    con.close()
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)  # kick, not a permanent ban
    except TelegramError as exc:
        log.warning("captcha kick failed for %s in %s: %s", user_id, chat_id, exc)
    if message_id:
        try:
            await context.bot.delete_message(chat_id, message_id)
        except TelegramError:
            pass


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
    old = cmu.old_chat_member
    if new.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        return
    # Only a real join (not our own mute/unmute, or an admin action) starts
    # welcome/captcha -- otherwise every restrict we do would re-fire this.
    is_join = old.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
    user = new.user
    chat_id = update.effective_chat.id
    con = _db()
    banned = con.execute("SELECT 1 FROM global_bans WHERE user_id=?", (user.id,)).fetchone()
    settings = _get_settings(con, chat_id)
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
        return
    if not is_join or user.is_bot:
        return
    if settings["captcha_on"]:
        await _start_captcha(context, chat_id, user, settings["captcha_timeout"])
    elif settings["welcome_on"]:
        await context.bot.send_message(chat_id, _render_welcome(settings["welcome_text"], user, update.effective_chat))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or update.effective_chat.type == "private":
        return
    user = update.effective_user
    if not user:
        return
    if _is_anon_admin(update):
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
    app.add_handler(CommandHandler("welcome", welcome_cmd))
    app.add_handler(CommandHandler("setwelcome", setwelcome_cmd))
    app.add_handler(CommandHandler("testwelcome", testwelcome_cmd))
    app.add_handler(CommandHandler("captcha", captcha_cmd))
    app.add_handler(CommandHandler("cleanservice", cleanservice_cmd))
    app.add_handler(CommandHandler("purge", purge_cmd))
    app.add_handler(CallbackQueryHandler(captcha_verify_cb, pattern=r"^cap:"))
    app.add_handler(ChatMemberHandler(on_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER, on_service_message)
    )
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
            BotCommand("welcome", "Toggle welcome message"),
            BotCommand("setwelcome", "Set the welcome message"),
            BotCommand("testwelcome", "Preview the welcome message"),
            BotCommand("captcha", "Toggle tap-to-verify for new members"),
            BotCommand("cleanservice", "Toggle auto-delete of join/leave messages"),
            BotCommand("purge", "Reply to a message to delete from there to now"),
        ]
        await application.bot.set_my_commands(cmds)

    app.post_init = _post
    log.info("Ferzan Guardian running")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
