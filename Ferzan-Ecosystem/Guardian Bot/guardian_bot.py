"""Ferzan Guardian — group shield. Token: GUARDIAN_TOKEN"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import random
import re
import sqlite3
import time
import urllib.request
from collections import defaultdict, deque
from datetime import datetime as _dt, timedelta, timezone
from datetime import time as dtime
from io import BytesIO
from pathlib import Path

try:
    import pytesseract
    from PIL import Image

    _OCR_AVAILABLE = True
except Exception:
    _OCR_AVAILABLE = False

try:
    from deep_translator import GoogleTranslator

    _TRANSLATE_AVAILABLE = True
except Exception:
    _TRANSLATE_AVAILABLE = False

try:
    from zoneinfo import ZoneInfo, available_timezones

    _TZ_AVAILABLE = True
except Exception:
    _TZ_AVAILABLE = False

from dotenv import load_dotenv
from telegram import (
    BotCommand,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

load_dotenv("/opt/ferzan/.env")
load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s guardian %(message)s")
log = logging.getLogger("guardian")

DB = Path(os.getenv("GUARDIAN_DB", "/opt/ferzan/app/guardian.db"))
BANNER = Path(os.getenv("GUARDIAN_BANNER", str(Path(__file__).resolve().parent / "banner.png")))
TRADE = (os.getenv("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")
BUY = (os.getenv("FERZAN_BUY_BOT") or "Ferzan_Buy_Bot").lstrip("@")
LIQ = (os.getenv("FERZAN_LIQ_BOT") or "FerzanLiqBot").lstrip("@")
ME = (os.getenv("FERZAN_GUARDIAN_BOT") or "FerzanGuardianBot").lstrip("@")
CHAT = os.getenv("FERZAN_CHAT_URL") or "https://t.me/Ferzan_Chat"
X_URL = os.getenv("FERZAN_X_URL") or "https://x.com/ferzaneco"
LOG_CHAT = os.getenv("GUARDIAN_LOG_CHAT", "").strip()
FAKE = re.compile(r"(admin|owner|dev|support|moderator|official|helpdesk)", re.I)
DEFAULT_WORDS = {"airdrop claim", "double your sol", "seed phrase", "connect wallet to claim", "free mint drainer"}
LINK_RE = re.compile(r"(https?://|t\.me/|@\w{4,})", re.I)
OWNER_IDS = {5107098957}
HEARTBEAT_HOURS = float(os.getenv("GUARDIAN_HEARTBEAT_HOURS", "0") or 0)
WEBHOOK_URL = os.getenv("GUARDIAN_WEBHOOK_URL", "").strip()
WEBHOOK_PORT = int(os.getenv("GUARDIAN_WEBHOOK_PORT", "8443") or 8443)
WEBHOOK_PATH = os.getenv("GUARDIAN_WEBHOOK_PATH", "/guardian-webhook").strip() or "/guardian-webhook"
DB_BACKUP_HOURS = float(os.getenv("GUARDIAN_DB_BACKUP_HOURS", "0") or 0)
WEEKLY_DIGEST_ENABLED = (os.getenv("GUARDIAN_WEEKLY_DIGEST", "1") or "1").strip().lower() not in ("0", "off", "false")
# Off by default — opt in once you've weighed that this grows the scam list a lot (thousands of
# entries from a public feed), which makes every message's scam-list check somewhat heavier.
PHISHING_SYNC_HOURS = float(os.getenv("GUARDIAN_PHISHING_SYNC_HOURS", "0") or 0)
# Off by default — DMs every owner a /ghealth-style score for every group Guardian is in.
OWNER_DIGEST_HOURS = float(os.getenv("GUARDIAN_OWNER_DIGEST_HOURS", "0") or 0)
PHISHING_FEED_URL = os.getenv(
    "GUARDIAN_PHISHING_FEED_URL",
    "https://raw.githubusercontent.com/MetaMask/eth-phishing-detect/master/src/config.json",
).strip()

WARN_LIMIT = 3
NEW_MEMBER_LOCK_SECONDS = 900
FLOOD_LIMIT = 6
FLOOD_SECONDS = 8
FLOOD_MUTE_SECONDS = 600
ADAPTIVE_SLOWMODE_WINDOW = 20
ADAPTIVE_SLOWMODE_TRIGGER = 25
ADAPTIVE_SLOWMODE_SECONDS = 10
ADAPTIVE_SLOWMODE_DURATION = 300
RULES_GATE_TIMEOUT_SECONDS = 600
FAQ_COOLDOWN_SECONDS = 300
FAQ_MIN_SCORE = 2
REPORT_COOLDOWN_SECONDS = 30
VOTEMUTE_COOLDOWN_SECONDS = 60
TR_COOLDOWN_SECONDS = 5
RAID_WINDOW_SECONDS = 30
RAID_THRESHOLD = 8
RAID_LOCK_SECONDS = 600
NOPHOTO_RAID_THRESHOLD = 3
NOPHOTO_RESTRICT_SECONDS = 1800
CAPTCHA_TIMEOUT_SECONDS = 300
PURGE_MAX = 300
# Telegram user IDs are roughly sequential — anything above this is a very recently created
# account. Drifts upward over time; admins can bump it with /gnewacct <id>.
NEWACCT_DEFAULT_MIN_ID = 7_800_000_000
NEWACCT_RESTRICT_SECONDS = 900

# Default welcome/goodbye text when the group hasn't set its own — picked by the joining member's
# Telegram language, so international groups get something sensible out of the box. A custom
# /setwelcome or /setgoodbye always wins over this.
DEFAULT_WELCOME_I18N = {
    "en": "Welcome, {first}! Glad to have you.",
    "es": "¡Bienvenido, {first}! Un gusto tenerte aquí.",
    "pt": "Bem-vindo, {first}! Que bom ter você aqui.",
    "ru": "Добро пожаловать, {first}! Рады видеть тебя здесь.",
    "fr": "Bienvenue, {first} ! Ravi de t'avoir parmi nous.",
    "de": "Willkommen, {first}! Schön, dass du da bist.",
    "tr": "Hoş geldin, {first}! Aramızda olmana sevindik.",
    "id": "Selamat datang, {first}! Senang kamu ada di sini.",
    "hi": "स्वागत है, {first}! आपका यहाँ होना अच्छा लगा।",
    "zh": "欢迎你，{first}！很高兴你能加入。",
    "ja": "ようこそ、{first}！参加してくれて嬉しいです。",
    "ko": "환영합니다, {first}님! 함께하게 되어 기뻐요.",
    "ar": "أهلاً بك، {first}! سعداء بانضمامك.",
}
DEFAULT_GOODBYE_I18N = {
    "en": "Goodbye, {first}.",
    "es": "Adiós, {first}.",
    "pt": "Adeus, {first}.",
    "ru": "Прощай, {first}.",
    "fr": "Au revoir, {first}.",
    "de": "Tschüss, {first}.",
    "tr": "Hoşça kal, {first}.",
    "id": "Selamat tinggal, {first}.",
    "hi": "अलविदा, {first}.",
    "zh": "再见，{first}。",
    "ja": "さようなら、{first}。",
    "ko": "안녕히 가세요, {first}님.",
    "ar": "وداعاً، {first}.",
}


def _default_welcome_for(lang_code: str | None) -> str:
    code = (lang_code or "en").split("-")[0].lower()
    return DEFAULT_WELCOME_I18N.get(code, DEFAULT_WELCOME_I18N["en"])


def _default_goodbye_for(lang_code: str | None) -> str:
    code = (lang_code or "en").split("-")[0].lower()
    return DEFAULT_GOODBYE_I18N.get(code, DEFAULT_GOODBYE_I18N["en"])
LOCK_TYPES = {
    "links": "lock_links",
    "forwards": "lock_forwards",
    "stickers": "lock_stickers",
    "photos": "lock_photos",
    "voice": "lock_voice",
    "videonote": "lock_video_note",
}
SCAM_CACHE_TTL = 60

_flood: dict[tuple[int, int], deque] = defaultdict(deque)
_chat_msg_times: dict[int, deque] = defaultdict(deque)
_adaptive_slowmode_until: dict[int, float] = defaultdict(float)
_pending_rules: dict[tuple[int, int], int] = {}
_raid_joins: dict[int, deque] = defaultdict(deque)
_raid_nophoto: dict[int, deque] = defaultdict(deque)
_raid_lock_until: dict[int, float] = defaultdict(float)
_pending_captcha: dict[tuple[int, int], int] = {}
_captcha_answers: dict[tuple[int, int], int] = {}
_scam_cache: dict = {"ts": 0.0, "items": []}
_slowmode_last: dict[tuple[int, int], float] = {}
_local_scam_cache: dict[int, dict] = defaultdict(lambda: {"ts": 0.0, "items": []})

# chat_id -> (admin_user_id who armed it, expiry ts). Set by a bare /setwelcome; consumed by the
# next photo/GIF/video that SAME admin posts in the chat (no caption or command needed).
_pending_welcome_media: dict[int, tuple[int, float]] = {}

# chat_id -> (admin_user_id who armed it, note name, expiry ts). Set by a bare /save <name>;
# consumed by the next photo/GIF/video that SAME admin posts in the chat.
_pending_note_media: dict[int, tuple[int, str, float]] = {}


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
    con.execute(
        """CREATE TABLE IF NOT EXISTS joins (
            chat_id INTEGER, user_id INTEGER, joined_ts INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS warns (
            chat_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS approved (
            chat_id INTEGER, user_id INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS notes (
            chat_id INTEGER, name TEXT, text TEXT,
            PRIMARY KEY (chat_id, name)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS autoreply (
            chat_id INTEGER, trig TEXT, reply TEXT,
            PRIMARY KEY (chat_id, trig)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS scam_list (
            value TEXT PRIMARY KEY, kind TEXT, added_by INTEGER, ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS msg_counts (
            chat_id INTEGER, day TEXT, count INTEGER DEFAULT 0,
            PRIMARY KEY (chat_id, day)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS known_chats (
            chat_id INTEGER PRIMARY KEY, title TEXT, last_seen INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS local_scam (
            chat_id INTEGER, value TEXT, kind TEXT, added_by INTEGER, ts INTEGER,
            PRIMARY KEY (chat_id, value)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS link_whitelist (
            chat_id INTEGER, domain TEXT,
            PRIMARY KEY (chat_id, domain)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS scam_reports (
            value TEXT, kind TEXT, chat_id INTEGER, ts INTEGER,
            PRIMARY KEY (value, kind, chat_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS mods (
            chat_id INTEGER, user_id INTEGER, added_by INTEGER, ts INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS shadowbanned (
            chat_id INTEGER, user_id INTEGER, by_id INTEGER, ts INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS user_trust (
            user_id INTEGER PRIMARY KEY, first_seen INTEGER, last_seen INTEGER,
            groups_seen INTEGER DEFAULT 0, strikes_ever INTEGER DEFAULT 0
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS giveaways (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, prize TEXT, message_id INTEGER,
            ends_ts INTEGER, winner_id INTEGER, status TEXT DEFAULT 'active', created_by INTEGER, ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS giveaway_entries (
            giveaway_id INTEGER, user_id INTEGER, ts INTEGER,
            PRIMARY KEY (giveaway_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS vote_mutes (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, target_id INTEGER, message_id INTEGER,
            threshold INTEGER, created_by INTEGER, status TEXT DEFAULT 'active', ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS vote_mute_votes (
            vote_id INTEGER, user_id INTEGER,
            PRIMARY KEY (vote_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS scheduled_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, kind TEXT, hour INTEGER, minute INTEGER,
            weekday INTEGER, text TEXT, created_by INTEGER, ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, reporter_id INTEGER, target_id INTEGER,
            message_id INTEGER, text TEXT, status TEXT DEFAULT 'open', resolved_by INTEGER, ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS mute_history (
            chat_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0, last_ts INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS faq (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, question TEXT, answer TEXT,
            keywords TEXT, created_by INTEGER, ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, mod_id INTEGER, target_id INTEGER,
            action TEXT, reason TEXT, ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS current_mutes (
            chat_id INTEGER, user_id INTEGER, until_ts INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS chat_bans (
            chat_id INTEGER, user_id INTEGER, ts INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS welcome_variants (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, text TEXT, ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS link_scan_cache (
            domain TEXT PRIMARY KEY, verdict TEXT, ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS sticker_blocklist (
            chat_id INTEGER, set_name TEXT, added_by INTEGER, ts INTEGER,
            PRIMARY KEY (chat_id, set_name)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS admin_spree_lock (
            chat_id INTEGER, user_id INTEGER, ts INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS join_invite_links (
            chat_id INTEGER, user_id INTEGER, invite_link TEXT, link_name TEXT, ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS config_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, admin_id INTEGER,
            action TEXT, detail TEXT, ts INTEGER
        )"""
    )
    con.execute(
        """CREATE TABLE IF NOT EXISTS command_perms (
            chat_id INTEGER, command TEXT, tier TEXT,
            PRIMARY KEY (chat_id, command)
        )"""
    )
    for ddl in (
        "ALTER TABLE settings ADD COLUMN welcome_text TEXT",
        "ALTER TABLE settings ADD COLUMN welcome_enabled INTEGER DEFAULT 1",
        "ALTER TABLE settings ADD COLUMN rules_text TEXT",
        "ALTER TABLE settings ADD COLUMN captcha_enabled INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN lock_links INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN lock_forwards INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN lock_stickers INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN lock_photos INTEGER DEFAULT 0",
        "ALTER TABLE filters ADD COLUMN action TEXT DEFAULT 'mute'",
        "ALTER TABLE settings ADD COLUMN welcome_media_id TEXT",
        "ALTER TABLE settings ADD COLUMN welcome_media_type TEXT",
        "ALTER TABLE notes ADD COLUMN media_id TEXT",
        "ALTER TABLE notes ADD COLUMN media_type TEXT",
        "ALTER TABLE settings ADD COLUMN goodbye_text TEXT",
        "ALTER TABLE settings ADD COLUMN goodbye_enabled INTEGER DEFAULT 1",
        "ALTER TABLE settings ADD COLUMN cleanservice_enabled INTEGER DEFAULT 1",
        "ALTER TABLE settings ADD COLUMN warn_limit INTEGER DEFAULT 3",
        "ALTER TABLE settings ADD COLUMN warn_decay_days INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN log_chat_id TEXT",
        "ALTER TABLE settings ADD COLUMN slowmode_seconds INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN welcome_btn_label TEXT",
        "ALTER TABLE settings ADD COLUMN welcome_btn_url TEXT",
        "ALTER TABLE warns ADD COLUMN last_ts INTEGER DEFAULT 0",
        "ALTER TABLE filters ADD COLUMN is_regex INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN lock_voice INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN lock_video_note INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN newacct_enabled INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN newacct_min_id INTEGER",
        "ALTER TABLE settings ADD COLUMN captcha_mode TEXT DEFAULT 'button'",
        "ALTER TABLE settings ADD COLUMN votemute_enabled INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN votemute_threshold INTEGER DEFAULT 5",
        "ALTER TABLE settings ADD COLUMN honeypot_cmd TEXT",
        "ALTER TABLE settings ADD COLUMN linkscan_enabled INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN timezone TEXT",
        "ALTER TABLE settings ADD COLUMN rules_gate_enabled INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN adaptive_slowmode_enabled INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN quiet_hours_enabled INTEGER DEFAULT 0",
        "ALTER TABLE settings ADD COLUMN quiet_hours_start INTEGER",
        "ALTER TABLE settings ADD COLUMN quiet_hours_end INTEGER",
        "ALTER TABLE scheduled_posts ADD COLUMN tz_name TEXT",
        "ALTER TABLE scheduled_posts ADD COLUMN local_hour INTEGER",
        "ALTER TABLE scheduled_posts ADD COLUMN local_minute INTEGER",
        "ALTER TABLE scheduled_posts ADD COLUMN local_weekday INTEGER",
    ):
        try:
            con.execute(ddl)
        except sqlite3.OperationalError:
            pass
    return con


def _esc(s) -> str:
    return html.escape(str(s or ""), quote=False)


_DURATION_RE = re.compile(r"^(\d+)([mhd])$", re.I)
_DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400}


def _parse_duration(text: str, default_seconds: int = 3600) -> int:
    """Parses '10m' / '1h' / '2d' into seconds. Falls back to default_seconds on anything else."""
    if not text:
        return default_seconds
    m = _DURATION_RE.match(text.strip())
    if not m:
        return default_seconds
    return int(m.group(1)) * _DURATION_UNITS[m.group(2).lower()]


def _log_chat_for(chat_id: int | None) -> str:
    if chat_id is not None:
        con = _db()
        row = con.execute("SELECT log_chat_id FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
        con.close()
        if row and row[0]:
            return row[0]
    return LOG_CHAT


async def _log(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    chat_id: int | None = None,
    keyboard: InlineKeyboardMarkup | None = None,
) -> None:
    dest = _log_chat_for(chat_id)
    if not dest:
        return
    try:
        await context.bot.send_message(
            dest, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=keyboard
        )
    except Exception as exc:
        log.warning("guardian log %s", exc)


def _antilink_enabled(chat_id: int) -> bool:
    con = _db()
    row = con.execute("SELECT antilink FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return True if row is None else bool(row[0])


def _captcha_enabled(chat_id: int) -> bool:
    con = _db()
    row = con.execute("SELECT captcha_enabled FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return bool(row[0]) if row and row[0] is not None else False


def _captcha_mode(chat_id: int) -> str:
    con = _db()
    row = con.execute("SELECT captcha_mode FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    mode = (row[0] if row else None) or "button"
    return mode if mode in ("button", "math") else "button"


def _welcome_settings(chat_id: int) -> tuple[str | None, bool]:
    con = _db()
    row = con.execute(
        "SELECT welcome_text, welcome_enabled FROM settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    con.close()
    if row is None:
        return None, True
    return row[0], (True if row[1] is None else bool(row[1]))


def _welcome_media(chat_id: int) -> tuple[str | None, str | None]:
    con = _db()
    row = con.execute(
        "SELECT welcome_media_id, welcome_media_type FROM settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    con.close()
    if row is None:
        return None, None
    return row[0], row[1]


def _goodbye_settings(chat_id: int) -> tuple[str | None, bool]:
    con = _db()
    row = con.execute(
        "SELECT goodbye_text, goodbye_enabled FROM settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    con.close()
    if row is None:
        return None, True
    return row[0], (True if row[1] is None else bool(row[1]))


def _cleanservice_enabled(chat_id: int) -> bool:
    con = _db()
    row = con.execute("SELECT cleanservice_enabled FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return True if row is None or row[0] is None else bool(row[0])


def _locks(chat_id: int) -> dict:
    con = _db()
    row = con.execute(
        "SELECT lock_links, lock_forwards, lock_stickers, lock_photos, lock_voice, lock_video_note "
        "FROM settings WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    con.close()
    if row is None:
        return {
            "links": False, "forwards": False, "stickers": False,
            "photos": False, "voice": False, "videonote": False,
        }
    return {
        "links": bool(row[0]), "forwards": bool(row[1]), "stickers": bool(row[2]),
        "photos": bool(row[3]), "voice": bool(row[4]), "videonote": bool(row[5]),
    }


def _is_approved(chat_id: int, user_id: int) -> bool:
    con = _db()
    row = con.execute("SELECT 1 FROM approved WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    con.close()
    return bool(row)


def _slowmode_seconds(chat_id: int) -> int:
    con = _db()
    row = con.execute("SELECT slowmode_seconds FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return int(row[0]) if row and row[0] else 0


def _chat_timezone_name(chat_id: int) -> str:
    con = _db()
    row = con.execute("SELECT timezone FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return (row[0] if row else None) or "UTC"


def _tz_for_chat(chat_id: int):
    if not _TZ_AVAILABLE:
        return timezone.utc
    try:
        return ZoneInfo(_chat_timezone_name(chat_id))
    except Exception:
        return timezone.utc


def _local_to_utc_hm(chat_id: int, hour: int, minute: int, weekday: int | None = None) -> tuple[int, int, int | None]:
    """Converts an admin-entered local HH:MM (and optional mon=0..sun=6 weekday) into UTC,
    using the group's configured timezone (UTC if none set)."""
    tz = _tz_for_chat(chat_id)
    base = _dt(2024, 1, 1)  # a Monday, so weekday() lines up with the mon..sun index scheme used here
    if weekday is not None:
        base = base + timedelta(days=weekday)
    local_dt = base.replace(hour=hour, minute=minute, tzinfo=tz)
    utc_dt = local_dt.astimezone(timezone.utc)
    new_weekday = utc_dt.weekday() if weekday is not None else None
    return utc_dt.hour, utc_dt.minute, new_weekday


def _honeypot_cmd(chat_id: int) -> str | None:
    con = _db()
    row = con.execute("SELECT honeypot_cmd FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return row[0] if row and row[0] else None


_cmd_cooldown: dict[tuple[int, int, str], float] = {}


def _check_cooldown(user_id: int, chat_id: int, cmd: str, seconds: int) -> float:
    """Returns 0 if the action is allowed (and records the hit), else the seconds remaining."""
    key = (chat_id, user_id, cmd)
    now_ts = time.time()
    last = _cmd_cooldown.get(key, 0.0)
    remaining = seconds - (now_ts - last)
    if remaining > 0:
        return remaining
    _cmd_cooldown[key] = now_ts
    return 0.0


def _escalated_mute_seconds(chat_id: int, user_id: int, base_seconds: int) -> int:
    """Escalates repeat flood-mutes within a rolling 7-day window, capped at 8x the base duration."""
    window = 7 * 86400
    now_ts = int(time.time())
    con = _db()
    row = con.execute(
        "SELECT count, last_ts FROM mute_history WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    if row and row[1] and now_ts - row[1] <= window:
        count = row[0] + 1
    else:
        count = 1
    con.execute(
        "INSERT INTO mute_history(chat_id, user_id, count, last_ts) VALUES(?,?,?,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET count=excluded.count, last_ts=excluded.last_ts",
        (chat_id, user_id, count, now_ts),
    )
    con.commit()
    con.close()
    multiplier = min(count, 8)
    return base_seconds * multiplier


def _audit(chat_id: int, mod_id: int, target_id: int, action: str, reason: str = "") -> None:
    """Permanent record of every mod action, for /auditlog and /gmodstats. Never blocks the caller."""
    try:
        con = _db()
        con.execute(
            "INSERT INTO audit_log(chat_id, mod_id, target_id, action, reason, ts) "
            "VALUES(?,?,?,?,?,strftime('%s','now'))",
            (chat_id, mod_id, target_id, action, reason),
        )
        con.commit()
        con.close()
    except Exception as exc:
        log.warning("audit log %s", exc)


def _config_audit(chat_id: int, admin_id: int, action: str, detail: str = "") -> None:
    """Separate trail for settings/config changes, as opposed to actions taken on members. Useful
    when several admins share the bot and something's config drifts unexpectedly."""
    try:
        con = _db()
        con.execute(
            "INSERT INTO config_audit_log(chat_id, admin_id, action, detail, ts) "
            "VALUES(?,?,?,?,strftime('%s','now'))",
            (chat_id, admin_id, action, detail),
        )
        con.commit()
        con.close()
    except Exception as exc:
        log.warning("config audit log %s", exc)


def _cmd_tier(chat_id: int, command: str, default_tier: str) -> str:
    con = _db()
    row = con.execute(
        "SELECT tier FROM command_perms WHERE chat_id=? AND command=?", (chat_id, command)
    ).fetchone()
    con.close()
    return row[0] if row and row[0] in ("mod", "admin") else default_tier


async def _check_cmd_perm(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str, default_tier: str) -> bool:
    """Lets an admin reassign which tier (mod vs full admin) can run a given command, via /gsetperm.
    Falls back to default_tier when nothing's configured."""
    tier = _cmd_tier(update.effective_chat.id, command, default_tier)
    if tier == "mod":
        return await _is_mod(update, context)
    return await _is_admin(update, context)


def _raid_top_invite_link(chat_id: int, since_ts: float) -> tuple[str, str, int] | None:
    """Returns (invite_link, link_name, count) for the invite link most joins in this raid
    window came through, or None if no joins in the window carried an invite link."""
    con = _db()
    row = con.execute(
        "SELECT invite_link, link_name, COUNT(*) AS c FROM join_invite_links "
        "WHERE chat_id=? AND ts>=? AND invite_link IS NOT NULL AND invite_link!='' "
        "GROUP BY invite_link ORDER BY c DESC LIMIT 1",
        (chat_id, int(since_ts)),
    ).fetchone()
    con.close()
    if not row:
        return None
    return (row[0], row[1], row[2])


ADMIN_SPREE_THRESHOLD = 8
ADMIN_SPREE_WINDOW_SECONDS = 120
_admin_action_times: dict[tuple[int, int], deque] = defaultdict(deque)


async def _check_admin_spree(context: ContextTypes.DEFAULT_TYPE, chat_id: int, mod_id: int) -> None:
    """A single admin banning/muting/kicking an abnormal number of members in a short window is the
    signature of a compromised admin account gone rogue, not a bad day at moderation. Strip their
    restrict/ban ability the moment it trips, and tell the owner — a false positive costs an admin a
    few minutes of reduced power; a missed real one costs the group its membership."""
    if mod_id in OWNER_IDS:
        return
    con = _db()
    already = con.execute(
        "SELECT 1 FROM admin_spree_lock WHERE chat_id=? AND user_id=?", (chat_id, mod_id)
    ).fetchone()
    con.close()
    if already:
        return
    key = (chat_id, mod_id)
    now_ts = time.time()
    dq = _admin_action_times[key]
    dq.append(now_ts)
    while dq and now_ts - dq[0] > ADMIN_SPREE_WINDOW_SECONDS:
        dq.popleft()
    if len(dq) < ADMIN_SPREE_THRESHOLD:
        return
    try:
        await context.bot.promote_chat_member(chat_id, mod_id, can_restrict_members=False)
    except Exception as exc:
        log.warning("spree lock promote %s", exc)
        return
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO admin_spree_lock(chat_id, user_id, ts) VALUES(?,?,strftime('%s','now'))",
        (chat_id, mod_id),
    )
    con.commit()
    con.close()
    _audit(chat_id, 0, mod_id, "spree_lock", f"{len(dq)} destructive actions in {ADMIN_SPREE_WINDOW_SECONDS}s")
    await _log(
        context,
        f"\U0001F6A8 <b>Anti-nuke:</b> {mod_id} made {len(dq)} bans/mutes/kicks in "
        f"{ADMIN_SPREE_WINDOW_SECONDS}s in {chat_id} — restrict/ban rights auto-revoked. "
        f"An owner or another admin can restore with /gunlockadmin {mod_id}.",
        chat_id,
    )
    for admin in await _admin_users(context, chat_id):
        if admin.id == mod_id:
            continue
        try:
            await context.bot.send_message(
                admin.id,
                f"🚨 Anti-nuke tripped in a Ferzan group: admin {mod_id} made {len(dq)} "
                f"bans/mutes/kicks in under {ADMIN_SPREE_WINDOW_SECONDS}s and had restrict/ban "
                f"rights auto-revoked there. Could be a compromised account — check it out.",
            )
        except Exception:
            pass


def _track_mute(chat_id: int, user_id: int, until_ts: int) -> None:
    con = _db()
    con.execute(
        "INSERT INTO current_mutes(chat_id, user_id, until_ts) VALUES(?,?,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET until_ts=excluded.until_ts",
        (chat_id, user_id, until_ts),
    )
    con.commit()
    con.close()


async def _notify_mute(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, until_ts: int | None, reason: str
) -> None:
    """DMs a muted user why and for how long, with an Appeal button. Most users never think to check
    /mystats on their own, so this reaches them proactively. Silently no-ops if they've never started
    a DM with the bot — that's the normal case, not an error worth logging loudly."""
    try:
        chat = await context.bot.get_chat(chat_id)
        chat_name = chat.title or str(chat_id)
    except Exception:
        chat_name = str(chat_id)
    duration = f"about {max(1, int((until_ts - time.time()) // 60))} min" if until_ts else "indefinitely"
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📣 Appeal to mods", callback_data=f"ap:{chat_id}:{user_id}")]]
    )
    try:
        await context.bot.send_message(
            user_id,
            f"🔇 You were muted in <b>{_esc(chat_name)}</b> — {duration}.\n"
            f"Reason: {_esc(reason)}\n\nThink it's a mistake? Tap below to notify a mod.",
            parse_mode="HTML",
            reply_markup=kb,
        )
    except Exception:
        pass  # they haven't started a DM with the bot — expected, not an error


async def appeal_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        _, chat_id_s, user_id_s = (q.data or "").split(":")
        chat_id, user_id = int(chat_id_s), int(user_id_s)
    except Exception:
        await q.answer()
        return
    if q.from_user.id != user_id:
        await q.answer()
        return
    try:
        target = await context.bot.get_chat_member(chat_id, user_id)
        name = target.user.full_name
    except Exception:
        name = str(user_id)
    await _log(
        context,
        f"📣 <b>Mute appeal</b> — {_esc(name)} (<code>{user_id}</code>) is appealing their mute in "
        f"{chat_id}. Use /gunmute (reply to them) if you agree.",
        chat_id,
    )
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await q.answer("Sent to the mods.")


def _untrack_mute(chat_id: int, user_id: int) -> None:
    con = _db()
    con.execute("DELETE FROM current_mutes WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    con.commit()
    con.close()


def _track_ban(chat_id: int, user_id: int) -> None:
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO chat_bans(chat_id, user_id, ts) VALUES(?,?,strftime('%s','now'))",
        (chat_id, user_id),
    )
    con.commit()
    con.close()


def _untrack_ban(chat_id: int, user_id: int) -> None:
    con = _db()
    con.execute("DELETE FROM chat_bans WHERE chat_id=? AND user_id=?", (chat_id, user_id))
    con.commit()
    con.close()


def _sticker_blocked(chat_id: int, key: str) -> bool:
    con = _db()
    row = con.execute(
        "SELECT 1 FROM sticker_blocklist WHERE chat_id=? AND set_name=?", (chat_id, key)
    ).fetchone()
    con.close()
    return bool(row)


def _linkscan_enabled(chat_id: int) -> bool:
    con = _db()
    row = con.execute("SELECT linkscan_enabled FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return bool(row[0]) if row and row[0] is not None else False


def _rules_gate_enabled(chat_id: int) -> bool:
    con = _db()
    row = con.execute("SELECT rules_gate_enabled FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return bool(row[0]) if row and row[0] is not None else False


def _adaptive_slowmode_enabled(chat_id: int) -> bool:
    con = _db()
    row = con.execute("SELECT adaptive_slowmode_enabled FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return bool(row[0]) if row and row[0] is not None else False


def _quiet_hours_settings(chat_id: int) -> tuple[bool, int | None, int | None]:
    con = _db()
    row = con.execute(
        "SELECT quiet_hours_enabled, quiet_hours_start, quiet_hours_end FROM settings WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    con.close()
    if not row:
        return False, None, None
    return bool(row[0]), row[1], row[2]


def _in_quiet_hours(chat_id: int) -> bool:
    enabled, start_h, end_h = _quiet_hours_settings(chat_id)
    if not enabled or start_h is None or end_h is None:
        return False
    tz = _tz_for_chat(chat_id)
    now_local = _dt.now(tz)
    h = now_local.hour
    if start_h == end_h:
        return False
    if start_h < end_h:
        return start_h <= h < end_h
    return h >= start_h or h < end_h  # wraps past midnight, e.g. 22 -> 6


LINKSCAN_CACHE_TTL = 86400


def _linkscan_check(domain: str) -> bool:
    """Checks a domain against abuse.ch URLhaus (free, no key). Cached 24h. Fails open on any error
    or timeout — a scan outage should never block legitimate links from posting."""
    domain = (domain or "").strip().lower()
    if not domain:
        return False
    now_ts = int(time.time())
    con = _db()
    row = con.execute("SELECT verdict, ts FROM link_scan_cache WHERE domain=?", (domain,)).fetchone()
    if row and now_ts - row[1] < LINKSCAN_CACHE_TTL:
        con.close()
        return row[0] == "bad"
    verdict = "ok"
    try:
        req = urllib.request.Request(
            "https://urlhaus-api.abuse.ch/v1/host/",
            data=f"host={domain}".encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=4) as resp:
            payload = json.loads(resp.read().decode())
        if payload.get("query_status") == "ok" and int(payload.get("url_count") or 0) > 0:
            verdict = "bad"
    except Exception as exc:
        log.warning("linkscan %s", exc)
        con.close()
        return False
    con.execute(
        "INSERT INTO link_scan_cache(domain, verdict, ts) VALUES(?,?,?) "
        "ON CONFLICT(domain) DO UPDATE SET verdict=excluded.verdict, ts=excluded.ts",
        (domain, verdict, now_ts),
    )
    con.commit()
    con.close()
    return verdict == "bad"


def _welcome_variants(chat_id: int) -> list[str]:
    con = _db()
    rows = con.execute("SELECT text FROM welcome_variants WHERE chat_id=? ORDER BY id", (chat_id,)).fetchall()
    con.close()
    return [r[0] for r in rows]


def _newacct_settings(chat_id: int) -> tuple[bool, int]:
    con = _db()
    row = con.execute(
        "SELECT newacct_enabled, newacct_min_id FROM settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    con.close()
    if row is None:
        return False, NEWACCT_DEFAULT_MIN_ID
    enabled = bool(row[0])
    min_id = int(row[1]) if row[1] else NEWACCT_DEFAULT_MIN_ID
    return enabled, min_id


def _is_shadowbanned(chat_id: int, user_id: int) -> bool:
    con = _db()
    row = con.execute(
        "SELECT 1 FROM shadowbanned WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    ).fetchone()
    con.close()
    return bool(row)


# ---- Federated trust — a clean record across Ferzan groups earns a lighter touch ----

TRUST_DAYS = 30
_trust_touch_cache: dict[int, float] = {}


def _touch_trust(user_id: int) -> None:
    now_ts = int(time.time())
    con = _db()
    con.execute(
        "INSERT INTO user_trust(user_id, first_seen, last_seen, groups_seen, strikes_ever) "
        "VALUES(?,?,?,1,0) ON CONFLICT(user_id) DO UPDATE SET last_seen=excluded.last_seen",
        (user_id, now_ts, now_ts),
    )
    con.commit()
    con.close()


def _maybe_touch_trust(user_id: int) -> None:
    """Throttled so a busy chat doesn't hit the DB on every single message."""
    now = time.time()
    last = _trust_touch_cache.get(user_id, 0)
    if now - last < 21600:
        return
    _trust_touch_cache[user_id] = now
    _touch_trust(user_id)


def _mark_strike_trust(user_id: int) -> None:
    now_ts = int(time.time())
    con = _db()
    con.execute(
        "INSERT INTO user_trust(user_id, first_seen, last_seen, groups_seen, strikes_ever) "
        "VALUES(?,?,?,1,1) ON CONFLICT(user_id) DO UPDATE SET "
        "strikes_ever = strikes_ever + 1, last_seen=excluded.last_seen",
        (user_id, now_ts, now_ts),
    )
    con.commit()
    con.close()


def _is_trusted(user_id: int) -> bool:
    con = _db()
    row = con.execute(
        "SELECT first_seen, strikes_ever FROM user_trust WHERE user_id=?", (user_id,)
    ).fetchone()
    con.close()
    if not row:
        return False
    first_seen, strikes_ever = row
    return strikes_ever == 0 and (int(time.time()) - (first_seen or 0)) >= TRUST_DAYS * 86400


def _local_scam_list(chat_id: int) -> list:
    cache = _local_scam_cache[chat_id]
    now = time.time()
    if now - cache["ts"] > SCAM_CACHE_TTL:
        con = _db()
        cache["items"] = con.execute(
            "SELECT value, kind FROM local_scam WHERE chat_id=?", (chat_id,)
        ).fetchall()
        con.close()
        cache["ts"] = now
    return cache["items"]


def _welcome_btn(chat_id: int) -> tuple[str | None, str | None]:
    con = _db()
    row = con.execute(
        "SELECT welcome_btn_label, welcome_btn_url FROM settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    con.close()
    if not row:
        return None, None
    return row[0], row[1]


_whitelist_cache: dict[int, dict] = defaultdict(lambda: {"ts": 0.0, "items": []})


def _link_whitelist(chat_id: int) -> list[str]:
    cache = _whitelist_cache[chat_id]
    now = time.time()
    if now - cache["ts"] > SCAM_CACHE_TTL:
        con = _db()
        cache["items"] = [
            r[0] for r in con.execute("SELECT domain FROM link_whitelist WHERE chat_id=?", (chat_id,)).fetchall()
        ]
        con.close()
        cache["ts"] = now
    return cache["items"]


def _strip_whitelisted_links(chat_id: int, text: str) -> str:
    """Removes any whitelisted domain/path substrings so LINK_RE no longer flags them."""
    for domain in _link_whitelist(chat_id):
        if domain:
            text = re.sub(re.escape(domain), " ", text, flags=re.I)
    return text


# ---- Quick-action buttons on mod alerts ----


def _qa_kb(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🔨 Ban", callback_data=f"qa:ban:{chat_id}:{user_id}"),
                InlineKeyboardButton("🔓 Unmute", callback_data=f"qa:unmute:{chat_id}:{user_id}"),
                InlineKeyboardButton("✅ Dismiss", callback_data=f"qa:dismiss:{chat_id}:{user_id}"),
            ]
        ]
    )


def _qa_unban_kb(chat_id: int, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("↩️ Unban", callback_data=f"qa:unban:{chat_id}:{user_id}")]]
    )


async def quickaction_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        _, action, chat_id_s, user_id_s = (q.data or "").split(":")
        chat_id, user_id = int(chat_id_s), int(user_id_s)
    except Exception:
        await q.answer()
        return
    try:
        clicker = await context.bot.get_chat_member(chat_id, q.from_user.id)
        if clicker.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await q.answer("Admins of that group only.", show_alert=True)
            return
    except Exception:
        await q.answer("Couldn't verify admin status there.", show_alert=True)
        return

    result = None
    if action == "ban":
        try:
            await context.bot.ban_chat_member(chat_id, user_id)
        except Exception as exc:
            log.warning("qa ban %s", exc)
        con = _db()
        con.execute(
            "INSERT OR REPLACE INTO global_bans(user_id, reason, by_id, ts) VALUES(?,?,?,strftime('%s','now'))",
            (user_id, "quick-action ban", q.from_user.id),
        )
        con.commit()
        con.close()
        _track_ban(chat_id, user_id)
        _audit(chat_id, q.from_user.id, user_id, "ban", "quick-action")
        await _check_admin_spree(context, chat_id, q.from_user.id)
        result = "🔨 Banned"
    elif action == "unmute":
        try:
            chat = await context.bot.get_chat(chat_id)
            perms = chat.permissions or ChatPermissions(can_send_messages=True)
            await context.bot.restrict_chat_member(chat_id, user_id, perms)
        except Exception as exc:
            log.warning("qa unmute %s", exc)
        _untrack_mute(chat_id, user_id)
        _audit(chat_id, q.from_user.id, user_id, "unmute", "quick-action")
        result = "🔓 Unmuted"
    elif action == "dismiss":
        con = _db()
        con.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id))
        con.commit()
        con.close()
        try:
            chat = await context.bot.get_chat(chat_id)
            perms = chat.permissions or ChatPermissions(can_send_messages=True)
            await context.bot.restrict_chat_member(chat_id, user_id, perms)
        except Exception:
            pass
        _untrack_mute(chat_id, user_id)
        _audit(chat_id, q.from_user.id, user_id, "dismiss", "quick-action")
        result = "✅ Dismissed — strikes cleared"
    elif action == "unban":
        try:
            await context.bot.unban_chat_member(chat_id, user_id)
        except Exception as exc:
            log.warning("qa unban %s", exc)
        con = _db()
        con.execute("DELETE FROM global_bans WHERE user_id=?", (user_id,))
        con.commit()
        con.close()
        _untrack_ban(chat_id, user_id)
        _audit(chat_id, q.from_user.id, user_id, "unban", "quick-action")
        result = "↩️ Unbanned"
    else:
        await q.answer()
        return

    await q.answer(result)
    try:
        base = q.message.text_html or q.message.text or ""
        await q.edit_message_text(
            f"{base}\n\n— {result} by {q.from_user.mention_html()}.", parse_mode="HTML"
        )
    except Exception as exc:
        log.warning("qa edit %s", exc)


# ---- Fuzzy phrase matching (catches l33t/spacing evasions of blocked phrases) ----


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * lb
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


_WORD_RE = re.compile(r"[a-z0-9]+")


def _fuzzy_hit(text: str, phrase: str) -> bool:
    """True if some run of words in `text` is a close (edit-distance) match for `phrase`."""
    p_words = phrase.lower().split()
    n = len(p_words)
    if n == 0 or n > 4:
        return False
    words = _WORD_RE.findall(text.lower())[:80]
    if len(words) < n:
        return False
    target = "".join(p_words)
    threshold = max(1, len(target) // 5)
    for i in range(len(words) - n + 1):
        candidate = "".join(words[i : i + n])
        if abs(len(candidate) - len(target)) > threshold:
            continue
        if _levenshtein(candidate, target) <= threshold:
            return True
    return False


# ---- Cross-chat duplicate / coordinated spam detection ----

DUP_WINDOW_SECONDS = 60
DUP_USER_THRESHOLD = 4
DUP_CHAT_THRESHOLD = 3
_dup_tracker: dict[str, deque] = defaultdict(deque)
_dup_last_prune = 0.0


def _prune_dup_tracker(now_ts: float) -> None:
    global _dup_last_prune
    if now_ts - _dup_last_prune < 300:
        return
    _dup_last_prune = now_ts
    dead = []
    for key, dq in _dup_tracker.items():
        while dq and now_ts - dq[0][0] > DUP_WINDOW_SECONDS:
            dq.popleft()
        if not dq:
            dead.append(key)
    for key in dead:
        _dup_tracker.pop(key, None)


def _check_duplicate_spam(chat_id: int, user_id: int, text: str, now_ts: float) -> str | None:
    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    if len(norm) < 8:
        return None
    _prune_dup_tracker(now_ts)
    key = norm[:300]
    dq = _dup_tracker[key]
    dq.append((now_ts, chat_id, user_id))
    while dq and now_ts - dq[0][0] > DUP_WINDOW_SECONDS:
        dq.popleft()
    users_here = {u for ts, c, u in dq if c == chat_id}
    chats_hit = {c for ts, c, u in dq}
    if len(users_here) >= DUP_USER_THRESHOLD:
        return f"same message from {len(users_here)} different accounts in {DUP_WINDOW_SECONDS}s"
    if len(chats_hit) >= DUP_CHAT_THRESHOLD:
        return f"same message posted across {len(chats_hit)} groups in {DUP_WINDOW_SECONDS}s"
    return None


# ---- Federated scam intel — auto-promotes repeatedly-reported CAs/domains ecosystem-wide ----

FEDERATION_THRESHOLD = 3
CA_RE = re.compile(r"\b(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})\b")
DOMAIN_RE = re.compile(r"\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|xyz|app|so|fun|gg|me|co|finance|vip)\b", re.I)


def _extract_scam_candidates(text: str) -> list[tuple[str, str]]:
    out = []
    for m in CA_RE.findall(text or ""):
        out.append((m.lower(), "ca"))
    for m in DOMAIN_RE.findall(text or ""):
        out.append((m.lower(), "domain"))
    return out


# ---- URL-shortener unwinding — scammers hide phishing links behind these specifically to dodge
# domain blocklists, so a shortened link gets resolved before it's checked against the scam list ----

SHORTENER_DOMAINS = {
    "bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "buff.ly",
    "cutt.ly", "rebrand.ly", "shorturl.at", "tiny.cc", "rb.gy", "s.id", "v.gd", "bl.ink",
}
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


def _url_host(url: str) -> str:
    host = re.sub(r"^https?://", "", url, flags=re.I).split("/")[0].split("?")[0].lower()
    return host.split("@")[-1]  # strip any userinfo@ prefix


def _resolve_shortlink_sync(url: str) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (FerzanGuardian)"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            return resp.geturl()
    except Exception:
        return None


async def _resolve_shortlinks(text: str) -> list[str]:
    """Finds any known-shortener URLs in `text` and resolves where they actually go. Bounded to a
    couple of links per message and a short timeout each, so one slow link can't stall moderation."""
    urls = URL_RE.findall(text or "")
    targets = [u for u in urls if _url_host(u) in SHORTENER_DOMAINS][:2]
    if not targets:
        return []
    loop = asyncio.get_event_loop()
    resolved = []
    for u in targets:
        try:
            final = await asyncio.wait_for(loop.run_in_executor(None, _resolve_shortlink_sync, u), timeout=7)
        except Exception:
            final = None
        if final:
            resolved.append(final)
    return resolved


def _federate_report(value: str, kind: str, chat_id: int) -> bool:
    """Records that `chat_id` flagged `value`; auto-promotes to the ecosystem-wide scam list
    once enough distinct groups have reported the same value. Returns True if just promoted."""
    if not value:
        return False
    con = _db()
    con.execute(
        "INSERT OR IGNORE INTO scam_reports(value, kind, chat_id, ts) VALUES(?,?,?,strftime('%s','now'))",
        (value, kind, chat_id),
    )
    con.commit()
    distinct = con.execute(
        "SELECT COUNT(DISTINCT chat_id) FROM scam_reports WHERE value=? AND kind=?", (value, kind)
    ).fetchone()[0]
    already = con.execute("SELECT 1 FROM scam_list WHERE value=?", (value,)).fetchone()
    promoted = False
    if distinct >= FEDERATION_THRESHOLD and not already:
        con.execute(
            "INSERT OR REPLACE INTO scam_list(value, kind, added_by, ts) VALUES(?,?,0,strftime('%s','now'))",
            (value, kind),
        )
        con.commit()
        _scam_cache["ts"] = 0
        promoted = True
    con.close()
    return promoted


# ---- OCR on images (best-effort; no-op if pytesseract/tesseract isn't installed) ----


async def _ocr_photo(context: ContextTypes.DEFAULT_TYPE, msg) -> str:
    if not _OCR_AVAILABLE or not msg.photo:
        return ""
    try:
        photo = msg.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        buf.seek(0)
        img = Image.open(buf)
        loop = asyncio.get_event_loop()
        text = await asyncio.wait_for(
            loop.run_in_executor(None, pytesseract.image_to_string, img), timeout=8
        )
        return text or ""
    except Exception as exc:
        log.warning("ocr %s", exc)
        return ""


def _scam_list() -> list:
    now = time.time()
    if now - _scam_cache["ts"] > SCAM_CACHE_TTL:
        con = _db()
        _scam_cache["items"] = con.execute("SELECT value, kind FROM scam_list").fetchall()
        con.close()
        _scam_cache["ts"] = now
    return _scam_cache["items"]


def _touch_chat(chat_id: int, title: str) -> None:
    con = _db()
    con.execute(
        "INSERT INTO known_chats(chat_id, title, last_seen) VALUES(?,?,strftime('%s','now')) "
        "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, last_seen=excluded.last_seen",
        (chat_id, title or ""),
    )
    con.commit()
    con.close()


def _bump_msg_count(chat_id: int) -> None:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    con = _db()
    con.execute(
        "INSERT INTO msg_counts(chat_id, day, count) VALUES(?,?,1) "
        "ON CONFLICT(chat_id, day) DO UPDATE SET count = count + 1",
        (chat_id, day),
    )
    con.commit()
    con.close()


def _warn_settings(chat_id: int) -> tuple[int, int]:
    """Returns (warn_limit, warn_decay_days) for a chat, falling back to defaults."""
    con = _db()
    row = con.execute(
        "SELECT warn_limit, warn_decay_days FROM settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    con.close()
    limit = WARN_LIMIT if row is None or row[0] is None else int(row[0])
    decay = 0 if row is None or row[1] is None else int(row[1])
    return limit, decay


async def _strike(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, reason: str) -> None:
    _mark_strike_trust(user_id)
    limit, decay_days = _warn_settings(chat_id)
    now_ts = int(time.time())
    con = _db()
    if decay_days > 0:
        row = con.execute(
            "SELECT count, last_ts FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id)
        ).fetchone()
        if row and row[1] and now_ts - row[1] > decay_days * 86400:
            # Strikes decayed — start this one fresh instead of piling onto stale strikes.
            con.execute(
                "INSERT INTO warns(chat_id, user_id, count, last_ts) VALUES(?,?,1,?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET count=1, last_ts=excluded.last_ts",
                (chat_id, user_id, now_ts),
            )
            con.commit()
            con.close()
            await _log(
                context,
                f"⚠️ Strike 1/{limit} for {user_id} in {chat_id} — {_esc(reason)}.",
                chat_id,
                _qa_kb(chat_id, user_id),
            )
            return
    con.execute(
        "INSERT INTO warns(chat_id, user_id, count, last_ts) VALUES(?,?,1,?) "
        "ON CONFLICT(chat_id, user_id) DO UPDATE SET count = count + 1, last_ts=excluded.last_ts",
        (chat_id, user_id, now_ts),
    )
    row = con.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    count = row[0] if row else 1
    if count >= limit:
        con.execute(
            "INSERT OR REPLACE INTO global_bans(user_id, reason, by_id, ts) VALUES(?,?,?,strftime('%s','now'))",
            (user_id, reason, 0),
        )
    con.commit()
    con.close()
    if count >= limit:
        try:
            await context.bot.ban_chat_member(chat_id, user_id)
        except Exception as exc:
            log.warning("strike ban %s", exc)
        await _log(
            context,
            f"\U0001F528 Auto-banned {user_id} in {chat_id} — {limit} strikes ({_esc(reason)}).",
            chat_id,
            _qa_unban_kb(chat_id, user_id),
        )
    else:
        await _log(
            context,
            f"⚠️ Strike {count}/{limit} for {user_id} in {chat_id} — {_esc(reason)}.",
            chat_id,
            _qa_kb(chat_id, user_id),
        )


async def _delete_later(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    try:
        await context.bot.delete_message(data["chat_id"], data["message_id"])
    except Exception:
        pass


async def _captcha_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    chat_id, user_id, msg_id = data["chat_id"], data["user_id"], data["msg_id"]
    if _pending_captcha.get((chat_id, user_id)) != msg_id:
        return
    _pending_captcha.pop((chat_id, user_id), None)
    _captcha_answers.pop((chat_id, user_id), None)
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)
    except Exception as exc:
        log.warning("captcha kick %s", exc)
    try:
        await context.bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def captcha_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    parts = (q.data or "").split(":")
    try:
        chat_id, user_id = int(parts[1]), int(parts[2])
    except Exception:
        await q.answer()
        return
    if q.from_user.id != user_id:
        await q.answer("This isn't your verification.", show_alert=True)
        return
    if len(parts) >= 4:
        # Math-mode captcha — verify the tapped answer before unlocking.
        try:
            chosen = int(parts[3])
        except ValueError:
            chosen = None
        correct = _captcha_answers.get((chat_id, user_id))
        if correct is None or chosen != correct:
            await q.answer("Wrong answer — try again.", show_alert=True)
            return
        _captcha_answers.pop((chat_id, user_id), None)
    _pending_captcha.pop((chat_id, user_id), None)
    try:
        chat = await context.bot.get_chat(chat_id)
        perms = chat.permissions or ChatPermissions(can_send_messages=True)
        await context.bot.restrict_chat_member(chat_id, user_id, perms)
    except Exception as exc:
        log.warning("captcha unmute %s", exc)
    try:
        await q.message.delete()
    except Exception:
        pass
    await q.answer("Verified ✅")


async def _rules_gate_timeout(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    chat_id, user_id, msg_id = data["chat_id"], data["user_id"], data["msg_id"]
    if _pending_rules.get((chat_id, user_id)) != msg_id:
        return
    _pending_rules.pop((chat_id, user_id), None)
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)
    except Exception as exc:
        log.warning("rules gate kick %s", exc)
    try:
        await context.bot.delete_message(chat_id, msg_id)
    except Exception:
        pass


async def rules_gate_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        _, chat_id_s, user_id_s = (q.data or "").split(":")
        chat_id, user_id = int(chat_id_s), int(user_id_s)
    except Exception:
        await q.answer()
        return
    if q.from_user.id != user_id:
        await q.answer("This isn't your gate.", show_alert=True)
        return
    _pending_rules.pop((chat_id, user_id), None)
    try:
        chat = await context.bot.get_chat(chat_id)
        perms = chat.permissions or ChatPermissions(can_send_messages=True)
        await context.bot.restrict_chat_member(chat_id, user_id, perms)
    except Exception as exc:
        log.warning("rules gate unmute %s", exc)
    try:
        await q.message.delete()
    except Exception:
        pass
    await q.answer("Welcome in ✅")


def _start_text() -> str:
    return (
        "🛡 <b>Welcome to Ferzan Guardian</b>\n"
        "<i>Community shield for any Telegram project</i>\n"
        "━━━━━━━━━━━━━━━\n\n"
        "New here? Tap <b>🚀 Quick Start Guide</b> below to get protected in under a minute.\n\n"
        "🚫 <b>Impersonator block</b> — fake admin/owner/dev/support usernames auto-banned\n"
        "🎭 <b>Look-alike detection</b> — names copying your real admins get removed on join\n"
        "🔇 <b>Scam word filter</b> — blacklisted phrases deleted + the sender muted\n"
        "🌊 <b>Antiflood & slow mode</b> — spam bursts throttled automatically\n"
        "🚨 <b>Anti-raid</b> — join floods trigger an automatic lockdown\n"
        "🔗 <b>Cross-group ban</b> — banned in one shielded chat, blocked in every other\n\n"
        "👀 See it.   🦍 Ape it.   🚀 Send it."
    )


def _start_kb() -> InlineKeyboardMarkup:
    add = f"https://t.me/{ME}?startgroup=true"
    rows = [
        [InlineKeyboardButton("🤖 ADD BOT TO GROUP", url=add)],
        [InlineKeyboardButton("🚀 Quick Start Guide", callback_data="gm:quickstart")],
    ]
    # Guardian's own category buttons first, since this is the Guardian Bot card.
    rows.extend(_menu_kb("main").inline_keyboard)
    # Ecosystem cross-promo links below.
    rows.extend(
        [
            [
                InlineKeyboardButton("👑 Community", url=CHAT),
                InlineKeyboardButton("🤝 Support", url=CHAT),
            ],
            [InlineKeyboardButton("⚡ Ferzan Trade", url=f"https://t.me/{TRADE}")],
            [InlineKeyboardButton("🟢 Ferzan Buy", url=f"https://t.me/{TRADE}")],
            [InlineKeyboardButton("💧 Ferzan Liq", url=f"https://t.me/{LIQ}")],
            [InlineKeyboardButton("𝕏 Follow Ferzan", url=X_URL)],
        ]
    )
    return InlineKeyboardMarkup(rows)


async def _send_banner_card(
    update: Update, context: ContextTypes.DEFAULT_TYPE, caption: str, kb: InlineKeyboardMarkup
) -> None:
    """Send caption+kb as a photo card on the banner when available, same pattern as Trending. Falls back to plain text."""
    if BANNER.exists():
        try:
            with open(BANNER, "rb") as fh:
                await update.effective_message.reply_photo(
                    fh, caption=caption, parse_mode="HTML", reply_markup=kb
                )
            return
        except Exception as exc:
            log.warning("banner send %s", exc)
    await update.effective_message.reply_text(caption, parse_mode="HTML", reply_markup=kb)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_banner_card(update, context, _start_text(), _start_kb())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Always show the full category menu — /start is the welcome card, /help is the menu.
    await _send_banner_card(update, context, _menu_text("main"), _menu_kb("main"))


MENU_SECTIONS: dict[str, tuple[str, str]] = {
    "settings": (
        "⚙️ Settings Panel",
        "⚙️ <b>Live Settings Panel</b>\n\n"
        "Flip toggles with a tap — no commands to remember.\n\n"
        "• <code>/gsettings</code> — opens the button panel (welcome, goodbye, Clean Service, "
        "captcha, link lock, and content locks)\n"
        "• <code>/exportconfig</code> — download this group's full setup as a file (now also includes "
        "FAQs and welcome-message variants)\n"
        "• <code>/importconfig</code> (reply to an export file) — apply it here\n"
        "• <code>/cloneconfig chat_id</code> — copy another group's setup into this one "
        "(you must be admin in both)",
    ),
    "moderation": (
        "🛡 Moderation",
        "🛡 <b>Moderation</b>\n\n"
        "Keep bad actors out and repeat offenders gone.\n\n"
        "• <code>/gfilter [warn|mute|ban] word</code> — auto-delete a phrase, with a chosen action\n"
        "• <code>/gunfilter word</code> — remove a blocked phrase\n"
        "• <code>/gfilters</code> — list everything currently blocked\n"
        "• Fuzzy matching runs automatically — catches spelling-dodges like \"fr33 m1nt\" or "
        "\"s e e d  p h r a s e\" on blocked phrases, no setup needed\n"
        "• Mod alerts in your log channel come with one-tap Ban / Unmute / Dismiss buttons\n"
        "• <code>/gwarn</code> (reply) — add a strike\n"
        "• <code>/gwarns</code> (reply) — check a user's strike count\n"
        "• <code>/mystats</code> — anyone can check their own strikes and mute status, no admin needed\n"
        "• <code>/gunwarn</code> (reply) — remove one strike\n"
        "• <code>/gsetwarns N</code> — set how many strikes trigger an auto-ban (default 3)\n"
        "• <code>/gsetdecay N</code> — strikes auto-expire after N days of good behavior (0 = never)\n"
        "• <code>/gmute</code> (reply) <code>[10m|1h|1d]</code> — timeout a user, default 1h\n"
        "• <code>/gunmute</code> (reply) — lift a mute\n"
        "• <code>/gkick</code> (reply) — remove a user without a permanent ban\n"
        "• <code>/gapprove</code> (reply) — exempt a trusted user from auto-mod\n"
        "• <code>/gunapprove</code> (reply) — remove that exemption\n"
        "• <code>/scamadd</code> / <code>/scamdel</code> / <code>/scamlist</code> — this group's own blocked CAs/domains\n"
        "• Federated intel: once 3+ groups flag the same CA/domain (by <code>/scamadd</code> or "
        "auto-detected coordinated spam), it's promoted to the shared ecosystem-wide scam list automatically\n\n"
        "🛡 <b>Guardian mods</b> — a trusted tier below full admin (warn/mute/kick/delete/purge, "
        "nothing else):\n"
        "• <code>/gmodadd</code> (reply, or user_id) — make someone a Guardian mod here\n"
        "• <code>/gmodremove</code> (reply, or user_id) — remove that\n"
        "• <code>/gmods</code> — list this group's Guardian mods\n\n"
        "👻 <b>Shadowban</b> — for trolls who feed on reactions, not bans:\n"
        "• <code>/gshadowban</code> (reply) — their messages vanish for everyone but them, silently\n"
        "• <code>/gunshadowban</code> (reply, or user_id) — lift it\n\n"
        "🍯 <b>Honeypot</b> — a secret trap command only a scraper/bot would ever type:\n"
        "• <code>/sethoneypot secretname</code> — arm it; anyone who runs <code>/secretname</code> "
        "gets instantly banned and the message deleted\n"
        "• <code>/deletehoneypot</code> — disarm it\n\n"
        "🧯 <b>Command cooldowns</b> — <code>/report</code>, <code>/votemute</code>, and "
        "<code>/tr</code> now rate-limit per member automatically, closing off a spam vector on "
        "Guardian's own commands.\n\n"
        "🧹 <b>Mass undo</b> — for when a filter, lock, or lockdown overcorrects:\n"
        "• <code>/gclearmutes</code> — lift every mute Guardian has issued in this group\n"
        "• <code>/gclearbans</code> — lift every ban Guardian has issued in this group\n\n"
        "📜 <b>Audit log</b> — every ban/unban/mute/unmute/warn/kick/resolve is now permanently "
        "recorded with who did it and when:\n"
        "• <code>/auditlog</code> (reply, or a user_id, or nothing for the whole group) — last 20 entries\n"
        "• <code>/exportauditlog</code> — full history as a downloadable CSV, for record-keeping\n\n"
        "⚙️ <b>Config audit log</b> — a separate trail, just for settings/config changes (filters, "
        "scam list, captcha, locks, link lock), so you're not hunting through member-action history "
        "to see who flipped what:\n"
        "• <code>/configauditlog</code> — last 20 config changes, who made them, when\n\n"
        "🎚 <b>Command permission tiers</b> — reassign which tier can run specific moderation "
        "commands, instead of the fixed mod-vs-admin defaults:\n"
        "• <code>/gsetperm</code> — show current overrides\n"
        "• <code>/gsetperm gban mod</code> — let Guardian mods run <code>/gban</code>, not just admins\n"
        "• Customizable: gban, gunban, gmute, gunmute, gkick, glockdown\n\n"
        "🎭 <b>Sticker/GIF blocklist</b> — same idea as the scam list, but for spam sticker packs and GIFs:\n"
        "• <code>/gblocksticker</code> (reply to the sticker or GIF) — block that whole pack/GIF\n"
        "• <code>/gunblocksticker</code> (reply, or pass the blocked name) — unblock it\n"
        "• <code>/gstickerblocklist</code> — list what's blocked here\n\n"
        "📣 <b>Mute appeals</b> — runs automatically. Anyone Guardian mutes (manual, flood, filter, or "
        "vote-to-mute) now gets DMed why and for how long, with an Appeal button that pings your log "
        "channel — if they've never started a DM with the bot, this just quietly no-ops.\n\n"
        "👥 <b>Member export</b> — download who Guardian has seen join this group:\n"
        "• <code>/exportmembers</code> — CSV of recorded joins with strikes and cross-group trust info. "
        "Not a live full member list — Telegram's Bot API has no method for that — this is everyone "
        "Guardian has personally seen join.",
    ),
    "bans": (
        "🚫 Bans",
        "🚫 <b>Bans</b>\n\n"
        "One shared blocklist across every Guardian-shielded chat.\n\n"
        "• <code>/gban</code> (reply or user_id) — ban here and everywhere Guardian runs\n"
        "• <code>/gunban user_id</code> — lift a global ban\n\n"
        "Banned users are auto-removed the moment they try to join any shielded group.\n\n"
        "⚡ <b>Anti-nuke</b> — runs automatically. Guardian now watches admin-role changes too: "
        "you and every other admin get an instant DM (and it's logged) the moment anyone is promoted "
        "or demoted, naming who did it. A compromised owner quietly handing out fake admin is a real "
        "attack pattern — this catches it the moment it happens.\n\n"
        "🚨 <b>Spree lock</b> — also automatic. If a single admin bans/mutes/kicks 8+ members within "
        "2 minutes (a compromised admin account going on a rampage, not a bad moderation day), "
        "Guardian instantly strips their restrict/ban rights in that group and DMs every other admin. "
        "• <code>/gunlockadmin</code> (reply, or user_id) — restore their rights once you've confirmed "
        "it was a false alarm.",
    ),
    "antiflood": (
        "🌊 Antiflood",
        "🌊 <b>Antiflood</b>\n\n"
        "Runs automatically — nothing to configure.\n\n"
        "Guardian watches message rate per user. Too many messages too fast "
        "triggers an instant mute plus a strike, no admin needed.\n\n"
        "• <code>/slowmode 30</code> — cap everyone to one message per 30s (any number of seconds); "
        "<code>/slowmode off</code> to disable\n\n"
        "⏫ <b>Escalating mutes</b> — flood-mute duration now doubles-ish per repeat offense (capped "
        "at 8x), tracked per user over a rolling 7 days, so a serial flooder gets muted longer each "
        "time instead of the same flat duration forever.\n\n"
        "Also watches for the <i>same</i> message coming from several different accounts, or the same "
        "message hitting several Ferzan-shielded groups at once — a classic shill/spam-bot pattern — "
        "and deletes + strikes automatically.\n\n"
        "⚡ <b>Adaptive slowmode</b> (off by default) — auto-enables a short slowmode the moment the "
        "chat's overall message rate spikes, and lifts it automatically once it cools down. Layers on "
        "top of, not instead of, a manually set <code>/slowmode</code>.\n"
        "• <code>/gadaptiveslowmode on|off</code>\n\n"
        "🌙 <b>Quiet hours</b> — a recurring low-traffic window (group-local time) where non-admins "
        "get an automatic light slowmode — handy for groups that get targeted during off-hours.\n"
        "• <code>/gquiethours 2 6</code> — e.g. 2am–6am\n"
        "• <code>/gquiethours off</code>",
    ),
    "antiraid": (
        "🚨 Anti-Raid",
        "🚨 <b>Anti-Raid</b>\n\n"
        "Detects coordinated join floods and locks the door.\n\n"
        "• 8+ joins in 30 seconds → full chat lockdown: new joins get muted, and every message "
        "(not just from new members) is silently deleted for 10 min\n"
        "• All group admins get an instant DM the moment a raid is detected\n"
        "• <code>/graidoff</code> — clear an active raid lock early\n"
        "• <code>/glockdown</code> — trigger the same full lockdown manually, if you spot trouble before "
        "Guardian's own detection does\n\n"
        "🕵️ <b>New-account scrutiny</b> (off by default) — Telegram ids are roughly sequential, so a "
        "very high one means a brand-new account. Catches the slow trickle that doesn't spike fast "
        "enough to trip the raid detector above. Skipped automatically for anyone with a 30+ day "
        "clean record in another Ferzan group.\n"
        "• <code>/gnewacct on|off</code>\n"
        "• <code>/gnewacctid &lt;id&gt;</code> — adjust the cutoff (drifts upward over time)\n\n"
        "👤 <b>No-avatar burst detection</b> — runs automatically, no setup. 3+ accounts with no "
        "profile photo joining within 30 seconds each get held for 30 min, even below the full raid "
        "threshold — a cheap but real signal most raid/scrape bots share.\n\n"
        "🔗 <b>Invite-link attribution</b> — runs automatically. Every join is matched to the invite "
        "link it came through, so a raid alert names the specific link most of the raiders used:\n"
        "• <code>/grevokeinvite</code> — kills the link an active/recent raid was attributed to "
        "(no argument needed if a raid just happened)\n"
        "• <code>/grevokeinvite https://t.me/+abc123</code> — kill a specific link by hand",
    ),
    "locks": (
        "🔒 Locks",
        "🔒 <b>Locks</b>\n\n"
        "Block specific content types from being posted at all.\n\n"
        "• <code>/lock links|forwards|stickers|photos|voice|videonote</code>\n"
        "• <code>/unlock links|forwards|stickers|photos|voice|videonote</code>\n"
        "• <code>/glink on|off</code> — extra: block links from brand-new members for their first 15 minutes\n"
        "• <code>/linkwhitelist add|remove|list</code> — trusted domains that skip the link lock entirely\n"
        "• <code>/gfilter regex:pattern</code> — block anything matching a custom regex, not just plain phrases\n"
        "• Scam text baked into a screenshot gets read too (OCR) and checked against every filter above\n"
        "• Shortened links (bit.ly, tinyurl, etc.) get unwound to their real destination before the "
        "scam-list check runs — closes the trick of hiding a phishing domain behind a shortener\n\n"
        "🔎 <b>Live link safety scan</b> (off by default) — checks any link not already on a scam list "
        "against a public malware/phishing feed (abuse.ch URLhaus, no API key needed), cached 24h per "
        "domain. Fails open — a scan outage never blocks a legitimate link.\n"
        "• <code>/glinkscan on|off</code>",
    ),
    "captcha": (
        "🤖 CAPTCHA",
        "🤖 <b>Join CAPTCHA</b>\n\n"
        "New members must tap a button before they can chat.\n\n"
        "• <code>/gcaptcha on|off</code>\n"
        "• <code>/gcaptcha mode simple|math</code> — simple is one tap, math asks them to solve "
        "\"3 + 5 = ?\" from 4 options, harder for a bot to click through blind\n\n"
        "Anyone who doesn't verify is auto-kicked after 5 minutes. Welcome/goodbye messages also "
        "auto-translate to the joining member's Telegram language when you haven't set a custom one.\n\n"
        "📜 <b>Rules-acceptance gate</b> — an alternative to captcha: new members must tap \"I agree to "
        "the rules\" (shows your <code>/setrules</code> text) before they can post, auto-kicked after "
        "10 min if they don't. If captcha is also on, captcha takes priority and this gate won't "
        "trigger — turn captcha off to use this instead.\n"
        "• <code>/grulesgate on|off</code>",
    ),
    "greetings": (
        "👋 Greetings",
        "👋 <b>Greetings</b>\n\n"
        "Welcome the new, see off the ones who leave.\n\n"
        "• <code>/setwelcome text</code> — supports <code>{first}</code> / <code>{chatname}</code>\n"
        "• <code>/welcome on|off</code>\n"
        "• <code>/setwelcomebtn Label | https://link</code> — add a tappable button to the welcome message\n"
        "• <code>/delwelcomebtn</code> — remove it\n"
        "• <code>/setgoodbye text</code> — supports <code>{first}</code> / <code>{chatname}</code>\n"
        "• <code>/goodbye on|off</code>\n\n"
        "🔀 <b>Welcome rotation</b> — add a few variants and Guardian picks one at random per join, "
        "so it doesn't feel like the same canned line every time:\n"
        "• <code>/addwelcome text</code> — add a variant (same <code>{first}</code>/<code>{chatname}</code> support)\n"
        "• <code>/welcomevariants</code> — list them\n"
        "• <code>/delwelcome id</code> — remove one\n"
        "With no variants added, <code>/setwelcome</code>'s single message is used every time, as before.",
    ),
    "cleanservice": (
        "🧹 Clean Service",
        "🧹 <b>Clean Service</b>\n\n"
        "Keeps the chat tidy by auto-deleting Telegram's own system spam.\n\n"
        "• <code>/cleanservice on|off</code> — removes \"X joined/left the group\" notices\n\n"
        "Pin notices are always auto-cleaned, no toggle needed.",
    ),
    "pinpurge": (
        "📌 Pin & Purge",
        "📌 <b>Pin & Purge</b>\n\n"
        "• <code>/pin</code> (reply) — pin a message\n"
        "• <code>/unpin</code> (reply, or none for the current pin)\n"
        "• <code>/del</code> (reply) — delete one message\n"
        "• <code>/purge</code> (reply) — bulk-delete up to that message (max 300)",
    ),
    "notes": (
        "📝 Notes",
        "📝 <b>Saved Notes</b>\n\n"
        "Store text once, recall it anytime.\n\n"
        "• <code>/save name text</code> (or reply) — save a note\n"
        "• <code>#name</code> — anyone can trigger it in chat\n"
        "• <code>/notes</code> — list saved notes\n"
        "• <code>/delnote name</code> — remove one",
    ),
    "autoreplies": (
        "💬 Auto-Replies",
        "💬 <b>Auto-Replies</b>\n\n"
        "Guardian replies automatically when a keyword is mentioned.\n\n"
        "• <code>/setreply trigger text</code>\n"
        "• <code>/replies</code> — list triggers\n"
        "• <code>/delreply trigger</code> — remove one\n\n"
        "🧠 <b>Smart FAQ</b> — upgraded matching for actual questions (needs a \"?\" and overlapping "
        "keywords, not an exact trigger):\n"
        "• <code>/faqadd question | answer</code> — e.g. <code>/faqadd how do I stake? | Head to "
        "app.example.com/stake and connect your wallet.</code>\n"
        "• <code>/faqlist</code> — list this group's FAQs by id\n"
        "• <code>/faqdel id</code> — remove one\n"
        "Each FAQ only auto-answers a given asker once every few minutes, so it won't spam a busy chat.",
    ),
    "rules": (
        "📜 Rules",
        "📜 <b>Rules</b>\n\n"
        "• <code>/setrules text</code>\n"
        "• <code>/rules</code> — show them",
    ),
    "reports": (
        "🚩 Reports",
        "🚩 <b>Reports</b>\n\n"
        "• <code>/report</code> (reply) — flag a message to the admins instantly, cooldown of "
        "30s per member to stop report-spam\n"
        "Every report is now numbered and queued, with a one-tap ✅ Resolve button on the alert:\n"
        "• <code>/reports</code> — mods: list this group's open reports\n"
        "• <code>/resolve id</code> — mods: close one by id",
    ),
    "info": (
        "👤 Info & Admins",
        "👤 <b>Info & Admins</b>\n\n"
        "• <code>/info</code> (reply, or none for yourself) — join date, strikes, ban status, and now "
        "ecosystem-wide reputation too: how many Ferzan groups they've been seen in, since when, and "
        "whether they're a trusted veteran\n"
        "• <code>/admins</code> — list the chat's admins\n"
        "• <code>/setlogchat chat_id</code> — send this group's ban/strike/raid alerts to a channel of your own "
        "(<code>/setlogchat off</code> to stop)",
    ),
    "stats": (
        "📊 Stats",
        "📊 <b>Stats</b>\n\n"
        "• <code>/gstats</code> — messages today, joins in the last 24h, total active strikes\n"
        "• <code>/ghealth</code> — a quick composite health score (0-100) from message volume, "
        "mod-action rate, strikes, and open reports — for scanning several groups at a glance\n"
        "• <code>/gmodstats</code> — mod action leaderboard for the last 7 days, from the audit log\n"
        "• <code>/gdigest</code> (owner-only) — DM yourself a <code>/ghealth</code>-style score for "
        "every group Guardian is in, worst-scoring first, right now\n"
        "• <code>GUARDIAN_OWNER_DIGEST_HOURS</code> in <code>/opt/ferzan/.env</code> — set to e.g. 24 "
        "to get that same ecosystem-wide digest DMed to every owner automatically on that interval "
        "(off by default)\n"
        "• A weekly digest posts automatically to your log channel every Monday, if one's set "
        "(<code>/setlogchat</code>) — messages, joins, and strikes for the week\n"
        "• <code>GUARDIAN_DB_BACKUP_HOURS</code> in <code>/opt/ferzan/.env</code> — set to e.g. 24 and "
        "you'll get the full database DM'd to you on that interval, as a safety net\n"
        "• <code>GUARDIAN_PHISHING_SYNC_HOURS</code> — set to e.g. 12 to periodically pull a public "
        "crypto-phishing domain feed into the ecosystem scam list (off by default — grows the list a "
        "lot, so opt in deliberately)\n"
        "• Inline lookup — type <code>@FerzanGuardianBot &lt;user_id or CA/domain&gt;</code> in any "
        "chat to check the blocklist without leaving the conversation (needs inline mode turned on "
        "for the bot once, in @BotFather → Bot Settings → Inline Mode)",
    ),
    "community": (
        "🎉 Community",
        "🎉 <b>Community</b>\n\n"
        "🎁 <b>Giveaways</b>\n"
        "• <code>/giveaway 1h Free NFT mint spot</code> — anyone taps 🎉 Enter, one entry each, "
        "Guardian picks a random winner when time's up\n"
        "• <code>/gendgiveaway &lt;id&gt;</code> — end one early\n\n"
        "🗳 <b>Vote-to-mute</b> — a fallback for the hours nobody's an active admin (off by default)\n"
        "• <code>/gvotemute on|off</code>, <code>/gvotemutethreshold N</code> (default 5)\n"
        "• <code>/votemute</code> (reply) — any member can start one; passes once enough distinct "
        "members tap 👍, muting the target for 1h. Can't be used on admins or yourself\n\n"
        "🌐 <b>Translate</b>\n"
        "• <code>/tr [lang]</code> (reply to a message) — translates it inline, defaults to English\n\n"
        "📅 <b>Scheduled announcements</b> — times are this group's local timezone (UTC by default)\n"
        "• <code>/gsettimezone America/New_York</code> — set it once, any IANA name\n"
        "• <code>/schedule daily 09:00 &lt;text&gt;</code>\n"
        "• <code>/schedule weekly mon 09:00 &lt;text&gt;</code>\n"
        "• <code>/schedulelist</code> / <code>/scheduledel &lt;id&gt;</code>",
    ),
}

MENU_ROW_WIDTH = 2


QUICKSTART_TEXT = (
    "🚀 <b>Quick Start</b>\n\n"
    "Get protected in under a minute:\n\n"
    "1️⃣  <b>Add me as admin</b> — tap \"🤖 ADD BOT TO GROUP\" above, then give me "
    "<i>Ban users</i> + <i>Delete messages</i> permissions.\n\n"
    "2️⃣  <b>Turn Group Privacy off</b> — in @BotFather, open my bot → Bot Settings → "
    "Group Privacy → Turn off. This lets me actually see messages to moderate them.\n\n"
    "3️⃣  <b>I'm already protecting you</b> — antiflood, anti-raid, the scam-phrase filter, "
    "and impersonator detection all run automatically, zero setup.\n\n"
    "4️⃣  <b>Make it yours</b> — set a welcome message, house rules, and your strike limit "
    "from the 🛡 Moderation and 👋 Greetings categories below.\n\n"
    "5️⃣  <b>Need help mid-conversation?</b> Just run <code>/help</code> anytime to pull this menu back up.\n\n"
    "👀 See it.   🦍 Ape it.   🚀 Send it."
)


def _menu_text(section: str) -> str:
    if section == "main":
        return (
            "🛡 <b>Ferzan Guardian</b>\n\n"
            "Your group's shield against scams, spam, and raids — most of it runs "
            "automatically, and everything else is one tap away.\n\n"
            "Pick a category 👇"
        )
    if section == "quickstart":
        return QUICKSTART_TEXT
    return MENU_SECTIONS[section][1]


def _menu_kb(section: str) -> InlineKeyboardMarkup:
    if section == "main":
        keys = list(MENU_SECTIONS.items())
        rows = []
        for i in range(0, len(keys), MENU_ROW_WIDTH):
            chunk = keys[i : i + MENU_ROW_WIDTH]
            rows.append(
                [InlineKeyboardButton(label, callback_data=f"gm:{key}") for key, (label, _) in chunk]
            )
        return InlineKeyboardMarkup(rows)
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="gm:main")]])


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    section = (q.data or "").split(":", 1)[-1]
    if section not in ("main", "quickstart") and section not in MENU_SECTIONS:
        await q.answer()
        return
    text, kb = _menu_text(section), _menu_kb(section)
    try:
        if q.message and q.message.photo:
            # The card behind this button is the banner photo — edit its caption, keep the photo.
            await q.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=kb)
        else:
            await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    except Exception as exc:
        log.warning("menu edit %s", exc)
    await q.answer()


async def gmenu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_banner_card(update, context, _menu_text("main"), _menu_kb("main"))


# ---- Live settings panel (button-driven, no commands to type/remember) ----

TOGGLE_FIELDS = {
    "welcome": ("welcome_enabled", "👋 Welcome messages", 1),
    "goodbye": ("goodbye_enabled", "👋 Goodbye messages", 1),
    "cleanservice": ("cleanservice_enabled", "🧹 Clean Service", 1),
    "captcha": ("captcha_enabled", "🔐 Join captcha", 0),
    "antilink": ("antilink", "🔗 New-member link lock", 0),
    "lock_links": ("lock_links", "🚫 Lock: links", 0),
    "lock_forwards": ("lock_forwards", "🚫 Lock: forwards", 0),
    "lock_stickers": ("lock_stickers", "🚫 Lock: stickers", 0),
    "lock_photos": ("lock_photos", "🚫 Lock: photos", 0),
    "lock_voice": ("lock_voice", "🚫 Lock: voice notes", 0),
    "lock_video_note": ("lock_video_note", "🚫 Lock: video notes", 0),
    "newacct": ("newacct_enabled", "🕵️ New-account scrutiny", 0),
}


def _settings_row(chat_id: int) -> dict:
    con = _db()
    cols = [f[0] for f in TOGGLE_FIELDS.values()]
    extra_cols = ["warn_limit", "warn_decay_days", "slowmode_seconds"]
    all_cols = cols + extra_cols
    row = con.execute(
        f"SELECT {', '.join(all_cols)} FROM settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    con.close()
    if not row:
        return {c: None for c in all_cols}
    return dict(zip(all_cols, row))


def _settings_kb(chat_id: int) -> InlineKeyboardMarkup:
    vals = _settings_row(chat_id)
    rows = []
    for key, (col, label, default) in TOGGLE_FIELDS.items():
        raw = vals.get(col)
        on = bool(raw) if raw is not None else bool(default)
        mark = "✅" if on else "⬜️"
        rows.append([InlineKeyboardButton(f"{mark} {label}", callback_data=f"cfg:t:{key}")])
    warn_limit = vals.get("warn_limit") or 3
    decay = vals.get("warn_decay_days") or 0
    slow = vals.get("slowmode_seconds") or 0
    rows.append(
        [InlineKeyboardButton(f"⚠️ Strike limit: {warn_limit} (/gsetwarns)", callback_data="cfg:noop")]
    )
    rows.append(
        [InlineKeyboardButton(
            f"⏳ Strike decay: {decay if decay else 'off'}d (/gsetdecay)", callback_data="cfg:noop"
        )]
    )
    rows.append(
        [InlineKeyboardButton(
            f"🐢 Slow mode: {slow if slow else 'off'}s (/slowmode)", callback_data="cfg:noop"
        )]
    )
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="cfg:refresh")])
    return InlineKeyboardMarkup(rows)


def _settings_text() -> str:
    return (
        "⚙️ <b>Live Settings Panel</b>\n\n"
        "Tap any toggle to flip it instantly — no commands needed.\n"
        "Numbered items link to the command that adjusts their value.\n\n"
        "✅ = on   ⬜️ = off"
    )


async def gsettings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text(
            "Run /gsettings inside your group to open its live settings panel."
        )
        return
    chat_id = update.effective_chat.id
    await update.effective_message.reply_text(
        _settings_text(), parse_mode="HTML", reply_markup=_settings_kb(chat_id)
    )


async def config_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    chat_id = update.effective_chat.id
    if not await _is_admin(update, context):
        await q.answer("Admins only.", show_alert=True)
        return
    data = (q.data or "").split(":", 2)
    action = data[1] if len(data) > 1 else ""
    if action == "t":
        key = data[2] if len(data) > 2 else ""
        field = TOGGLE_FIELDS.get(key)
        if not field:
            await q.answer()
            return
        col, label, default = field
        vals = _settings_row(chat_id)
        raw = vals.get(col)
        current = bool(raw) if raw is not None else bool(default)
        new_val = 0 if current else 1
        con = _db()
        con.execute(
            f"INSERT INTO settings(chat_id, {col}) VALUES(?,?) "
            f"ON CONFLICT(chat_id) DO UPDATE SET {col}=excluded.{col}",
            (chat_id, new_val),
        )
        con.commit()
        con.close()
        try:
            await q.edit_message_text(
                _settings_text(), parse_mode="HTML", reply_markup=_settings_kb(chat_id)
            )
        except Exception as exc:
            log.warning("cfg edit %s", exc)
        await q.answer(f"{label}: {'ON' if new_val else 'OFF'}")
        return
    if action == "refresh":
        try:
            await q.edit_message_text(
                _settings_text(), parse_mode="HTML", reply_markup=_settings_kb(chat_id)
            )
        except Exception as exc:
            log.warning("cfg refresh %s", exc)
        await q.answer("Refreshed.")
        return
    await q.answer()


async def _is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if update.effective_chat.type == "private":
        return True
    msg = update.effective_message
    if msg and msg.sender_chat and msg.sender_chat.id == update.effective_chat.id:
        # Posted anonymously as the group/channel itself — only admins can do that.
        return True
    member = await context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def _is_guardian_mod(chat_id: int, user_id: int) -> bool:
    con = _db()
    row = con.execute("SELECT 1 FROM mods WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
    con.close()
    return bool(row)


async def _is_mod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """True for real Telegram admins, or a user added as a Guardian-only mod (/gmodadd)."""
    if await _is_admin(update, context):
        return True
    return _is_guardian_mod(update.effective_chat.id, update.effective_user.id)


PERM_CUSTOMIZABLE_COMMANDS = ("gban", "gunban", "gmute", "gunmute", "gkick", "glockdown")


async def gsetperm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    args = context.args or []
    if len(args) != 2 or args[0].lower() not in PERM_CUSTOMIZABLE_COMMANDS or args[1].lower() not in ("mod", "admin"):
        con = _db()
        rows = con.execute(
            "SELECT command, tier FROM command_perms WHERE chat_id=?", (update.effective_chat.id,)
        ).fetchall()
        con.close()
        overrides = ", ".join(f"{c}={t}" for c, t in rows) or "(none — all at default tier)"
        await update.effective_message.reply_text(
            f"Usage: /gsetperm <command> mod|admin\n"
            f"Customizable: {', '.join(PERM_CUSTOMIZABLE_COMMANDS)}\n"
            f"Current overrides: {overrides}"
        )
        return
    cmd, tier = args[0].lower(), args[1].lower()
    con = _db()
    con.execute(
        "INSERT INTO command_perms(chat_id, command, tier) VALUES(?,?,?) "
        "ON CONFLICT(chat_id, command) DO UPDATE SET tier=excluded.tier",
        (update.effective_chat.id, cmd, tier),
    )
    con.commit()
    con.close()
    _config_audit(update.effective_chat.id, update.effective_user.id, "set_perm", f"/{cmd} -> {tier}")
    await update.effective_message.reply_text(f"/{cmd} now requires: {tier}")


async def gmodadd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    uid = None
    if update.message and update.message.reply_to_message:
        uid = update.message.reply_to_message.from_user.id
    elif context.args:
        try:
            uid = int(context.args[0])
        except ValueError:
            uid = None
    if not uid:
        await update.effective_message.reply_text(
            "Reply to a user with /gmodadd, or /gmodadd 123456789 — gives them warn/mute/kick/"
            "delete/purge powers here, without full admin access."
        )
        return
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO mods(chat_id, user_id, added_by, ts) VALUES(?,?,?,strftime('%s','now'))",
        (update.effective_chat.id, uid, update.effective_user.id),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"🛡 {uid} is now a Guardian mod in this group.")


async def gmodremove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    uid = None
    if update.message and update.message.reply_to_message:
        uid = update.message.reply_to_message.from_user.id
    elif context.args:
        try:
            uid = int(context.args[0])
        except ValueError:
            uid = None
    if not uid:
        await update.effective_message.reply_text("Reply to a user with /gmodremove, or /gmodremove 123456789")
        return
    con = _db()
    con.execute("DELETE FROM mods WHERE chat_id=? AND user_id=?", (update.effective_chat.id, uid))
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Removed {uid} as a Guardian mod.")


async def gmods_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute(
        "SELECT user_id FROM mods WHERE chat_id=?", (update.effective_chat.id,)
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No Guardian mods set in this group yet.")
        return
    lines = "\n".join(f"• {r[0]}" for r in rows)
    await update.effective_message.reply_text(f"🛡 Guardian mods in this group:\n{lines}")


async def gfilter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    args = list(context.args or [])
    if not args:
        await update.effective_message.reply_text(
            "Usage: /gfilter [warn|mute|ban] free mint drainer\n"
            "Or a pattern: /gfilter [warn|mute|ban] regex:seed\\s*ph?rase"
        )
        return
    action = "mute"
    if args[0].lower() in ("warn", "mute", "ban"):
        action = args[0].lower()
        args = args[1:]
    raw = " ".join(args).strip()
    is_regex = 0
    if raw.lower().startswith("regex:"):
        pattern = raw[len("regex:"):].strip()
        try:
            re.compile(pattern, re.I)
        except re.error as exc:
            await update.effective_message.reply_text(f"That regex doesn't compile: {exc}")
            return
        word = pattern
        is_regex = 1
    else:
        word = raw.lower()
    if not word:
        await update.effective_message.reply_text("Usage: /gfilter [warn|mute|ban] free mint drainer")
        return
    con = _db()
    con.execute(
        "INSERT INTO filters(chat_id, word, action, is_regex) VALUES(?,?,?,?) "
        "ON CONFLICT(chat_id, word) DO UPDATE SET action=excluded.action, is_regex=excluded.is_regex",
        (update.effective_chat.id, word, action, is_regex),
    )
    con.commit()
    con.close()
    kind = "regex" if is_regex else "phrase"
    _config_audit(update.effective_chat.id, update.effective_user.id, "gfilter", f"{kind}:{word} ({action})")
    await update.effective_message.reply_text(f"Blocked {kind} ({action}): {_esc(word)}")


async def gunfilter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    word = " ".join(context.args).strip().lower()
    con = _db()
    con.execute("DELETE FROM filters WHERE chat_id=? AND word=?", (update.effective_chat.id, word))
    con.commit()
    con.close()
    _config_audit(update.effective_chat.id, update.effective_user.id, "gunfilter", word)
    await update.effective_message.reply_text("Removed.")


async def gfilters(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute(
        "SELECT word, action, is_regex FROM filters WHERE chat_id=?", (update.effective_chat.id,)
    ).fetchall()
    con.close()
    extra = ", ".join(f"{'regex:' if r else ''}{w} ({a or 'mute'})" for w, a, r in rows) or "(none extra)"
    await update.effective_message.reply_text("Default scam lines + " + extra)


async def gban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_cmd_perm(update, context, "gban", "admin"):
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
    _track_ban(update.effective_chat.id, uid)
    _audit(update.effective_chat.id, update.effective_user.id, uid, "ban", "manual")
    await _check_admin_spree(context, update.effective_chat.id, update.effective_user.id)
    await update.effective_message.reply_text(f"Global ban set for {uid}.")
    await _log(
        context,
        f"\U0001F528 Manual global ban: {uid} by {update.effective_user.id} in {update.effective_chat.id}.",
        update.effective_chat.id,
    )


async def gunban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_cmd_perm(update, context, "gunban", "admin") or not context.args:
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
    _untrack_ban(update.effective_chat.id, uid)
    _audit(update.effective_chat.id, update.effective_user.id, uid, "unban", "manual")
    await update.effective_message.reply_text(f"Lifted {uid}.")


async def glink(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("Usage: /glink on|off")
        return
    val = 1 if context.args[0].lower() == "on" else 0
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, antilink) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET antilink=excluded.antilink",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    _config_audit(update.effective_chat.id, update.effective_user.id, "glink", "on" if val else "off")
    await update.effective_message.reply_text(f"New-member link lock: {'ON' if val else 'OFF'}")


async def linkwhitelist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    args = list(context.args or [])
    chat_id = update.effective_chat.id
    if not args or args[0].lower() not in ("add", "remove", "list"):
        await update.effective_message.reply_text(
            "Usage: /linkwhitelist add t.me/Ferzan_Trade\n"
            "       /linkwhitelist remove t.me/Ferzan_Trade\n"
            "       /linkwhitelist list"
        )
        return
    action = args[0].lower()
    con = _db()
    if action == "list":
        rows = con.execute("SELECT domain FROM link_whitelist WHERE chat_id=?", (chat_id,)).fetchall()
        con.close()
        text = ", ".join(r[0] for r in rows) or "(none whitelisted)"
        await update.effective_message.reply_text(text)
        return
    if len(args) < 2:
        con.close()
        await update.effective_message.reply_text("Give me a domain or t.me link, e.g. /linkwhitelist add ferzan.io")
        return
    domain = args[1].strip().lower()
    if action == "add":
        con.execute("INSERT OR IGNORE INTO link_whitelist(chat_id, domain) VALUES(?,?)", (chat_id, domain))
        con.commit()
        msg = f"Whitelisted: {_esc(domain)} — locks and antilink will ignore it."
    else:
        con.execute("DELETE FROM link_whitelist WHERE chat_id=? AND domain=?", (chat_id, domain))
        con.commit()
        msg = f"Removed from whitelist: {_esc(domain)}"
    con.close()
    _whitelist_cache[chat_id]["ts"] = 0
    await update.effective_message.reply_text(msg)


async def gwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_mod(update, context):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to a user with /gwarn to add a strike.")
        return
    uid = update.message.reply_to_message.from_user.id
    await _strike(context, update.effective_chat.id, uid, "manual admin warn")
    _audit(update.effective_chat.id, update.effective_user.id, uid, "warn", "manual")
    await update.effective_message.reply_text("Strike added.")


async def gwarns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to a user with /gwarns to see their strikes.")
        return
    uid = update.message.reply_to_message.from_user.id
    limit, _decay = _warn_settings(update.effective_chat.id)
    con = _db()
    row = con.execute(
        "SELECT count FROM warns WHERE chat_id=? AND user_id=?", (update.effective_chat.id, uid)
    ).fetchone()
    con.close()
    count = row[0] if row else 0
    await update.effective_message.reply_text(f"{count}/{limit} strikes.")


async def mystats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Self-service version of /gwarns — any member can check their own record, no admin needed."""
    chat_id = update.effective_chat.id
    uid = update.effective_user.id
    limit, _decay = _warn_settings(chat_id)
    con = _db()
    row = con.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, uid)).fetchone()
    mute_row = con.execute(
        "SELECT until_ts FROM current_mutes WHERE chat_id=? AND user_id=?", (chat_id, uid)
    ).fetchone()
    con.close()
    count = row[0] if row else 0
    lines = [f"📋 <b>Your record here</b>\n• Strikes: {count}/{limit}"]
    now_ts = time.time()
    if mute_row and mute_row[0] and mute_row[0] > now_ts:
        remaining_min = max(1, int((mute_row[0] - now_ts) // 60))
        lines.append(f"• Currently muted — about {remaining_min} min left")
    else:
        lines.append("• Not currently muted")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def gunwarn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_mod(update, context):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to a user with /gunwarn to remove one strike.")
        return
    uid = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id
    con = _db()
    row = con.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, uid)).fetchone()
    if not row or row[0] <= 0:
        con.close()
        await update.effective_message.reply_text("They have no strikes to remove.")
        return
    new_count = max(0, row[0] - 1)
    con.execute("UPDATE warns SET count=? WHERE chat_id=? AND user_id=?", (new_count, chat_id, uid))
    con.commit()
    con.close()
    _audit(chat_id, update.effective_user.id, uid, "unwarn", "manual")
    limit, _decay = _warn_settings(chat_id)
    await update.effective_message.reply_text(f"Strike removed — now {new_count}/{limit}.")


async def gsetwarns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit() or int(context.args[0]) < 1:
        await update.effective_message.reply_text("Usage: /gsetwarns 5 — strikes before an auto-ban")
        return
    val = int(context.args[0])
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, warn_limit) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET warn_limit=excluded.warn_limit",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Strike limit set to {val}.")


async def gsetdecay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text(
            "Usage: /gsetdecay 30 — strikes reset after 30 days of no new strikes (0 = never expire)"
        )
        return
    val = int(context.args[0])
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, warn_decay_days) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET warn_decay_days=excluded.warn_decay_days",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    if val <= 0:
        await update.effective_message.reply_text("Strike decay disabled — strikes never expire.")
    else:
        await update.effective_message.reply_text(f"Strikes now reset after {val} day(s) of good behavior.")


async def graidoff(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    _raid_lock_until[update.effective_chat.id] = 0
    await update.effective_message.reply_text("Raid lock cleared.")


async def grevokeinvite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kills an invite link a raid was attributed to. Bot needs 'invite users' admin rights."""
    if not await _is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    link = " ".join(context.args or []).strip()
    if not link:
        top = _raid_top_invite_link(chat_id, time.time() - RAID_WINDOW_SECONDS)
        if top:
            link = top[0]
        else:
            await update.effective_message.reply_text(
                "Usage: /grevokeinvite <invite link> — with no recent raid to auto-detect from, "
                "paste the exact t.me/+... link."
            )
            return
    try:
        await context.bot.revoke_chat_invite_link(chat_id, link)
    except Exception as exc:
        await update.effective_message.reply_text(f"Couldn't revoke that link: {exc}")
        return
    _config_audit(chat_id, update.effective_user.id, "grevokeinvite", link)
    await update.effective_message.reply_text(f"🔗 Revoked invite link: {_esc(link)}")


async def glockdown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual panic button — triggers the same full-lockdown behavior anti-raid uses automatically,
    for when an admin spots trouble before Guardian's own detection would."""
    if not await _check_cmd_perm(update, context, "glockdown", "admin"):
        return
    chat_id = update.effective_chat.id
    _raid_lock_until[chat_id] = time.time() + RAID_LOCK_SECONDS
    await update.effective_message.reply_text(
        f"🔒 Manual lockdown engaged — non-admin messages are auto-removed for "
        f"{RAID_LOCK_SECONDS // 60} min. /graidoff to lift it early."
    )
    await _log(
        context,
        f"🔒 Manual lockdown engaged in {chat_id} by {update.effective_user.id}.",
        chat_id,
    )


async def gmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_cmd_perm(update, context, "gmute", "mod"):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to a user with /gmute [10m|1h|1d] (default 1h).")
        return
    uid = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id
    raw_dur = context.args[0] if context.args else ""
    seconds = _parse_duration(raw_dur, 3600)
    label = raw_dur if _DURATION_RE.match(raw_dur.strip()) else "1h"
    until_ts = int(time.time()) + seconds
    try:
        await context.bot.restrict_chat_member(
            chat_id, uid, ChatPermissions(can_send_messages=False), until_date=until_ts
        )
    except Exception as exc:
        log.warning("gmute %s", exc)
        await update.effective_message.reply_text("Couldn't mute them — check my admin permissions.")
        return
    _track_mute(chat_id, uid, until_ts)
    _audit(chat_id, update.effective_user.id, uid, "mute", f"manual ({label})")
    await _check_admin_spree(context, chat_id, update.effective_user.id)
    await _notify_mute(context, chat_id, uid, until_ts, f"manual mute by an admin ({label})")
    await update.effective_message.reply_text(f"🔇 Muted for {label}.")


async def gunmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_cmd_perm(update, context, "gunmute", "mod"):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to a user with /gunmute to lift a mute.")
        return
    uid = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id
    try:
        chat = await context.bot.get_chat(chat_id)
        perms = chat.permissions or ChatPermissions(can_send_messages=True)
        await context.bot.restrict_chat_member(chat_id, uid, perms)
    except Exception as exc:
        log.warning("gunmute %s", exc)
        await update.effective_message.reply_text("Couldn't unmute them — check my admin permissions.")
        return
    _untrack_mute(chat_id, uid)
    _audit(chat_id, update.effective_user.id, uid, "unmute", "manual")
    await update.effective_message.reply_text("🔊 Unmuted.")


async def gkick(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _check_cmd_perm(update, context, "gkick", "mod"):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to a user with /gkick to remove them (not a ban).")
        return
    uid = update.message.reply_to_message.from_user.id
    chat_id = update.effective_chat.id
    try:
        await context.bot.ban_chat_member(chat_id, uid)
        await context.bot.unban_chat_member(chat_id, uid)
    except Exception as exc:
        log.warning("gkick %s", exc)
        await update.effective_message.reply_text("Couldn't remove them — check my admin permissions.")
        return
    _audit(chat_id, update.effective_user.id, uid, "kick", "manual")
    await _check_admin_spree(context, chat_id, update.effective_user.id)
    await update.effective_message.reply_text("👢 Kicked — they can rejoin with a new invite.")


async def gunlockadmin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restores restrict/ban rights to an admin whose account tripped the anti-nuke spree lock."""
    if not await _is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    uid = None
    if update.message and update.message.reply_to_message:
        uid = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        uid = int(context.args[0])
    if not uid:
        await update.effective_message.reply_text("Reply to the admin, or pass their numeric id.")
        return
    try:
        await context.bot.promote_chat_member(chat_id, uid, can_restrict_members=True)
    except Exception as exc:
        log.warning("gunlockadmin %s", exc)
        await update.effective_message.reply_text("Couldn't restore their permissions — check my own admin rights.")
        return
    con = _db()
    con.execute("DELETE FROM admin_spree_lock WHERE chat_id=? AND user_id=?", (chat_id, uid))
    con.commit()
    con.close()
    _admin_action_times.pop((chat_id, uid), None)
    _audit(chat_id, update.effective_user.id, uid, "unlock_admin", "manual")
    await update.effective_message.reply_text(f"Restrict/ban rights restored for {uid}.")


async def gclearmutes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    con = _db()
    rows = con.execute("SELECT user_id FROM current_mutes WHERE chat_id=?", (chat_id,)).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No Guardian-tracked mutes to clear here.")
        return
    try:
        chat = await context.bot.get_chat(chat_id)
        perms = chat.permissions or ChatPermissions(can_send_messages=True)
    except Exception:
        perms = ChatPermissions(can_send_messages=True)
    cleared = 0
    for (uid,) in rows:
        try:
            await context.bot.restrict_chat_member(chat_id, uid, perms)
            cleared += 1
        except Exception as exc:
            log.warning("gclearmutes %s", exc)
    con = _db()
    con.execute("DELETE FROM current_mutes WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()
    _audit(chat_id, update.effective_user.id, 0, "clearmutes", f"{cleared} lifted")
    await update.effective_message.reply_text(f"🔊 Cleared {cleared} mute(s).")


async def gclearbans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    con = _db()
    rows = con.execute("SELECT user_id FROM chat_bans WHERE chat_id=?", (chat_id,)).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No Guardian-tracked bans to clear here.")
        return
    cleared = 0
    for (uid,) in rows:
        try:
            await context.bot.unban_chat_member(chat_id, uid)
            cleared += 1
        except Exception as exc:
            log.warning("gclearbans %s", exc)
    con = _db()
    con.execute("DELETE FROM chat_bans WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()
    _audit(chat_id, update.effective_user.id, 0, "clearbans", f"{cleared} lifted")
    await update.effective_message.reply_text(
        f"↩️ Cleared {cleared} ban(s) here. (Ecosystem-wide global bans, if any, aren't touched — use /gunban for those.)"
    )


async def gmodstats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_mod(update, context):
        return
    chat_id = update.effective_chat.id
    cutoff = int(time.time()) - 7 * 86400
    con = _db()
    rows = con.execute(
        "SELECT mod_id, COUNT(*) FROM audit_log WHERE chat_id=? AND ts>=? GROUP BY mod_id ORDER BY COUNT(*) DESC LIMIT 10",
        (chat_id, cutoff),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No mod actions logged here in the last 7 days.")
        return
    lines = ["📊 <b>Mod leaderboard — last 7 days</b>"]
    for mod_id, count in rows:
        lines.append(f"• <code>{mod_id}</code> — {count} action(s)")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def auditlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_mod(update, context):
        return
    chat_id = update.effective_chat.id
    target_id = None
    if update.message and update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
    con = _db()
    if target_id:
        rows = con.execute(
            "SELECT mod_id, action, reason, ts FROM audit_log WHERE chat_id=? AND target_id=? "
            "ORDER BY id DESC LIMIT 20",
            (chat_id, target_id),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT mod_id, action, reason, ts FROM audit_log WHERE chat_id=? ORDER BY id DESC LIMIT 20",
            (chat_id,),
        ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No audit log entries" + (f" for {target_id}." if target_id else " here yet."))
        return
    header = f"📜 <b>Audit log</b>{' for ' + str(target_id) if target_id else ''}"
    lines = [header]
    for mod_id, action, reason, ts in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))
        lines.append(f"• {when} UTC — <code>{mod_id}</code> {_esc(action)} ({_esc(reason or '-')})")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def configauditlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_mod(update, context):
        return
    chat_id = update.effective_chat.id
    con = _db()
    rows = con.execute(
        "SELECT admin_id, action, detail, ts FROM config_audit_log WHERE chat_id=? ORDER BY id DESC LIMIT 20",
        (chat_id,),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No config-change entries here yet.")
        return
    lines = ["⚙️ <b>Config audit log</b> (settings changes, not member actions)"]
    for admin_id, action, detail, ts in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))
        lines.append(f"• {when} UTC — <code>{admin_id}</code> {_esc(action)}: {_esc(detail or '-')}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def exportauditlog_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    con = _db()
    rows = con.execute(
        "SELECT id, mod_id, target_id, action, reason, ts FROM audit_log WHERE chat_id=? ORDER BY id",
        (chat_id,),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No audit log entries here yet.")
        return
    lines = ["id,mod_id,target_id,action,reason,timestamp_utc"]
    for rid, mod_id, target_id, action, reason, ts in rows:
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts))
        safe_reason = (reason or "").replace('"', '""')
        lines.append(f'{rid},{mod_id},{target_id},{action},"{safe_reason}",{when}')
    data = ("\n".join(lines)).encode("utf-8")
    bio = BytesIO(data)
    bio.name = f"guardian_auditlog_{chat_id}.csv"
    await update.effective_message.reply_document(
        bio, caption=f"📜 Full audit log export — {len(rows)} entries."
    )


async def exportmembers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Exports everyone Guardian has recorded joining this group, with their strikes and
    cross-group trust info. NOT a live full member list — Telegram's Bot API has no method
    to enumerate a group's full membership, so this only covers joins Guardian has seen."""
    if not await _is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    con = _db()
    rows = con.execute(
        "SELECT j.user_id, j.joined_ts, COALESCE(w.count,0), t.first_seen, t.last_seen, "
        "COALESCE(t.groups_seen,0), COALESCE(t.strikes_ever,0) "
        "FROM joins j LEFT JOIN warns w ON w.chat_id=j.chat_id AND w.user_id=j.user_id "
        "LEFT JOIN user_trust t ON t.user_id=j.user_id "
        "WHERE j.chat_id=? ORDER BY j.joined_ts",
        (chat_id,),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No recorded joins for this group yet.")
        return
    lines = [
        "user_id,joined_utc,strikes_here,strikes_ever,groups_seen_ecosystem_wide,"
        "first_seen_ecosystem_utc,last_seen_ecosystem_utc"
    ]
    for uid, joined_ts, strikes_here, first_seen, last_seen, groups_seen, strikes_ever in rows:
        joined_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(joined_ts)) if joined_ts else ""
        first_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(first_seen)) if first_seen else ""
        last_str = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(last_seen)) if last_seen else ""
        lines.append(
            f"{uid},{joined_str},{strikes_here},{strikes_ever},{groups_seen},{first_str},{last_str}"
        )
    data = ("\n".join(lines)).encode("utf-8")
    bio = BytesIO(data)
    bio.name = f"guardian_members_{chat_id}.csv"
    await update.effective_message.reply_document(
        bio,
        caption=(
            f"👥 {len(rows)} recorded joins exported.\n"
            "⚠️ Not a live full member list — Telegram's Bot API has no way to enumerate a "
            "group's full membership. This is everyone Guardian has personally seen join."
        ),
    )


async def glinkscan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("Usage: /glinkscan on|off")
        return
    val = 1 if context.args[0].lower() == "on" else 0
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, linkscan_enabled) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET linkscan_enabled=excluded.linkscan_enabled",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Link safety scan: {'ON' if val else 'OFF'}")


async def gapprove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to a user with /gapprove to exempt them from auto-mod.")
        return
    uid = update.message.reply_to_message.from_user.id
    con = _db()
    con.execute("INSERT OR IGNORE INTO approved(chat_id, user_id) VALUES(?,?)", (update.effective_chat.id, uid))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Approved — exempt from auto-mod here.")


async def gunapprove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to a user with /gunapprove to remove their exemption.")
        return
    uid = update.message.reply_to_message.from_user.id
    con = _db()
    con.execute("DELETE FROM approved WHERE chat_id=? AND user_id=?", (update.effective_chat.id, uid))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Approval removed.")


from telegram.ext import TypeHandler as _TypeHandler


async def _debug_log_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Warn admins when Telegram sends a message format this bot can't read (e.g. rich numbered lists)."""
    import logging as _lg
    m = update.message
    if not m or m.text or m.caption or m.effective_attachment is not None or not m.api_kwargs:
        return
    _lg.getLogger("guardian").warning(
        "unreadable message format in chat %s, fields=%s", update.effective_chat.id, list(m.api_kwargs.keys())[:5]
    )
    try:
        if await _is_admin(update, context):
            await m.reply_text(
                "\u26a0\ufe0f I couldn't read that message \u2014 it uses Telegram formatting "
                "(like a numbered list) this bot doesn't support yet. Remove the list formatting "
                "and send it as plain text."
            )
    except Exception:
        pass


async def _filter_cmd_first(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run /filter and /save for admins before any link/spam/scam protection can swallow them."""
    import logging as _lg
    from telegram.ext import ApplicationHandlerStop as _Stop
    _log = _lg.getLogger("guardian")
    _log.info("filter-cmd-first: saw filter command in chat %s", update.effective_chat.id)
    if not await _is_admin(update, context):
        _log.info("filter-cmd-first: sender not admin, passing through")
        return
    try:
        await save_note(update, context)
    except Exception:
        _log.exception("filter-cmd-first: save_note failed")
        try:
            await update.effective_message.reply_text("\u26a0\ufe0f Couldn't save that filter, error logged.")
        except Exception:
            pass
    raise _Stop


async def save_note(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    msg = update.effective_message
    parts = (msg.text or msg.caption or "").split(None, 2)
    if len(parts) < 2:
        await msg.reply_text(
            "Usage: /save name text — or attach a photo/GIF/video with /save name as the "
            "caption, or reply to one with /save name [text]"
        )
        return
    name = parts[1].strip().lower()
    media_id, media_type = None, None
    if msg.photo:
        media_id, media_type = msg.photo[-1].file_id, "photo"
    elif msg.animation:
        media_id, media_type = msg.animation.file_id, "animation"
    elif msg.video:
        media_id, media_type = msg.video.file_id, "video"
    if msg.reply_to_message and not media_id:
        rmsg = msg.reply_to_message
        if rmsg.photo:
            media_id, media_type = rmsg.photo[-1].file_id, "photo"
        elif rmsg.animation:
            media_id, media_type = rmsg.animation.file_id, "animation"
        elif rmsg.video:
            media_id, media_type = rmsg.video.file_id, "video"
    if msg.reply_to_message and len(parts) < 3:
        text = msg.reply_to_message.text or msg.reply_to_message.caption or ""
    else:
        text = parts[2] if len(parts) > 2 else ""
    if not text.strip() and not media_id:
        _pending_note_media[update.effective_chat.id] = (update.effective_user.id, name, time.time() + 300)
        await msg.reply_text(
            f"📎 Now just upload the photo, GIF, or video for #{name} — no caption or "
            "command needed, I'll grab the next one you post here within 5 min.\n"
            f"Or send /save {name} <text> any time to just set/update its text."
        )
        return
    con = _db()
    con.execute(
        "INSERT INTO notes(chat_id, name, text, media_id, media_type) VALUES(?,?,?,?,?) "
        "ON CONFLICT(chat_id, name) DO UPDATE SET text=excluded.text, "
        "media_id=COALESCE(excluded.media_id, notes.media_id), "
        "media_type=COALESCE(excluded.media_type, notes.media_type)",
        (update.effective_chat.id, name, text.strip(), media_id, media_type),
    )
    con.commit()
    con.close()
    note = f" with a {media_type}" if media_type else ""
    await msg.reply_text(f"Saved note: #{name}{note}")


async def notes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute("SELECT name FROM notes WHERE chat_id=?", (update.effective_chat.id,)).fetchall()
    con.close()
    names = ", ".join(f"#{r[0]}" for r in rows) or "(none saved)"
    await update.effective_message.reply_text(f"Saved notes: {names}")


async def delnote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /delnote name")
        return
    name = context.args[0].strip().lower()
    con = _db()
    con.execute("DELETE FROM notes WHERE chat_id=? AND name=?", (update.effective_chat.id, name))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Removed.")


async def setreply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    parts = (update.effective_message.text or "").split(None, 2)
    if len(parts) < 3:
        await update.effective_message.reply_text("Usage: /setreply trigger reply text")
        return
    trig = parts[1].strip().lower()
    rep = parts[2].strip()
    con = _db()
    con.execute(
        "INSERT INTO autoreply(chat_id, trig, reply) VALUES(?,?,?) "
        "ON CONFLICT(chat_id, trig) DO UPDATE SET reply=excluded.reply",
        (update.effective_chat.id, trig, rep),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Auto-reply set for: {_esc(trig)}")


async def delreply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /delreply trigger")
        return
    trig = " ".join(context.args).strip().lower()
    con = _db()
    con.execute("DELETE FROM autoreply WHERE chat_id=? AND trig=?", (update.effective_chat.id, trig))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Removed.")


async def replies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute("SELECT trig FROM autoreply WHERE chat_id=?", (update.effective_chat.id,)).fetchall()
    con.close()
    names = ", ".join(r[0] for r in rows) or "(none set)"
    await update.effective_message.reply_text(f"Auto-reply triggers: {names}")


async def faqadd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    raw = (update.effective_message.text or "").split(None, 1)
    if len(raw) < 2 or "|" not in raw[1]:
        await update.effective_message.reply_text(
            "Usage: /faqadd question | answer\nExample: /faqadd how do I stake? | Head to app.example.com/stake and connect your wallet."
        )
        return
    question, answer = raw[1].split("|", 1)
    question, answer = question.strip(), answer.strip()
    if not question or not answer:
        await update.effective_message.reply_text("Usage: /faqadd question | answer")
        return
    keywords = ",".join(sorted(set(_WORD_RE.findall(question.lower()))))
    con = _db()
    con.execute(
        "INSERT INTO faq(chat_id, question, answer, keywords, created_by, ts) "
        "VALUES(?,?,?,?,?,strftime('%s','now'))",
        (update.effective_chat.id, question, answer, keywords, update.effective_user.id),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"FAQ added: {_esc(question)}")


async def faqlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute(
        "SELECT id, question FROM faq WHERE chat_id=? ORDER BY id", (update.effective_chat.id,)
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No FAQs set yet — add one with /faqadd question | answer")
        return
    lines = ["\U0001F4DA <b>FAQs</b>"] + [f"#{fid} — {_esc(q)}" for fid, q in rows]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def faqdel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /faqdel <id>")
        return
    con = _db()
    con.execute(
        "DELETE FROM faq WHERE id=? AND chat_id=?", (int(context.args[0]), update.effective_chat.id)
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text("Removed.")


async def sethoneypot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /sethoneypot <secret_command_name> (without the /)")
        return
    name = context.args[0].strip().lstrip("/").lower()
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, honeypot_cmd) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET honeypot_cmd=excluded.honeypot_cmd",
        (update.effective_chat.id, name),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(
        f"Honeypot armed on /{_esc(name)} — anyone who runs it gets instantly banned."
    )


async def deletehoneypot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    con = _db()
    con.execute("UPDATE settings SET honeypot_cmd=NULL WHERE chat_id=?", (update.effective_chat.id,))
    con.commit()
    con.close()
    await update.effective_message.reply_text("Honeypot disarmed.")


async def gblocksticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    msg = update.effective_message
    target = msg.reply_to_message
    key = None
    if target and target.sticker and target.sticker.set_name:
        key = target.sticker.set_name.lower()
    elif target and target.animation and target.animation.file_unique_id:
        key = target.animation.file_unique_id
    if not key:
        await msg.reply_text("Reply to the sticker or GIF you want to block with /gblocksticker.")
        return
    con = _db()
    con.execute(
        "INSERT OR IGNORE INTO sticker_blocklist(chat_id, set_name, added_by, ts) VALUES(?,?,?,strftime('%s','now'))",
        (update.effective_chat.id, key, update.effective_user.id),
    )
    con.commit()
    con.close()
    await msg.reply_text(f"Blocked: {_esc(key)}")


async def gunblocksticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    msg = update.effective_message
    key = None
    target = msg.reply_to_message
    if target and target.sticker and target.sticker.set_name:
        key = target.sticker.set_name.lower()
    elif target and target.animation and target.animation.file_unique_id:
        key = target.animation.file_unique_id
    elif context.args:
        key = context.args[0].strip().lower()
    if not key:
        await msg.reply_text("Reply to the sticker/GIF, or pass the blocked set name, with /gunblocksticker.")
        return
    con = _db()
    con.execute(
        "DELETE FROM sticker_blocklist WHERE chat_id=? AND set_name=?", (update.effective_chat.id, key)
    )
    con.commit()
    con.close()
    await msg.reply_text(f"Unblocked: {_esc(key)}")


async def gstickerblocklist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute(
        "SELECT set_name FROM sticker_blocklist WHERE chat_id=?", (update.effective_chat.id,)
    ).fetchall()
    con.close()
    names = ", ".join(r[0] for r in rows) or "(none blocked)"
    await update.effective_message.reply_text(f"Blocked sticker/GIF packs: {names}")


async def gscamadd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in OWNER_IDS:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /gscamadd <ca-or-domain>")
        return
    val = " ".join(context.args).strip().lower()
    kind = "domain" if "." in val and not val.startswith("0x") else "ca"
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO scam_list(value, kind, added_by, ts) VALUES(?,?,?,strftime('%s','now'))",
        (val, kind, update.effective_user.id),
    )
    con.commit()
    con.close()
    _scam_cache["ts"] = 0
    await update.effective_message.reply_text(f"Added to ecosystem scam list ({kind}): {_esc(val)}")


async def gscamdel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in OWNER_IDS:
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /gscamdel <ca-or-domain>")
        return
    val = " ".join(context.args).strip().lower()
    con = _db()
    con.execute("DELETE FROM scam_list WHERE value=?", (val,))
    con.commit()
    con.close()
    _scam_cache["ts"] = 0
    await update.effective_message.reply_text("Removed.")


async def gscamlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in OWNER_IDS:
        return
    con = _db()
    rows = con.execute("SELECT value, kind FROM scam_list ORDER BY ts DESC LIMIT 50").fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("Ecosystem scam list is empty.")
        return
    lines = [f"{k}: {v}" for v, k in rows]
    await update.effective_message.reply_text("\n".join(lines)[:4000])


async def scamadd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /scamadd <ca-or-domain> — blocks it in this group only")
        return
    val = " ".join(context.args).strip().lower()
    kind = "domain" if "." in val and not val.startswith("0x") else "ca"
    chat_id = update.effective_chat.id
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO local_scam(chat_id, value, kind, added_by, ts) VALUES(?,?,?,?,strftime('%s','now'))",
        (chat_id, val, kind, update.effective_user.id),
    )
    con.commit()
    con.close()
    _local_scam_cache[chat_id]["ts"] = 0
    _config_audit(chat_id, update.effective_user.id, "scamadd", f"{kind}:{val}")
    promoted = _federate_report(val, kind, chat_id)
    msg_text = f"Added to this group's scam list ({kind}): {_esc(val)}"
    if promoted:
        msg_text += (
            f"\n\n🌐 {FEDERATION_THRESHOLD}+ groups have now flagged this — promoted to the "
            "ecosystem-wide scam list automatically."
        )
    await update.effective_message.reply_text(msg_text)


async def scamdel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /scamdel <ca-or-domain>")
        return
    val = " ".join(context.args).strip().lower()
    chat_id = update.effective_chat.id
    con = _db()
    con.execute("DELETE FROM local_scam WHERE chat_id=? AND value=?", (chat_id, val))
    con.commit()
    con.close()
    _local_scam_cache[chat_id]["ts"] = 0
    _config_audit(chat_id, update.effective_user.id, "scamdel", val)
    await update.effective_message.reply_text("Removed.")


async def scamlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    con = _db()
    rows = con.execute(
        "SELECT value, kind FROM local_scam WHERE chat_id=? ORDER BY ts DESC LIMIT 50",
        (update.effective_chat.id,),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("This group's scam list is empty.")
        return
    lines = [f"{k}: {v}" for v, k in rows]
    await update.effective_message.reply_text("\n".join(lines)[:4000])


async def setlogchat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /setlogchat <chat_id> — where THIS group's ban/strike/raid alerts get posted.\n"
            "Add me to that log channel/group first, then run this with its numeric id (forward a "
            "message from it to @userinfobot to find it), or /setlogchat off to stop logging."
        )
        return
    val = context.args[0].strip()
    if val.lower() == "off":
        val = None
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, log_chat_id) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET log_chat_id=excluded.log_chat_id",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text("Log channel cleared." if val is None else f"Log channel set to {val}.")


async def slowmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /slowmode 30 (seconds between messages per user), or /slowmode off")
        return
    arg = context.args[0].strip().lower()
    val = 0 if arg == "off" else (int(arg) if arg.isdigit() else None)
    if val is None:
        await update.effective_message.reply_text("Usage: /slowmode 30 (seconds between messages per user), or /slowmode off")
        return
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, slowmode_seconds) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET slowmode_seconds=excluded.slowmode_seconds",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text("Slow mode off." if val <= 0 else f"⏳ Slow mode: {val}s between messages per user.")


async def gadaptiveslowmode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text(
            f"Usage: /gadaptiveslowmode on|off — auto-enables a {ADAPTIVE_SLOWMODE_SECONDS}s slowmode "
            f"whenever the chat's message rate spikes ({ADAPTIVE_SLOWMODE_TRIGGER}+ msgs in "
            f"{ADAPTIVE_SLOWMODE_WINDOW}s), and lifts it automatically once things cool down."
        )
        return
    val = 1 if context.args[0].lower() == "on" else 0
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, adaptive_slowmode_enabled) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET adaptive_slowmode_enabled=excluded.adaptive_slowmode_enabled",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Adaptive slowmode: {'ON' if val else 'OFF'}")


async def gquiethours_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    if context.args and context.args[0].lower() == "off":
        con = _db()
        con.execute(
            "INSERT INTO settings(chat_id, quiet_hours_enabled) VALUES(?,0) "
            "ON CONFLICT(chat_id) DO UPDATE SET quiet_hours_enabled=0",
            (chat_id,),
        )
        con.commit()
        con.close()
        await update.effective_message.reply_text("Quiet hours: OFF")
        return
    if len(context.args or []) != 2 or not context.args[0].isdigit() or not context.args[1].isdigit():
        tz_name = _chat_timezone_name(chat_id)
        await update.effective_message.reply_text(
            f"Usage: /gquiethours 2 6 — restricts non-admins to slow-mode-only between 2am and 6am "
            f"group-local time ({tz_name}, set with /gsettimezone). /gquiethours off to disable."
        )
        return
    start_h, end_h = int(context.args[0]), int(context.args[1])
    if not (0 <= start_h < 24 and 0 <= end_h < 24):
        await update.effective_message.reply_text("Hours should be 0-23.")
        return
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, quiet_hours_enabled, quiet_hours_start, quiet_hours_end) "
        "VALUES(?,1,?,?) ON CONFLICT(chat_id) DO UPDATE SET quiet_hours_enabled=1, "
        "quiet_hours_start=excluded.quiet_hours_start, quiet_hours_end=excluded.quiet_hours_end",
        (chat_id, start_h, end_h),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(
        f"🌙 Quiet hours: {start_h:02d}:00–{end_h:02d}:00 group-local — non-admins get a "
        f"{ADAPTIVE_SLOWMODE_SECONDS}s slowmode automatically during that window."
    )


async def gstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    day = time.strftime("%Y-%m-%d", time.gmtime())
    con = _db()
    mrow = con.execute("SELECT count FROM msg_counts WHERE chat_id=? AND day=?", (chat_id, day)).fetchone()
    joins_today = con.execute(
        "SELECT COUNT(*) FROM joins WHERE chat_id=? AND joined_ts>=?",
        (chat_id, int(time.time()) - 86400),
    ).fetchone()[0]
    warns_total = con.execute(
        "SELECT COALESCE(SUM(count),0) FROM warns WHERE chat_id=?", (chat_id,)
    ).fetchone()[0]
    con.close()
    msgs = mrow[0] if mrow else 0
    text = (
        f"\U0001F4CA <b>Guardian Stats</b>\n"
        f"Messages today (UTC): {msgs}\n"
        f"New joins (last 24h): {joins_today}\n"
        f"Total active strikes: {warns_total}\n"
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


def _compute_health(chat_id: int) -> dict:
    cutoff7 = int(time.time()) - 7 * 86400
    con = _db()
    days = [time.strftime("%Y-%m-%d", time.gmtime(time.time() - i * 86400)) for i in range(7)]
    q = ",".join("?" * len(days))
    msgs_7d = con.execute(
        f"SELECT COALESCE(SUM(count),0) FROM msg_counts WHERE chat_id=? AND day IN ({q})",
        (chat_id, *days),
    ).fetchone()[0]
    strikes_total = con.execute(
        "SELECT COALESCE(SUM(count),0) FROM warns WHERE chat_id=?", (chat_id,)
    ).fetchone()[0]
    mod_actions_7d = con.execute(
        "SELECT COUNT(*) FROM audit_log WHERE chat_id=? AND ts>=?", (chat_id, cutoff7)
    ).fetchone()[0]
    reports_open = con.execute(
        "SELECT COUNT(*) FROM reports WHERE chat_id=? AND status='open'", (chat_id,)
    ).fetchone()[0]
    reports_total = con.execute("SELECT COUNT(*) FROM reports WHERE chat_id=?", (chat_id,)).fetchone()[0]
    con.close()

    mod_ratio = mod_actions_7d / max(1, msgs_7d)
    strike_ratio = strikes_total / max(1, msgs_7d)
    score = 100.0
    score -= min(50.0, mod_ratio * 800)
    score -= min(25.0, strike_ratio * 400)
    score -= min(15.0, reports_open * 4)
    score = max(0, min(100, round(score)))

    if score >= 80:
        label = "🟢 Healthy"
    elif score >= 50:
        label = "🟡 Watch"
    else:
        label = "🔴 At risk"

    return {
        "score": score,
        "label": label,
        "msgs_7d": msgs_7d,
        "mod_actions_7d": mod_actions_7d,
        "strikes_total": strikes_total,
        "reports_open": reports_open,
        "reports_total": reports_total,
    }


async def ghealth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_mod(update, context):
        return
    chat_id = update.effective_chat.id
    h = _compute_health(chat_id)

    text = (
        f"\U0001FA7A <b>Group Health</b> — {h['label']} ({h['score']}/100)\n\n"
        f"Messages (7d): {h['msgs_7d']}\n"
        f"Mod actions (7d): {h['mod_actions_7d']}\n"
        f"Active strikes: {h['strikes_total']}\n"
        f"Open reports: {h['reports_open']} (of {h['reports_total']} total)\n\n"
        f"<i>A rough pulse, not a diagnosis — high mod-action or strike rates relative to message "
        f"volume pull the score down.</i>"
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def gbroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id not in OWNER_IDS:
        return
    parts = (update.effective_message.text or "").split(None, 1)
    text = parts[1] if len(parts) > 1 else ""
    if not text.strip():
        await update.effective_message.reply_text("Usage: /gbroadcast <message>")
        return
    con = _db()
    chats = con.execute("SELECT chat_id FROM known_chats").fetchall()
    con.close()
    sent, failed = 0, 0
    for (cid,) in chats:
        try:
            await context.bot.send_message(cid, text.strip(), parse_mode="HTML")
            sent += 1
        except Exception as exc:
            failed += 1
            log.warning("broadcast %s %s", cid, exc)
    await update.effective_message.reply_text(f"Broadcast sent to {sent} chats ({failed} failed).")


async def inline_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """@FerzanGuardianBot <id-or-CA-or-domain> in any chat — quick blocklist check without
    leaving the conversation. Requires inline mode enabled for the bot via @BotFather."""
    query = (update.inline_query.query or "").strip()
    if not query:
        return
    if query.isdigit():
        uid = int(query)
        con = _db()
        banned = con.execute("SELECT reason FROM global_bans WHERE user_id=?", (uid,)).fetchone()
        con.close()
        status = f"🚫 Globally banned — {banned[0]}" if banned else "✅ Not on the global ban list"
        title = f"User {uid}"
    else:
        val = query.lower()
        con = _db()
        hit = con.execute("SELECT kind FROM scam_list WHERE value=?", (val,)).fetchone()
        con.close()
        status = f"🚫 On the ecosystem scam list ({hit[0]})" if hit else "✅ Not on the ecosystem scam list"
        title = query
    results = [
        InlineQueryResultArticle(
            id="lookup",
            title=title,
            description=status,
            input_message_content=InputTextMessageContent(f"🔎 Guardian lookup — {_esc(title)}\n{status}"),
        )
    ]
    try:
        await update.inline_query.answer(results, cache_time=5)
    except Exception as exc:
        log.warning("inline lookup %s", exc)


# ---- Giveaways / raffles ----


def _giveaway_kb(giveaway_id: int, entrants: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🎉 Enter ({entrants})", callback_data=f"gw:{giveaway_id}")]]
    )


def _giveaway_text(prize: str, ends_ts: int, entrants: int, ended: bool = False, winner_id: int | None = None) -> str:
    if ended:
        result = f"🏆 Winner: {winner_id}" if winner_id else "No entries — no winner."
        return f"🎉 <b>Giveaway ended</b>\n\nPrize: {_esc(prize)}\nEntrants: {entrants}\n{result}"
    when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ends_ts))
    return (
        f"🎉 <b>Giveaway!</b>\n\nPrize: {_esc(prize)}\nEnds: {when}\nEntrants: {entrants}\n\n"
        "Tap below to enter — one entry per person."
    )


async def giveaway_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_mod(update, context):
        return
    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Usage: /giveaway <duration> <prize> — e.g. /giveaway 1h Free NFT mint spot"
        )
        return
    seconds = _parse_duration(args[0], 0)
    if seconds <= 0 or not _DURATION_RE.match(args[0].strip()):
        await update.effective_message.reply_text("Duration should look like 10m, 1h, or 2d.")
        return
    prize = " ".join(args[1:]).strip()
    chat_id = update.effective_chat.id
    ends_ts = int(time.time()) + seconds
    con = _db()
    cur = con.execute(
        "INSERT INTO giveaways(chat_id, prize, ends_ts, created_by, ts) VALUES(?,?,?,?,strftime('%s','now'))",
        (chat_id, prize, ends_ts, update.effective_user.id),
    )
    giveaway_id = cur.lastrowid
    con.commit()
    con.close()
    sent = await update.effective_message.reply_text(
        _giveaway_text(prize, ends_ts, 0), parse_mode="HTML", reply_markup=_giveaway_kb(giveaway_id, 0)
    )
    con = _db()
    con.execute("UPDATE giveaways SET message_id=? WHERE id=?", (sent.message_id, giveaway_id))
    con.commit()
    con.close()
    if context.job_queue:
        context.job_queue.run_once(
            _giveaway_end, seconds, data={"giveaway_id": giveaway_id}, name=f"giveaway:{giveaway_id}"
        )


async def giveaway_enter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        giveaway_id = int((q.data or "").split(":", 1)[1])
    except Exception:
        await q.answer()
        return
    con = _db()
    row = con.execute(
        "SELECT chat_id, prize, ends_ts, status FROM giveaways WHERE id=?", (giveaway_id,)
    ).fetchone()
    if not row or row[3] != "active":
        con.close()
        await q.answer("This giveaway's already over.", show_alert=True)
        return
    chat_id, prize, ends_ts, _ = row
    con.execute(
        "INSERT OR IGNORE INTO giveaway_entries(giveaway_id, user_id, ts) VALUES(?,?,strftime('%s','now'))",
        (giveaway_id, q.from_user.id),
    )
    con.commit()
    entrants = con.execute(
        "SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id=?", (giveaway_id,)
    ).fetchone()[0]
    con.close()
    try:
        await q.edit_message_text(
            _giveaway_text(prize, ends_ts, entrants), parse_mode="HTML",
            reply_markup=_giveaway_kb(giveaway_id, entrants),
        )
    except Exception:
        pass
    await q.answer("You're in! 🎉")


async def _end_giveaway(bot, giveaway_id: int) -> None:
    con = _db()
    row = con.execute(
        "SELECT chat_id, prize, message_id, status FROM giveaways WHERE id=?", (giveaway_id,)
    ).fetchone()
    if not row or row[3] != "active":
        con.close()
        return
    chat_id, prize, message_id, _ = row
    entrants = [r[0] for r in con.execute(
        "SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (giveaway_id,)
    ).fetchall()]
    winner_id = random.choice(entrants) if entrants else None
    con.execute(
        "UPDATE giveaways SET status='ended', winner_id=? WHERE id=?", (winner_id, giveaway_id)
    )
    con.commit()
    con.close()
    text = _giveaway_text(prize, 0, len(entrants), ended=True, winner_id=winner_id)
    try:
        if message_id:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML")
        else:
            await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as exc:
        log.warning("giveaway end edit %s", exc)
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception:
            pass
    if winner_id:
        try:
            await bot.send_message(chat_id, f"🏆 Congrats <a href=\"tg://user?id={winner_id}\">winner</a>!", parse_mode="HTML")
        except Exception:
            pass


async def _giveaway_end(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue entry point — the actual logic lives in _end_giveaway so /gendgiveaway can call it too."""
    await _end_giveaway(context.bot, context.job.data["giveaway_id"])


async def gendgiveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_mod(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /gendgiveaway <id>")
        return
    giveaway_id = int(context.args[0])
    if context.job_queue:
        for job in context.job_queue.get_jobs_by_name(f"giveaway:{giveaway_id}"):
            job.schedule_removal()
    await _end_giveaway(context.bot, giveaway_id)
    await update.effective_message.reply_text("Ended.")


def _reschedule_giveaways(app: Application) -> None:
    con = _db()
    rows = con.execute("SELECT id, ends_ts FROM giveaways WHERE status='active'").fetchall()
    con.close()
    now_ts = int(time.time())
    for giveaway_id, ends_ts in rows:
        delay = max(1, ends_ts - now_ts)
        if app.job_queue:
            app.job_queue.run_once(
                _giveaway_end, delay, data={"giveaway_id": giveaway_id}, name=f"giveaway:{giveaway_id}"
            )


# ---- Vote-to-mute — a fallback for when no admin's around ----


def _votemute_settings(chat_id: int) -> tuple[bool, int]:
    con = _db()
    row = con.execute(
        "SELECT votemute_enabled, votemute_threshold FROM settings WHERE chat_id=?", (chat_id,)
    ).fetchone()
    con.close()
    if row is None:
        return False, 5
    return bool(row[0]), int(row[1]) if row[1] else 5


async def gvotemute_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("Usage: /gvotemute on|off")
        return
    val = 1 if context.args[0].lower() == "on" else 0
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, votemute_enabled) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET votemute_enabled=excluded.votemute_enabled",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Vote-to-mute: {'ON' if val else 'OFF'}")


async def gvotemute_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit() or int(context.args[0]) < 2:
        await update.effective_message.reply_text("Usage: /gvotemutethreshold 5 (minimum 2)")
        return
    val = int(context.args[0])
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, votemute_threshold) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET votemute_threshold=excluded.votemute_threshold",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Vote-to-mute threshold: {val} votes.")


def _votemute_kb(vote_id: int, votes: int, threshold: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"👍 Vote to mute ({votes}/{threshold})", callback_data=f"vm:{vote_id}")]]
    )


async def votemute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    enabled, threshold = _votemute_settings(chat_id)
    if not enabled:
        await update.effective_message.reply_text("Vote-to-mute isn't enabled here — an admin can turn it on with /gvotemute on.")
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text("Reply to the person you want to vote-mute with /votemute.")
        return
    remaining = _check_cooldown(update.effective_user.id, chat_id, "votemute", VOTEMUTE_COOLDOWN_SECONDS)
    if remaining > 0:
        await update.effective_message.reply_text(f"Slow down — try again in {int(remaining)}s.")
        return
    target = update.message.reply_to_message.from_user
    if target.id == update.effective_user.id:
        await update.effective_message.reply_text("Can't vote to mute yourself.")
        return
    try:
        member = await context.bot.get_chat_member(chat_id, target.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            await update.effective_message.reply_text("Can't vote-mute an admin.")
            return
    except Exception:
        pass
    con = _db()
    existing = con.execute(
        "SELECT id FROM vote_mutes WHERE chat_id=? AND target_id=? AND status='active'", (chat_id, target.id)
    ).fetchone()
    if existing:
        con.close()
        await update.effective_message.reply_text("There's already an active vote for them.")
        return
    cur = con.execute(
        "INSERT INTO vote_mutes(chat_id, target_id, threshold, created_by, status, ts) "
        "VALUES(?,?,?,?, 'active', strftime('%s','now'))",
        (chat_id, target.id, threshold, update.effective_user.id),
    )
    vote_id = cur.lastrowid
    con.commit()
    con.close()
    sent = await update.effective_message.reply_text(
        f"🗳 Vote to mute <b>{_esc(target.full_name)}</b> for 1h?", parse_mode="HTML",
        reply_markup=_votemute_kb(vote_id, 0, threshold),
    )
    con = _db()
    con.execute("UPDATE vote_mutes SET message_id=? WHERE id=?", (sent.message_id, vote_id))
    con.commit()
    con.close()


async def votemute_vote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        vote_id = int((q.data or "").split(":", 1)[1])
    except Exception:
        await q.answer()
        return
    con = _db()
    row = con.execute(
        "SELECT chat_id, target_id, threshold, status FROM vote_mutes WHERE id=?", (vote_id,)
    ).fetchone()
    if not row or row[3] != "active":
        con.close()
        await q.answer("This vote's already closed.", show_alert=True)
        return
    chat_id, target_id, threshold, _ = row
    if q.from_user.id == target_id:
        con.close()
        await q.answer("You can't vote on your own mute.", show_alert=True)
        return
    con.execute("INSERT OR IGNORE INTO vote_mute_votes(vote_id, user_id) VALUES(?,?)", (vote_id, q.from_user.id))
    con.commit()
    votes = con.execute("SELECT COUNT(*) FROM vote_mute_votes WHERE vote_id=?", (vote_id,)).fetchone()[0]
    if votes >= threshold:
        con.execute("UPDATE vote_mutes SET status='passed' WHERE id=?", (vote_id,))
        con.commit()
        con.close()
        vm_until_ts = int(time.time()) + 3600
        try:
            await context.bot.restrict_chat_member(
                chat_id, target_id, ChatPermissions(can_send_messages=False), until_date=vm_until_ts
            )
        except Exception as exc:
            log.warning("votemute mute %s", exc)
        else:
            _track_mute(chat_id, target_id, vm_until_ts)
            await _notify_mute(context, chat_id, target_id, vm_until_ts, "community vote-to-mute passed")
        try:
            await q.edit_message_text(f"🔇 Vote passed ({votes}/{threshold}) — muted for 1h.")
        except Exception:
            pass
        await q.answer("Vote passed.")
        await _log(context, f"🗳 Vote-to-mute passed against {target_id} in {chat_id} ({votes} votes).", chat_id)
        return
    con.close()
    try:
        await q.edit_message_reply_markup(reply_markup=_votemute_kb(vote_id, votes, threshold))
    except Exception:
        pass
    await q.answer(f"Vote counted ({votes}/{threshold}).")


# ---- On-demand translation ----


async def tr_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _TRANSLATE_AVAILABLE:
        await update.effective_message.reply_text(
            "Translation isn't set up on this deploy yet (needs the deep-translator package)."
        )
        return
    msg = update.effective_message
    if not msg.reply_to_message or not (msg.reply_to_message.text or msg.reply_to_message.caption):
        await update.effective_message.reply_text("Reply to a text message with /tr [target-lang], e.g. /tr es")
        return
    remaining = _check_cooldown(update.effective_user.id, update.effective_chat.id, "tr", TR_COOLDOWN_SECONDS)
    if remaining > 0:
        await update.effective_message.reply_text(f"Slow down — try again in {int(remaining)}s.")
        return
    target = (context.args[0].strip().lower() if context.args else "en")
    source_text = msg.reply_to_message.text or msg.reply_to_message.caption
    try:
        loop = asyncio.get_event_loop()
        translated = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: GoogleTranslator(source="auto", target=target).translate(source_text)),
            timeout=10,
        )
    except Exception as exc:
        log.warning("translate %s", exc)
        await update.effective_message.reply_text("Couldn't translate that — check the language code and try again.")
        return
    if not translated:
        await update.effective_message.reply_text("Couldn't translate that.")
        return
    await msg.reply_text(f"🌐 {_esc(translated)}")


# ---- Scheduled recurring announcements ----


async def _scheduled_post_fire(context: ContextTypes.DEFAULT_TYPE) -> None:
    post_id = context.job.data["id"]
    con = _db()
    row = con.execute("SELECT chat_id, text FROM scheduled_posts WHERE id=?", (post_id,)).fetchone()
    con.close()
    if not row:
        return
    chat_id, text = row
    try:
        await context.bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as exc:
        log.warning("scheduled post %s", exc)


def _register_scheduled_post(app: Application, post_id: int, kind: str, hour: int, minute: int, weekday: int | None) -> None:
    if not app.job_queue:
        return
    days = (weekday,) if kind == "weekly" and weekday is not None else tuple(range(7))
    app.job_queue.run_daily(
        _scheduled_post_fire, time=dtime(hour=hour, minute=minute), days=days,
        data={"id": post_id}, name=f"scheduled_post:{post_id}",
    )


async def gsettimezone(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not _TZ_AVAILABLE:
        await update.effective_message.reply_text(
            "Timezone support isn't available on this deploy (the system's tzdata is missing)."
        )
        return
    if not context.args:
        await update.effective_message.reply_text(
            f"Current timezone: {_chat_timezone_name(update.effective_chat.id)}\n"
            "Usage: /gsettimezone America/New_York (any IANA name, e.g. Europe/London, Asia/Tokyo, UTC)"
        )
        return
    name = context.args[0].strip()
    try:
        ZoneInfo(name)
    except Exception:
        await update.effective_message.reply_text(
            "Not a recognized timezone — use an IANA name like America/New_York or Europe/London."
        )
        return
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, timezone) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET timezone=excluded.timezone",
        (update.effective_chat.id, name),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(
        f"Timezone set to {name}. New /schedule posts will use this — existing ones keep their original time."
    )


async def schedule_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    args = context.args or []
    if len(args) < 3 or args[0].lower() not in ("daily", "weekly"):
        tz_name = _chat_timezone_name(update.effective_chat.id)
        await update.effective_message.reply_text(
            "Usage:\n"
            "/schedule daily 09:00 <text>\n"
            "/schedule weekly mon 09:00 <text> — weekday: mon..sun\n"
            f"Times are this group's local time ({tz_name}) — set with /gsettimezone."
        )
        return
    kind = args[0].lower()
    weekday = None
    if kind == "weekly":
        wdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        if args[1].lower() not in wdays:
            await update.effective_message.reply_text("Weekday should be mon..sun.")
            return
        weekday = wdays.index(args[1].lower())
        time_str = args[2]
        text = " ".join(args[3:]).strip()
    else:
        time_str = args[1]
        text = " ".join(args[2:]).strip()
    m = re.match(r"^(\d{1,2}):(\d{2})$", time_str.strip())
    if not m or not text:
        await update.effective_message.reply_text("Time should look like 09:00, followed by the message text.")
        return
    local_hour, local_minute = int(m.group(1)), int(m.group(2))
    if not (0 <= local_hour < 24 and 0 <= local_minute < 60):
        await update.effective_message.reply_text("Time should look like 09:00.")
        return
    chat_id = update.effective_chat.id
    tz_name = _chat_timezone_name(chat_id)
    local_weekday = weekday
    hour, minute, weekday = _local_to_utc_hm(chat_id, local_hour, local_minute, weekday)
    con = _db()
    cur = con.execute(
        "INSERT INTO scheduled_posts(chat_id, kind, hour, minute, weekday, text, created_by, ts, "
        "tz_name, local_hour, local_minute, local_weekday) "
        "VALUES(?,?,?,?,?,?,?,strftime('%s','now'),?,?,?,?)",
        (chat_id, kind, hour, minute, weekday, text, update.effective_user.id,
         tz_name, local_hour, local_minute, local_weekday),
    )
    post_id = cur.lastrowid
    con.commit()
    con.close()
    _register_scheduled_post(context.application, post_id, kind, hour, minute, weekday)
    when = f"daily at {time_str}" if kind == "daily" else f"every {args[1].lower()} at {time_str}"
    await update.effective_message.reply_text(f"📅 Scheduled (#{post_id}) — {when} {tz_name}.")


async def schedulelist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    con = _db()
    rows = con.execute(
        "SELECT id, kind, hour, minute, weekday, text, tz_name, local_hour, local_minute, local_weekday "
        "FROM scheduled_posts WHERE chat_id=?",
        (update.effective_chat.id,),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No scheduled posts in this group.")
        return
    wdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    lines = []
    for pid, kind, hour, minute, weekday, text, tz_name, local_hour, local_minute, local_weekday in rows:
        if tz_name and local_hour is not None:
            disp_h, disp_m, disp_wd, tz_label = local_hour, local_minute, local_weekday, tz_name
        else:
            disp_h, disp_m, disp_wd, tz_label = hour, minute, weekday, "UTC"
        when = f"daily {disp_h:02d}:{disp_m:02d}" if kind == "daily" else f"{wdays[disp_wd]} {disp_h:02d}:{disp_m:02d}"
        snippet = text if len(text) <= 40 else text[:37] + "..."
        lines.append(f"#{pid} — {when} {tz_label} — {snippet}")
    await update.effective_message.reply_text("\n".join(lines))


async def scheduledel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /scheduledel <id>")
        return
    post_id = int(context.args[0])
    con = _db()
    con.execute("DELETE FROM scheduled_posts WHERE id=? AND chat_id=?", (post_id, update.effective_chat.id))
    con.commit()
    con.close()
    if context.job_queue:
        for job in context.job_queue.get_jobs_by_name(f"scheduled_post:{post_id}"):
            job.schedule_removal()
    await update.effective_message.reply_text("Removed.")


def _reload_scheduled_posts(app: Application) -> None:
    con = _db()
    rows = con.execute("SELECT id, kind, hour, minute, weekday FROM scheduled_posts").fetchall()
    con.close()
    for post_id, kind, hour, minute, weekday in rows:
        _register_scheduled_post(app, post_id, kind, hour, minute, weekday)


def _config_columns() -> list[str]:
    con = _db()
    cols = [r[1] for r in con.execute("PRAGMA table_info(settings)").fetchall() if r[1] != "chat_id"]
    con.close()
    return cols


def _export_config(chat_id: int) -> dict:
    con = _db()
    cols = _config_columns()
    row = con.execute(f"SELECT {', '.join(cols)} FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    settings = dict(zip(cols, row)) if row else {}
    filters_rows = con.execute(
        "SELECT word, action, is_regex FROM filters WHERE chat_id=?", (chat_id,)
    ).fetchall()
    scam_rows = con.execute("SELECT value, kind FROM local_scam WHERE chat_id=?", (chat_id,)).fetchall()
    wl_rows = con.execute("SELECT domain FROM link_whitelist WHERE chat_id=?", (chat_id,)).fetchall()
    faq_rows = con.execute(
        "SELECT question, answer, keywords FROM faq WHERE chat_id=?", (chat_id,)
    ).fetchall()
    welcome_rows = con.execute(
        "SELECT text FROM welcome_variants WHERE chat_id=? ORDER BY id", (chat_id,)
    ).fetchall()
    con.close()
    return {
        "settings": settings,
        "filters": [{"word": w, "action": a, "is_regex": bool(r)} for w, a, r in filters_rows],
        "local_scam": [{"value": v, "kind": k} for v, k in scam_rows],
        "link_whitelist": [w[0] for w in wl_rows],
        "faq": [{"question": q, "answer": a, "keywords": k} for q, a, k in faq_rows],
        "welcome_variants": [w[0] for w in welcome_rows],
    }


def _apply_config(chat_id: int, config: dict) -> None:
    con = _db()
    con.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
    settings = config.get("settings") or {}
    valid_cols = set(_config_columns())
    cols = [c for c in settings if c in valid_cols]
    if cols:
        set_clause = ", ".join(f"{c}=?" for c in cols)
        con.execute(f"UPDATE settings SET {set_clause} WHERE chat_id=?", [settings[c] for c in cols] + [chat_id])
    con.execute("DELETE FROM filters WHERE chat_id=?", (chat_id,))
    for f in config.get("filters") or []:
        con.execute(
            "INSERT OR REPLACE INTO filters(chat_id, word, action, is_regex) VALUES(?,?,?,?)",
            (chat_id, f.get("word"), f.get("action", "mute"), int(bool(f.get("is_regex")))),
        )
    con.execute("DELETE FROM local_scam WHERE chat_id=?", (chat_id,))
    for s in config.get("local_scam") or []:
        con.execute(
            "INSERT OR REPLACE INTO local_scam(chat_id, value, kind, added_by, ts) VALUES(?,?,?,0,strftime('%s','now'))",
            (chat_id, s.get("value"), s.get("kind", "ca")),
        )
    con.execute("DELETE FROM link_whitelist WHERE chat_id=?", (chat_id,))
    for d in config.get("link_whitelist") or []:
        con.execute("INSERT OR IGNORE INTO link_whitelist(chat_id, domain) VALUES(?,?)", (chat_id, d))
    if "faq" in config:
        con.execute("DELETE FROM faq WHERE chat_id=?", (chat_id,))
        for f in config.get("faq") or []:
            con.execute(
                "INSERT INTO faq(chat_id, question, answer, keywords, created_by, ts) "
                "VALUES(?,?,?,?,0,strftime('%s','now'))",
                (chat_id, f.get("question"), f.get("answer"), f.get("keywords", "")),
            )
    if "welcome_variants" in config:
        con.execute("DELETE FROM welcome_variants WHERE chat_id=?", (chat_id,))
        for text in config.get("welcome_variants") or []:
            con.execute(
                "INSERT INTO welcome_variants(chat_id, text, ts) VALUES(?,?,strftime('%s','now'))",
                (chat_id, text),
            )
    con.commit()
    con.close()
    _local_scam_cache[chat_id]["ts"] = 0
    _whitelist_cache[chat_id]["ts"] = 0
    _scam_cache["ts"] = 0


async def exportconfig_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    data = json.dumps(_export_config(chat_id), indent=2).encode("utf-8")
    bio = BytesIO(data)
    bio.name = f"guardian_config_{chat_id}.json"
    await update.effective_message.reply_document(
        bio,
        caption="📦 Guardian config export — settings, filters, scam list, link whitelist, FAQs, and welcome "
        "variants.\nReply to this file with /importconfig in another group to apply it all there.",
    )


async def importconfig_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    msg = update.effective_message
    doc = msg.reply_to_message.document if msg.reply_to_message else None
    if not doc:
        await msg.reply_text("Reply to a Guardian config .json file (from /exportconfig) with /importconfig.")
        return
    try:
        file = await context.bot.get_file(doc.file_id)
        buf = BytesIO()
        await file.download_to_memory(buf)
        config = json.loads(buf.getvalue().decode("utf-8"))
    except Exception as exc:
        log.warning("importconfig %s", exc)
        await msg.reply_text("Couldn't read that file — make sure it's a Guardian config export.")
        return
    _apply_config(update.effective_chat.id, config)
    await msg.reply_text(
        "✅ Config imported — filters, scam list, link whitelist, settings"
        + (", FAQs" if "faq" in config else "")
        + (", welcome variants" if "welcome_variants" in config else "")
        + " applied to this group."
    )


async def cloneconfig_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.effective_message.reply_text(
            "Usage: /cloneconfig <source_chat_id> — you must be an admin in both groups"
        )
        return
    source_id = int(context.args[0])
    try:
        member = await context.bot.get_chat_member(source_id, update.effective_user.id)
    except Exception:
        await update.effective_message.reply_text(
            "Couldn't check that chat — make sure I'm also a member there, and the id is right."
        )
        return
    if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await update.effective_message.reply_text("You need to be an admin in the source group too.")
        return
    _apply_config(update.effective_chat.id, _export_config(source_id))
    await update.effective_message.reply_text(f"✅ Cloned config from {source_id} into this group.")


async def setwelcome(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    msg = update.effective_message
    parts = (msg.text or msg.caption or "").split(None, 1)
    text = parts[1] if len(parts) > 1 else ""
    media_id, media_type = None, None
    if msg.photo:
        media_id, media_type = msg.photo[-1].file_id, "photo"
    elif msg.animation:
        media_id, media_type = msg.animation.file_id, "animation"
    elif msg.video:
        media_id, media_type = msg.video.file_id, "video"
    elif msg.reply_to_message:
        rmsg = msg.reply_to_message
        if rmsg.photo:
            media_id, media_type = rmsg.photo[-1].file_id, "photo"
        elif rmsg.animation:
            media_id, media_type = rmsg.animation.file_id, "animation"
        elif rmsg.video:
            media_id, media_type = rmsg.video.file_id, "video"
        if not text.strip():
            text = rmsg.text or rmsg.caption or ""
    if not text.strip() and not media_id:
        _pending_welcome_media[update.effective_chat.id] = (update.effective_user.id, time.time() + 300)
        await update.effective_message.reply_text(
            "📎 Now just upload the photo, GIF, or video you want as the welcome media — no "
            "caption or command needed, I'll grab the next one you post here within 5 min.\n"
            "Or send /setwelcome <text> any time to set/update the welcome text."
        )
        return
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, welcome_text, welcome_media_id, welcome_media_type) VALUES(?,?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET welcome_text=excluded.welcome_text, "
        "welcome_media_id=COALESCE(excluded.welcome_media_id, settings.welcome_media_id), "
        "welcome_media_type=COALESCE(excluded.welcome_media_type, settings.welcome_media_type)",
        (update.effective_chat.id, text.strip(), media_id, media_type),
    )
    con.commit()
    con.close()
    note = f" with a {media_type}" if media_type else ""
    await update.effective_message.reply_text(f"Welcome message saved{note}.")


async def _welcome_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Companion to a bare /setwelcome: grabs the very next photo/GIF/video the SAME admin who
    ran it posts in this chat and attaches it as the welcome media — no caption or reply needed."""
    msg = update.effective_message
    chat_id = update.effective_chat.id
    entry = _pending_welcome_media.get(chat_id)
    if not entry:
        return
    armed_by, expiry = entry
    if time.time() > expiry:
        _pending_welcome_media.pop(chat_id, None)
        return
    if not update.effective_user or update.effective_user.id != armed_by:
        return
    media_id, media_type = None, None
    if msg.photo:
        media_id, media_type = msg.photo[-1].file_id, "photo"
    elif msg.animation:
        media_id, media_type = msg.animation.file_id, "animation"
    elif msg.video:
        media_id, media_type = msg.video.file_id, "video"
    if not media_id:
        return
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, welcome_media_id, welcome_media_type) VALUES(?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET welcome_media_id=excluded.welcome_media_id, "
        "welcome_media_type=excluded.welcome_media_type",
        (chat_id, media_id, media_type),
    )
    con.commit()
    con.close()
    _pending_welcome_media.pop(chat_id, None)
    try:
        await msg.reply_text(f"✅ Welcome {media_type} saved.")
    except Exception as exc:
        log.warning("welcome media confirm %s", exc)


async def _note_media_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Companion to a bare /save <name>: grabs the next photo/GIF/video the SAME admin who ran
    it posts in this chat and attaches it to that note — no caption or reply needed."""
    msg = update.effective_message
    chat_id = update.effective_chat.id
    entry = _pending_note_media.get(chat_id)
    if not entry:
        return
    armed_by, note_name, expiry = entry
    if time.time() > expiry:
        _pending_note_media.pop(chat_id, None)
        return
    if not update.effective_user or update.effective_user.id != armed_by:
        return
    media_id, media_type = None, None
    if msg.photo:
        media_id, media_type = msg.photo[-1].file_id, "photo"
    elif msg.animation:
        media_id, media_type = msg.animation.file_id, "animation"
    elif msg.video:
        media_id, media_type = msg.video.file_id, "video"
    if not media_id:
        return
    con = _db()
    con.execute(
        "INSERT INTO notes(chat_id, name, text, media_id, media_type) VALUES(?,?,?,?,?) "
        "ON CONFLICT(chat_id, name) DO UPDATE SET "
        "media_id=excluded.media_id, media_type=excluded.media_type",
        (chat_id, note_name, "", media_id, media_type),
    )
    con.commit()
    con.close()
    _pending_note_media.pop(chat_id, None)
    try:
        await msg.reply_text(f"✅ #{note_name} {media_type} saved.")
    except Exception as exc:
        log.warning("note media confirm %s", exc)


async def _slash_filter_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lets a saved filter be pulled with /<name> in addition to #<name>. Runs in its own low
    -priority handler group, so it only fires for slash-words that are not already a real
    registered bot command (those get handled first, in their own groups, as normal)."""
    msg = update.effective_message
    if not msg or not msg.text:
        return
    chat_id = update.effective_chat.id
    cmd = msg.text.split()[0][1:].split("@")[0].strip().lower()
    if not cmd:
        return
    con = _db()
    row = con.execute(
        "SELECT text, media_id, media_type FROM notes WHERE chat_id=? AND name=?", (chat_id, cmd)
    ).fetchone()
    con.close()
    if not row:
        return
    note_text, note_media_id, note_media_type = row
    try:
        if note_media_id and note_media_type == "photo":
            await context.bot.send_photo(chat_id, note_media_id, caption=note_text or None)
        elif note_media_id and note_media_type == "animation":
            await context.bot.send_animation(chat_id, note_media_id, caption=note_text or None)
        elif note_media_id and note_media_type == "video":
            await context.bot.send_video(chat_id, note_media_id, caption=note_text or None)
        else:
            await msg.reply_text(note_text)
    except Exception as exc:
        log.warning("slash filter trigger %s", exc)


async def _pending_text_capture(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Companion to a bare /setwelcome or /filter <name>: if the very next message from that
    same admin is plain text instead of media, use it as the text — so sending the arm command
    and the content as two separate messages still works, not just captions/replies."""
    msg = update.effective_message
    if not msg or not msg.text:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else None

    w_entry = _pending_welcome_media.get(chat_id)
    if w_entry:
        armed_by, expiry = w_entry
        if time.time() > expiry:
            _pending_welcome_media.pop(chat_id, None)
        elif user_id == armed_by:
            con = _db()
            con.execute(
                "INSERT INTO settings(chat_id, welcome_text) VALUES(?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET welcome_text=excluded.welcome_text",
                (chat_id, msg.text.strip()),
            )
            con.commit()
            con.close()
            _pending_welcome_media.pop(chat_id, None)
            try:
                await msg.reply_text("✅ Welcome text saved.")
            except Exception as exc:
                log.warning("welcome text confirm %s", exc)
            return

    n_entry = _pending_note_media.get(chat_id)
    if n_entry:
        armed_by, note_name, expiry = n_entry
        if time.time() > expiry:
            _pending_note_media.pop(chat_id, None)
        elif user_id == armed_by:
            con = _db()
            con.execute(
                "INSERT INTO notes(chat_id, name, text) VALUES(?,?,?) "
                "ON CONFLICT(chat_id, name) DO UPDATE SET text=excluded.text",
                (chat_id, note_name, msg.text.strip()),
            )
            con.commit()
            con.close()
            _pending_note_media.pop(chat_id, None)
            try:
                await msg.reply_text(f"✅ #{note_name} text saved.")
            except Exception as exc:
                log.warning("note text confirm %s", exc)
            return


async def testwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin-only: sends a live preview of the current welcome message/media to this chat,
    exactly as a new member would see it (captcha/rules-gate prompts aside)."""
    if not await _is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    user = update.effective_user
    welcome_text, welcome_on = _welcome_settings(chat_id)
    variants = _welcome_variants(chat_id)
    if variants:
        pool = variants + ([welcome_text] if welcome_text else [])
        chosen_text = random.choice(pool)
    else:
        chosen_text = welcome_text or _default_welcome_for(user.language_code)
    greeting = chosen_text.format(
        first=_esc(user.first_name or user.full_name),
        chatname=_esc(update.effective_chat.title or ""),
    )
    welcome_media_id, welcome_media_type = _welcome_media(chat_id)
    btn_label, btn_url = _welcome_btn(chat_id)
    kb = (
        InlineKeyboardMarkup([[InlineKeyboardButton(btn_label, url=btn_url)]])
        if btn_label and btn_url
        else None
    )
    preview = (
        f"👁 Preview (welcome messages are currently {'ON' if welcome_on else 'OFF'}):\n\n"
        f"{greeting}"
    )
    try:
        if welcome_media_id and welcome_media_type == "photo":
            await context.bot.send_photo(chat_id, welcome_media_id, caption=preview, reply_markup=kb)
        elif welcome_media_id and welcome_media_type == "animation":
            await context.bot.send_animation(chat_id, welcome_media_id, caption=preview, reply_markup=kb)
        elif welcome_media_id and welcome_media_type == "video":
            await context.bot.send_video(chat_id, welcome_media_id, caption=preview, reply_markup=kb)
        else:
            await context.bot.send_message(chat_id, preview, reply_markup=kb)
    except Exception as exc:
        log.warning("testwelcome %s", exc)
        await update.effective_message.reply_text(f"Preview failed to send: {exc}")


async def addwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    parts = (update.effective_message.text or "").split(None, 1)
    text = parts[1].strip() if len(parts) > 1 else ""
    if not text:
        await update.effective_message.reply_text(
            "Usage: /addwelcome Hey {first}, welcome to {chatname}! 🎉\nAdd a few and Guardian rotates between them."
        )
        return
    con = _db()
    con.execute(
        "INSERT INTO welcome_variants(chat_id, text, ts) VALUES(?,?,strftime('%s','now'))",
        (update.effective_chat.id, text),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text("Welcome variant added.")


async def welcomevariants_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    rows = con.execute(
        "SELECT id, text FROM welcome_variants WHERE chat_id=? ORDER BY id", (update.effective_chat.id,)
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text(
            "No welcome variants yet — /setwelcome's message is used every time. Add some with /addwelcome."
        )
        return
    lines = ["🔀 <b>Welcome variants</b>"] + [f"#{vid} — {_esc(t[:80])}" for vid, t in rows]
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def delwelcome_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /delwelcome <id>")
        return
    con = _db()
    con.execute(
        "DELETE FROM welcome_variants WHERE id=? AND chat_id=?",
        (int(context.args[0]), update.effective_chat.id),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text("Removed.")


async def welcome_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("Usage: /welcome on|off")
        return
    val = 1 if context.args[0].lower() == "on" else 0
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, welcome_enabled) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET welcome_enabled=excluded.welcome_enabled",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Welcome messages: {'ON' if val else 'OFF'}")


async def setwelcomebtn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    parts = (update.effective_message.text or "").split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""
    if "|" not in rest:
        await update.effective_message.reply_text("Usage: /setwelcomebtn Label | https://your-link.com")
        return
    label, url = (p.strip() for p in rest.split("|", 1))
    if not label or not url.lower().startswith(("http://", "https://", "t.me/")):
        await update.effective_message.reply_text("Usage: /setwelcomebtn Label | https://your-link.com")
        return
    if url.lower().startswith("t.me/"):
        url = "https://" + url
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, welcome_btn_label, welcome_btn_url) VALUES(?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET welcome_btn_label=excluded.welcome_btn_label, welcome_btn_url=excluded.welcome_btn_url",
        (update.effective_chat.id, label, url),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Welcome button set: {_esc(label)} → {_esc(url)}")


async def delwelcomebtn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    con = _db()
    con.execute(
        "UPDATE settings SET welcome_btn_label=NULL, welcome_btn_url=NULL WHERE chat_id=?",
        (update.effective_chat.id,),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text("Welcome button removed.")


async def setgoodbye(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    parts = (update.effective_message.text or "").split(None, 1)
    text = parts[1] if len(parts) > 1 else ""
    if not text.strip():
        await update.effective_message.reply_text("Usage: /setgoodbye Bye {first}, we'll miss you!")
        return
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, goodbye_text) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET goodbye_text=excluded.goodbye_text",
        (update.effective_chat.id, text.strip()),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text("Goodbye message saved.")


async def goodbye_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("Usage: /goodbye on|off")
        return
    val = 1 if context.args[0].lower() == "on" else 0
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, goodbye_enabled) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET goodbye_enabled=excluded.goodbye_enabled",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Goodbye messages: {'ON' if val else 'OFF'}")


async def cleanservice_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text("Usage: /cleanservice on|off")
        return
    val = 1 if context.args[0].lower() == "on" else 0
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, cleanservice_enabled) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET cleanservice_enabled=excluded.cleanservice_enabled",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(
        f"Clean Service (auto-delete join/left notices): {'ON' if val else 'OFF'}"
    )


def _rules_text(chat_id: int) -> str | None:
    con = _db()
    row = con.execute("SELECT rules_text FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    con.close()
    return row[0] if row and row[0] else None


async def rules_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _rules_text(update.effective_chat.id) or "No rules set yet. An admin can run /setrules to add them."
    await update.effective_message.reply_text(text)


async def grulesgate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text(
            "Usage: /grulesgate on|off — new members must tap 'I agree' on the rules before they can "
            "post. If join captcha is also on, captcha takes priority and this won't trigger — turn "
            "captcha off (/gcaptcha off) to use this instead."
        )
        return
    val = 1 if context.args[0].lower() == "on" else 0
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, rules_gate_enabled) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET rules_gate_enabled=excluded.rules_gate_enabled",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Rules-acceptance gate: {'ON' if val else 'OFF'}")


async def setrules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    parts = (update.effective_message.text or "").split(None, 1)
    text = parts[1] if len(parts) > 1 else ""
    if not text.strip():
        await update.effective_message.reply_text("Usage: /setrules <text>")
        return
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, rules_text) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET rules_text=excluded.rules_text",
        (update.effective_chat.id, text.strip()),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text("Rules saved.")


async def gcaptcha(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    args = context.args or []
    if args and args[0].lower() == "mode":
        if len(args) < 2 or args[1].lower() not in ("simple", "math"):
            await update.effective_message.reply_text("Usage: /gcaptcha mode simple|math")
            return
        mode = "button" if args[1].lower() == "simple" else "math"
        con = _db()
        con.execute(
            "INSERT INTO settings(chat_id, captcha_mode) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET captcha_mode=excluded.captcha_mode",
            (update.effective_chat.id, mode),
        )
        con.commit()
        con.close()
        _config_audit(update.effective_chat.id, update.effective_user.id, "gcaptcha_mode", args[1].lower())
        await update.effective_message.reply_text(f"Captcha mode: {args[1].lower()}")
        return
    if not args or args[0].lower() not in ("on", "off"):
        await update.effective_message.reply_text(
            "Usage: /gcaptcha on|off\n/gcaptcha mode simple|math — math is harder to click through blind"
        )
        return
    val = 1 if args[0].lower() == "on" else 0
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, captcha_enabled) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET captcha_enabled=excluded.captcha_enabled",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    _config_audit(update.effective_chat.id, update.effective_user.id, "gcaptcha", "on" if val else "off")
    await update.effective_message.reply_text(f"Join captcha: {'ON' if val else 'OFF'}")


async def gnewacct(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    args = context.args or []
    if not args or args[0].lower() not in ("on", "off"):
        _, cur_min = _newacct_settings(update.effective_chat.id)
        await update.effective_message.reply_text(
            "Usage: /gnewacct on|off — restrict very-recently-created accounts for "
            f"{NEWACCT_RESTRICT_SECONDS // 60} min on join (rough heuristic, off by default)\n"
            f"/gnewacct threshold <user_id> — set the id cutoff (current: {cur_min})"
        )
        return
    val = 1 if args[0].lower() == "on" else 0
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, newacct_enabled) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET newacct_enabled=excluded.newacct_enabled",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"New-account scrutiny: {'ON' if val else 'OFF'}")


async def gnewacct_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /gnewacctid 7800000000")
        return
    val = int(context.args[0])
    con = _db()
    con.execute(
        "INSERT INTO settings(chat_id, newacct_min_id) VALUES(?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET newacct_min_id=excluded.newacct_min_id",
        (update.effective_chat.id, val),
    )
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"New-account id threshold set to {val}.")


async def gshadowban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not update.message or not update.message.reply_to_message:
        await update.effective_message.reply_text(
            "Reply to a user with /gshadowban — their messages get silently deleted from then on, "
            "no ban, no notice to them."
        )
        return
    uid = update.message.reply_to_message.from_user.id
    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO shadowbanned(chat_id, user_id, by_id, ts) VALUES(?,?,?,strftime('%s','now'))",
        (update.effective_chat.id, uid, update.effective_user.id),
    )
    con.commit()
    con.close()
    try:
        await update.message.reply_to_message.delete()
    except Exception:
        pass
    await update.effective_message.reply_text(f"👻 {uid} is now shadowbanned in this group.")


async def gunshadowban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    uid = None
    if update.message and update.message.reply_to_message:
        uid = update.message.reply_to_message.from_user.id
    elif context.args:
        try:
            uid = int(context.args[0])
        except ValueError:
            uid = None
    if not uid:
        await update.effective_message.reply_text("Reply to a user with /gunshadowban, or /gunshadowban 123456789")
        return
    con = _db()
    con.execute("DELETE FROM shadowbanned WHERE chat_id=? AND user_id=?", (update.effective_chat.id, uid))
    con.commit()
    con.close()
    await update.effective_message.reply_text(f"Lifted shadowban on {uid}.")


async def del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_mod(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("Reply to a message with /del to remove it.")
        return
    try:
        await msg.reply_to_message.delete()
    except Exception as exc:
        log.warning("del %s", exc)
    try:
        await msg.delete()
    except Exception:
        pass


async def purge_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_mod(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("Reply to the message you want to purge from, then run /purge.")
        return
    start_id = msg.reply_to_message.message_id
    end_id = msg.message_id
    if end_id - start_id > PURGE_MAX:
        start_id = end_id - PURGE_MAX
    chat_id = update.effective_chat.id
    removed = 0
    for mid in range(start_id, end_id + 1):
        try:
            await context.bot.delete_message(chat_id, mid)
            removed += 1
        except Exception:
            pass
    try:
        note = await context.bot.send_message(chat_id, f"\U0001F9F9 Purged {removed} messages.")
        if context.job_queue:
            context.job_queue.run_once(
                _delete_later, 5, data={"chat_id": chat_id, "message_id": note.message_id}
            )
    except Exception:
        pass


async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in LOCK_TYPES:
        await update.effective_message.reply_text("Usage: /lock links|forwards|stickers|photos|voice|videonote")
        return
    col = LOCK_TYPES[context.args[0].lower()]
    con = _db()
    con.execute(
        f"INSERT INTO settings(chat_id, {col}) VALUES(?,1) ON CONFLICT(chat_id) DO UPDATE SET {col}=1",
        (update.effective_chat.id,),
    )
    con.commit()
    con.close()
    _config_audit(update.effective_chat.id, update.effective_user.id, "lock", context.args[0].lower())
    await update.effective_message.reply_text(f"\U0001F512 Locked: {context.args[0].lower()}")


async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    if not context.args or context.args[0].lower() not in LOCK_TYPES:
        await update.effective_message.reply_text("Usage: /unlock links|forwards|stickers|photos|voice|videonote")
        return
    col = LOCK_TYPES[context.args[0].lower()]
    con = _db()
    con.execute(
        f"INSERT INTO settings(chat_id, {col}) VALUES(?,0) ON CONFLICT(chat_id) DO UPDATE SET {col}=0",
        (update.effective_chat.id,),
    )
    con.commit()
    con.close()
    _config_audit(update.effective_chat.id, update.effective_user.id, "unlock", context.args[0].lower())
    await update.effective_message.reply_text(f"\U0001F513 Unlocked: {context.args[0].lower()}")


async def info_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = msg.reply_to_message.from_user if msg.reply_to_message else update.effective_user
    chat_id = update.effective_chat.id
    con = _db()
    jrow = con.execute(
        "SELECT joined_ts FROM joins WHERE chat_id=? AND user_id=?", (chat_id, user.id)
    ).fetchone()
    wrow = con.execute(
        "SELECT count FROM warns WHERE chat_id=? AND user_id=?", (chat_id, user.id)
    ).fetchone()
    brow = con.execute("SELECT reason, ts FROM global_bans WHERE user_id=?", (user.id,)).fetchone()
    trow = con.execute(
        "SELECT first_seen, groups_seen, strikes_ever FROM user_trust WHERE user_id=?", (user.id,)
    ).fetchone()
    con.close()
    joined = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(jrow[0])) if jrow else "unknown"
    warns_n = wrow[0] if wrow else 0
    ban_line = f"\U0001F6AB Globally banned ({_esc(brow[0])})" if brow else "✅ Not globally banned"
    if trow:
        first_seen, groups_seen, strikes_ever = trow
        since = time.strftime("%Y-%m-%d", time.gmtime(first_seen)) if first_seen else "unknown"
        trust_line = (
            f"🌐 Ecosystem-wide: seen in {groups_seen or 1} Ferzan group(s) since {since}, "
            f"{strikes_ever} strike(s) ever — {'✅ trusted veteran' if _is_trusted(user.id) else '— not yet trusted'}"
        )
    else:
        trust_line = "🌐 Ecosystem-wide: no record yet"
    text = (
        f"\U0001F464 <b>{_esc(user.full_name)}</b> (<code>{user.id}</code>)\n"
        f"Joined this chat: {joined}\n"
        f"Strikes: {warns_n}/{WARN_LIMIT}\n"
        f"{ban_line}\n"
        f"{trust_line}"
    )
    await msg.reply_text(text, parse_mode="HTML")


async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    try:
        members = await context.bot.get_chat_administrators(chat_id)
    except Exception as exc:
        log.warning("admins list %s", exc)
        await update.effective_message.reply_text("Couldn't fetch admin list.")
        return
    lines = ["\U0001F451 <b>Admins</b>"]
    for m in members:
        u = m.user
        name = _esc(u.full_name)
        lines.append(f"• {name}" + (f" (@{_esc(u.username)})" if u.username else ""))
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    msg = update.effective_message
    if not msg.reply_to_message:
        await msg.reply_text("Reply to a message with /pin to pin it.")
        return
    try:
        await context.bot.pin_chat_message(update.effective_chat.id, msg.reply_to_message.message_id)
    except Exception as exc:
        log.warning("pin %s", exc)


async def unpin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_admin(update, context):
        return
    try:
        if update.effective_message.reply_to_message:
            await context.bot.unpin_chat_message(
                update.effective_chat.id, update.effective_message.reply_to_message.message_id
            )
        else:
            await context.bot.unpin_chat_message(update.effective_chat.id)
    except Exception as exc:
        log.warning("unpin %s", exc)


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    chat_id = update.effective_chat.id
    if not msg.reply_to_message:
        await msg.reply_text("Reply to the message you want to report with /report")
        return
    remaining = _check_cooldown(user.id, chat_id, "report", REPORT_COOLDOWN_SECONDS)
    if remaining > 0:
        await msg.reply_text(f"Slow down — try again in {int(remaining)}s.")
        return
    target = msg.reply_to_message
    reporter = user
    report_text = (target.text or target.caption or "")[:300]
    con = _db()
    cur = con.execute(
        "INSERT INTO reports(chat_id, reporter_id, target_id, message_id, text, status, ts) "
        "VALUES(?,?,?,?,?,'open',strftime('%s','now'))",
        (
            chat_id,
            reporter.id,
            target.from_user.id if target.from_user else 0,
            target.message_id,
            report_text,
            ),
    )
    report_id = cur.lastrowid
    con.commit()
    con.close()
    text = (
        f"\U0001F6A9 <b>Report #{report_id}</b> in {_esc(update.effective_chat.title or update.effective_chat.id)}\n"
        f"By: {_esc(reporter.full_name)}\n"
        f"From: {_esc(target.from_user.full_name if target.from_user else 'unknown')}\n"
        f"Text: {_esc(report_text)}"
    )
    kb = InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ Resolve", callback_data=f"rp:resolve:{report_id}")]]
    )
    if _log_chat_for(chat_id):
        await _log(context, text, chat_id, kb)
    else:
        admins = []
        try:
            members = await context.bot.get_chat_administrators(chat_id)
            admins = [m.user for m in members if not m.user.is_bot]
        except Exception:
            pass
        pings = " ".join(f'<a href="tg://user?id={a.id}">{_esc(a.first_name)}</a>' for a in admins[:5])
        if pings:
            try:
                await msg.reply_text(
                    f"\U0001F6A9 Reported to admins (#{report_id}). {pings}", parse_mode="HTML"
                )
            except Exception as exc:
                log.warning("report ping %s", exc)
    await msg.reply_text(f"Thanks — admins notified (report #{report_id}).")


async def reports_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_mod(update, context):
        return
    chat_id = update.effective_chat.id
    con = _db()
    rows = con.execute(
        "SELECT id, reporter_id, target_id, text, ts FROM reports "
        "WHERE chat_id=? AND status='open' ORDER BY id DESC LIMIT 20",
        (chat_id,),
    ).fetchall()
    con.close()
    if not rows:
        await update.effective_message.reply_text("No open reports. \U0001F389")
        return
    lines = ["\U0001F4CB <b>Open reports</b>"]
    for rid, reporter_id, target_id, text, ts in rows:
        lines.append(
            f"#{rid} — target <code>{target_id}</code> by <code>{reporter_id}</code>: "
            f"{_esc((text or '')[:80])}"
        )
    lines.append("\nUse /resolve &lt;id&gt; to close one.")
    await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")


async def resolve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _is_mod(update, context):
        return
    args = context.args
    if not args or not args[0].isdigit():
        await update.effective_message.reply_text("Usage: /resolve <report id>")
        return
    report_id = int(args[0])
    chat_id = update.effective_chat.id
    con = _db()
    con.execute(
        "UPDATE reports SET status='resolved', resolved_by=? WHERE id=? AND chat_id=?",
        (update.effective_user.id, report_id, chat_id),
    )
    changed = con.total_changes
    con.commit()
    con.close()
    if changed:
        _audit(chat_id, update.effective_user.id, 0, "resolve", f"report #{report_id}")
        await update.effective_message.reply_text(f"Report #{report_id} resolved.")
    else:
        await update.effective_message.reply_text("No such open report here.")


async def report_resolve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    try:
        _, action, report_id_s = (q.data or "").split(":")
        report_id = int(report_id_s)
    except Exception:
        await q.answer()
        return
    con = _db()
    row = con.execute("SELECT chat_id, status FROM reports WHERE id=?", (report_id,)).fetchone()
    if not row:
        con.close()
        await q.answer("Report not found.", show_alert=True)
        return
    chat_id, status = row
    try:
        clicker = await context.bot.get_chat_member(chat_id, q.from_user.id)
        if clicker.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            con.close()
            await q.answer("Admins of that group only.", show_alert=True)
            return
    except Exception:
        con.close()
        await q.answer("Couldn't verify admin status there.", show_alert=True)
        return
    if status == "resolved":
        con.close()
        await q.answer("Already resolved.")
        return
    con.execute(
        "UPDATE reports SET status='resolved', resolved_by=? WHERE id=?", (q.from_user.id, report_id)
    )
    con.commit()
    con.close()
    _audit(chat_id, q.from_user.id, 0, "resolve", f"report #{report_id} (button)")
    await q.answer("Resolved.")
    try:
        old_text = q.message.text_html or q.message.text or ""
        await q.edit_message_text(
            f"{old_text}\n\n✅ Resolved by {_esc(q.from_user.full_name)}", parse_mode="HTML"
        )
    except Exception as exc:
        log.warning("resolve edit %s", exc)


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


async def _admin_users(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> list:
    try:
        members = await context.bot.get_chat_administrators(chat_id)
        return [m.user for m in members if not m.user.is_bot]
    except Exception:
        return []


async def on_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cmu = update.chat_member
    if not cmu:
        return
    old = cmu.old_chat_member
    new = cmu.new_chat_member
    chat_id = update.effective_chat.id

    if old.status != new.status and ChatMemberStatus.ADMINISTRATOR in (old.status, new.status):
        # Anti-nuke: alert on any admin-role change, whoever made it — a compromised owner account
        # quietly promoting a fake "admin" is a real attack pattern in crypto groups.
        actor = cmu.from_user
        target = new.user
        promoted = new.status == ChatMemberStatus.ADMINISTRATOR
        change = "promoted to admin" if promoted else "demoted from admin"
        chat_title = update.effective_chat.title or str(chat_id)
        text = (
            f"⚡ <b>{_esc(target.full_name)}</b> ({target.id}) {change} in <b>{_esc(chat_title)}</b> "
            f"by {_esc(actor.full_name if actor else 'unknown')} ({actor.id if actor else '?'})."
        )
        await _log(context, text, chat_id)
        for admin in await _admin_users(context, chat_id):
            try:
                await context.bot.send_message(admin.id, text, parse_mode="HTML")
            except Exception:
                pass
        return

    if (
        new.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED)
        and old.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED, ChatMemberStatus.ADMINISTRATOR)
    ):
        goodbye_text, goodbye_on = _goodbye_settings(chat_id)
        if goodbye_on:
            farewell = (goodbye_text or _default_goodbye_for(new.user.language_code)).format(
                first=_esc(new.user.first_name or new.user.full_name),
                chatname=_esc(update.effective_chat.title or ""),
            )
            try:
                await context.bot.send_message(chat_id, farewell)
            except Exception as exc:
                log.warning("goodbye %s", exc)
        return

    if new.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        return
    user = new.user
    now_ts = time.time()
    _touch_chat(chat_id, update.effective_chat.title or "")

    invite_link_obj = getattr(cmu, "invite_link", None)
    if invite_link_obj is not None:
        try:
            con = _db()
            con.execute(
                "INSERT INTO join_invite_links(chat_id, user_id, invite_link, link_name, ts) VALUES(?,?,?,?,?)",
                (chat_id, user.id, invite_link_obj.invite_link, invite_link_obj.name or "", int(now_ts)),
            )
            con.commit()
            con.close()
        except Exception as exc:
            log.warning("invite link record %s", exc)

    dq = _raid_joins[chat_id]
    dq.append(now_ts)
    while dq and now_ts - dq[0] > RAID_WINDOW_SECONDS:
        dq.popleft()
    if len(dq) >= RAID_THRESHOLD and _raid_lock_until[chat_id] < now_ts:
        _raid_lock_until[chat_id] = now_ts + RAID_LOCK_SECONDS
        top_link_note = ""
        try:
            top_link = _raid_top_invite_link(chat_id, now_ts - RAID_WINDOW_SECONDS)
            if top_link:
                link_url, link_name, cnt = top_link
                label = link_name or link_url
                top_link_note = f"\n🔗 {cnt} of these joined via <code>{_esc(label)}</code> — /grevokeinvite to kill it."
        except Exception:
            pass
        try:
            await context.bot.send_message(
                chat_id,
                f"\U0001F6A8 <b>Raid detected</b> — {len(dq)} joins in {RAID_WINDOW_SECONDS}s.\n"
                f"🔒 Full lockdown for {RAID_LOCK_SECONDS // 60} min: non-admin messages are auto-removed "
                f"and new joins are muted. Run /graidoff to lift it early." + top_link_note,
                parse_mode="HTML",
            )
        except Exception:
            pass
        chat_title = update.effective_chat.title or str(chat_id)
        await _log(
            context,
            f"\U0001F6A8 Raid detected in {chat_id}: {len(dq)} joins in {RAID_WINDOW_SECONDS}s.{top_link_note}",
            chat_id,
        )

    has_photo = True
    try:
        photos = await context.bot.get_user_profile_photos(user.id, limit=1)
        has_photo = bool(photos and photos.total_count > 0)
    except Exception:
        pass
    nophoto_burst = False
    if not has_photo:
        npdq = _raid_nophoto[chat_id]
        npdq.append(now_ts)
        while npdq and now_ts - npdq[0] > RAID_WINDOW_SECONDS:
            npdq.popleft()
        nophoto_burst = len(npdq) >= NOPHOTO_RAID_THRESHOLD
        for admin in await _admin_users(context, chat_id):
            try:
                await context.bot.send_message(
                    admin.id,
                    f"🚨 Raid detected in <b>{_esc(chat_title)}</b> — {len(dq)} joins in {RAID_WINDOW_SECONDS}s. "
                    f"Guardian has locked the chat down for {RAID_LOCK_SECONDS // 60} min.",
                    parse_mode="HTML",
                )
            except Exception:
                pass  # admin hasn't started a DM with the bot — nothing we can do

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
        else:
            await _log(
                context,
                f"\U0001F6E1 Blocked join: {_esc(user.full_name)} ({user.id}) in {chat_id} — impersonator/listed ban.",
            )
        return
    admins = await _admins(context, chat_id)
    low = (user.full_name or "").lower()
    look_alike = low and any(
        low == a or (len(low) > 4 and low in a) or (len(low) > 4 and len(a) > 4 and _levenshtein(low, a) <= 2)
        for a in admins
    )
    if look_alike:
        try:
            await context.bot.ban_chat_member(chat_id, user.id)
            await context.bot.send_message(chat_id, "🛡 Removed admin look-alike.")
        except Exception:
            pass
        else:
            await _log(
                context,
                f"\U0001F6E1 Blocked join: {_esc(user.full_name)} ({user.id}) in {chat_id} — admin look-alike.",
            )
        return

    con = _db()
    con.execute(
        "INSERT OR REPLACE INTO joins(chat_id, user_id, joined_ts) VALUES(?,?,?)",
        (chat_id, user.id, int(now_ts)),
    )
    con.commit()
    con.close()

    welcome_text, welcome_on = _welcome_settings(chat_id)
    variants = _welcome_variants(chat_id)
    if variants:
        pool = variants + ([welcome_text] if welcome_text else [])
        chosen_text = random.choice(pool)
    else:
        chosen_text = welcome_text or _default_welcome_for(user.language_code)
    greeting = chosen_text.format(
        first=_esc(user.first_name or user.full_name),
        chatname=_esc(update.effective_chat.title or ""),
    )
    welcome_media_id, welcome_media_type = _welcome_media(chat_id)

    if _captcha_enabled(chat_id):
        try:
            await context.bot.restrict_chat_member(chat_id, user.id, ChatPermissions(can_send_messages=False))
        except Exception as exc:
            log.warning("captcha mute %s", exc)
        if _captcha_mode(chat_id) == "math":
            a, b = random.randint(1, 9), random.randint(1, 9)
            correct = a + b
            options = {correct}
            while len(options) < 4:
                options.add(max(0, correct + random.randint(-6, 6)))
            option_list = list(options)
            random.shuffle(option_list)
            _captcha_answers[(chat_id, user.id)] = correct
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton(str(o), callback_data=f"cap:{chat_id}:{user.id}:{o}") for o in option_list]]
            )
            prompt = (
                f"{greeting}\n\n\U0001F512 Quick check — what's {a} + {b}? Tap the right answer within "
                f"{CAPTCHA_TIMEOUT_SECONDS // 60} min to unlock chat."
            )
        else:
            kb = InlineKeyboardMarkup(
                [[InlineKeyboardButton("✅ I'm not a bot", callback_data=f"cap:{chat_id}:{user.id}")]]
            )
            prompt = f"{greeting}\n\n\U0001F512 Tap below within {CAPTCHA_TIMEOUT_SECONDS // 60} min to unlock chat."
        try:
            if welcome_media_id and welcome_media_type == "photo":
                sent = await context.bot.send_photo(chat_id, welcome_media_id, caption=prompt, reply_markup=kb)
            elif welcome_media_id and welcome_media_type == "animation":
                sent = await context.bot.send_animation(chat_id, welcome_media_id, caption=prompt, reply_markup=kb)
            elif welcome_media_id and welcome_media_type == "video":
                sent = await context.bot.send_video(chat_id, welcome_media_id, caption=prompt, reply_markup=kb)
            else:
                sent = await context.bot.send_message(chat_id, prompt, reply_markup=kb)
            _pending_captcha[(chat_id, user.id)] = sent.message_id
            if context.job_queue:
                context.job_queue.run_once(
                    _captcha_timeout,
                    CAPTCHA_TIMEOUT_SECONDS,
                    data={"chat_id": chat_id, "user_id": user.id, "msg_id": sent.message_id},
                )
        except Exception as exc:
            log.warning("captcha send %s", exc)
        return

    if _rules_gate_enabled(chat_id):
        try:
            await context.bot.restrict_chat_member(chat_id, user.id, ChatPermissions(can_send_messages=False))
        except Exception as exc:
            log.warning("rules gate mute %s", exc)
        rules_text = _rules_text(chat_id) or "Please be respectful and follow common sense."
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("✅ I agree to the rules", callback_data=f"rg:{chat_id}:{user.id}")]]
        )
        prompt = (
            f"{greeting}\n\n\U0001F4DC <b>Rules:</b>\n{_esc(rules_text)}\n\n"
            f"Tap below within {RULES_GATE_TIMEOUT_SECONDS // 60} min to unlock chat."
        )
        try:
            if welcome_media_id and welcome_media_type == "photo":
                sent = await context.bot.send_photo(chat_id, welcome_media_id, caption=prompt, reply_markup=kb, parse_mode="HTML")
            elif welcome_media_id and welcome_media_type == "animation":
                sent = await context.bot.send_animation(chat_id, welcome_media_id, caption=prompt, reply_markup=kb, parse_mode="HTML")
            elif welcome_media_id and welcome_media_type == "video":
                sent = await context.bot.send_video(chat_id, welcome_media_id, caption=prompt, reply_markup=kb, parse_mode="HTML")
            else:
                sent = await context.bot.send_message(chat_id, prompt, reply_markup=kb, parse_mode="HTML")
            _pending_rules[(chat_id, user.id)] = sent.message_id
            if context.job_queue:
                context.job_queue.run_once(
                    _rules_gate_timeout,
                    RULES_GATE_TIMEOUT_SECONDS,
                    data={"chat_id": chat_id, "user_id": user.id, "msg_id": sent.message_id},
                )
        except Exception as exc:
            log.warning("rules gate send %s", exc)
        return

    if now_ts < _raid_lock_until[chat_id]:
        try:
            await context.bot.restrict_chat_member(
                chat_id,
                user.id,
                ChatPermissions(can_send_messages=False),
                until_date=int(_raid_lock_until[chat_id]),
            )
        except Exception as exc:
            log.warning("raid mute %s", exc)
    else:
        newacct_on, newacct_min_id = _newacct_settings(chat_id)
        if newacct_on and user.id >= newacct_min_id and not _is_trusted(user.id):
            try:
                await context.bot.restrict_chat_member(
                    chat_id,
                    user.id,
                    ChatPermissions(can_send_messages=False),
                    until_date=int(now_ts) + NEWACCT_RESTRICT_SECONDS,
                )
            except Exception as exc:
                log.warning("newacct mute %s", exc)
            await _log(
                context,
                f"🕵️ New-looking account {user.id} restricted for {NEWACCT_RESTRICT_SECONDS // 60} min "
                f"in {chat_id} — id above the new-account threshold ({newacct_min_id}).",
                chat_id,
            )
        elif nophoto_burst and not _is_trusted(user.id):
            # Several no-avatar accounts joining at once is a cheap but real raid-bot signal,
            # even below the full raid-burst threshold. Hold them a bit harder than usual.
            try:
                await context.bot.restrict_chat_member(
                    chat_id,
                    user.id,
                    ChatPermissions(can_send_messages=False),
                    until_date=int(now_ts) + NOPHOTO_RESTRICT_SECONDS,
                )
            except Exception as exc:
                log.warning("nophoto mute %s", exc)
            await _log(
                context,
                f"\U0001F464 No-avatar join burst in {chat_id} — {user.id} held for "
                f"{NOPHOTO_RESTRICT_SECONDS // 60} min as a precaution.",
                chat_id,
            )

    if welcome_on:
        btn_label, btn_url = _welcome_btn(chat_id)
        kb = (
            InlineKeyboardMarkup([[InlineKeyboardButton(btn_label, url=btn_url)]])
            if btn_label and btn_url
            else None
        )
        try:
            if welcome_media_id and welcome_media_type == "photo":
                await context.bot.send_photo(chat_id, welcome_media_id, caption=greeting, reply_markup=kb)
            elif welcome_media_id and welcome_media_type == "animation":
                await context.bot.send_animation(chat_id, welcome_media_id, caption=greeting, reply_markup=kb)
            elif welcome_media_id and welcome_media_type == "video":
                await context.bot.send_video(chat_id, welcome_media_id, caption=greeting, reply_markup=kb)
            else:
                await context.bot.send_message(chat_id, greeting, reply_markup=kb)
        except Exception as exc:
            log.warning("welcome %s", exc)


_WORD_RE = re.compile(r"[a-z0-9]+")


def _match_faq(chat_id: int, text_lower: str) -> tuple[int, str] | None:
    """Keyword-overlap-scored FAQ matching — best match at/above FAQ_MIN_SCORE wins."""
    con = _db()
    rows = con.execute("SELECT id, answer, keywords FROM faq WHERE chat_id=?", (chat_id,)).fetchall()
    con.close()
    if not rows:
        return None
    msg_words = set(_WORD_RE.findall(text_lower))
    if not msg_words:
        return None
    best_id, best_answer, best_score = None, None, 0
    for faq_id, answer, keywords in rows:
        kw = {w for w in (keywords or "").lower().split(",") if w.strip()}
        kw = {w.strip() for w in kw}
        score = len(msg_words & kw)
        if score > best_score:
            best_id, best_answer, best_score = faq_id, answer, score
    if best_id is not None and best_score >= FAQ_MIN_SCORE:
        return best_id, best_answer
    return None


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg or update.effective_chat.type == "private":
        return
    user = update.effective_user
    if not user:
        return
    chat_id = update.effective_chat.id
    text_raw = msg.text or msg.caption or ""
    text = text_raw.lower()
    is_edit = update.edited_message is not None

    _touch_chat(chat_id, update.effective_chat.title or "")
    if not is_edit:
        _bump_msg_count(chat_id)
        _maybe_touch_trust(user.id)

    if text_raw.startswith("#") and len(text_raw) > 1:
        note_name = text_raw[1:].strip().lower().split()[0] if text_raw[1:].strip() else ""
        if note_name:
            con = _db()
            row = con.execute(
                "SELECT text, media_id, media_type FROM notes WHERE chat_id=? AND name=?",
                (chat_id, note_name),
            ).fetchone()
            con.close()
            if row:
                note_text, note_media_id, note_media_type = row
                try:
                    if note_media_id and note_media_type == "photo":
                        await context.bot.send_photo(chat_id, note_media_id, caption=note_text or None)
                    elif note_media_id and note_media_type == "animation":
                        await context.bot.send_animation(chat_id, note_media_id, caption=note_text or None)
                    elif note_media_id and note_media_type == "video":
                        await context.bot.send_video(chat_id, note_media_id, caption=note_text or None)
                    else:
                        await msg.reply_text(note_text)
                except Exception as exc:
                    log.warning("note send %s", exc)
                return

    # Bare-word filter match: typing a saved filter's name anywhere in an ordinary message
    # (not a command, not a #-trigger) fires it too — same behavior people know from Rose.
    if not text_raw.startswith("/") and not text_raw.startswith("#") and text_raw.strip():
        con = _db()
        note_rows = con.execute(
            "SELECT name, text, media_id, media_type FROM notes WHERE chat_id=?", (chat_id,)
        ).fetchall()
        con.close()
        for bw_name, bw_text, bw_media_id, bw_media_type in note_rows:
            if not bw_name:
                continue
            if re.search(rf"\b{re.escape(bw_name)}\b", text, re.I):
                try:
                    if bw_media_id and bw_media_type == "photo":
                        await context.bot.send_photo(chat_id, bw_media_id, caption=bw_text or None)
                    elif bw_media_id and bw_media_type == "animation":
                        await context.bot.send_animation(chat_id, bw_media_id, caption=bw_text or None)
                    elif bw_media_id and bw_media_type == "video":
                        await context.bot.send_video(chat_id, bw_media_id, caption=bw_text or None)
                    else:
                        await msg.reply_text(bw_text)
                except Exception as exc:
                    log.warning("bare-word filter trigger %s", exc)
                return

    if msg.sender_chat and msg.sender_chat.id == chat_id:
        # Posted anonymously as the group itself — only admins can do that, never moderate it.
        return
    try:
        member = await context.bot.get_chat_member(chat_id, user.id)
        if member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return
    except Exception:
        return

    if not is_edit and text_raw.startswith("/"):
        trap = _honeypot_cmd(chat_id)
        if trap:
            cmd_word = text_raw[1:].split()[0].split("@")[0].lower() if len(text_raw) > 1 else ""
            if cmd_word and cmd_word == trap.lower():
                try:
                    await msg.delete()
                except Exception as exc:
                    log.warning("honeypot delete %s", exc)
                try:
                    await context.bot.ban_chat_member(chat_id, user.id)
                except Exception as exc:
                    log.warning("honeypot ban %s", exc)
                await _log(
                    context,
                    f"\U0001F36F Honeypot triggered by {_esc(user.full_name)} "
                    f"(<code>{user.id}</code>) — instantly banned.",
                    chat_id,
                )
                return

    if _is_approved(chat_id, user.id):
        return

    if _is_shadowbanned(chat_id, user.id):
        # They still "post" from their own point of view — it just never reaches anyone else.
        try:
            await msg.delete()
        except Exception as exc:
            log.warning("shadowban delete %s", exc)
        return

    if _OCR_AVAILABLE and msg.photo and not is_edit:
        ocr_text = await _ocr_photo(context, msg)
        if ocr_text:
            text_raw = f"{text_raw} {ocr_text}".strip()
            text = text_raw.lower()

    now_ts = time.time()

    if now_ts < _raid_lock_until[chat_id]:
        # Full lockdown during an active raid — not just new joins, everyone non-admin is held.
        try:
            await msg.delete()
        except Exception as exc:
            log.warning("raid lockdown delete %s", exc)
        return

    if not is_edit and _adaptive_slowmode_enabled(chat_id):
        cdq = _chat_msg_times[chat_id]
        cdq.append(now_ts)
        while cdq and now_ts - cdq[0] > ADAPTIVE_SLOWMODE_WINDOW:
            cdq.popleft()
        if len(cdq) >= ADAPTIVE_SLOWMODE_TRIGGER:
            was_active = _adaptive_slowmode_until[chat_id] > now_ts
            _adaptive_slowmode_until[chat_id] = now_ts + ADAPTIVE_SLOWMODE_DURATION
            if not was_active:
                await _log(
                    context,
                    f"⚡ Adaptive slowmode auto-enabled in {chat_id} — message rate spiked "
                    f"({len(cdq)} in {ADAPTIVE_SLOWMODE_WINDOW}s). Cools down automatically.",
                    chat_id,
                )

    slow_s = _slowmode_seconds(chat_id)
    if _adaptive_slowmode_until[chat_id] > now_ts:
        slow_s = max(slow_s, ADAPTIVE_SLOWMODE_SECONDS)
    if _in_quiet_hours(chat_id):
        slow_s = max(slow_s, ADAPTIVE_SLOWMODE_SECONDS)
    if slow_s > 0:
        last = _slowmode_last.get((chat_id, user.id), 0)
        if now_ts - last < slow_s:
            try:
                await msg.delete()
            except Exception as exc:
                log.warning("slowmode delete %s", exc)
            return
        _slowmode_last[(chat_id, user.id)] = now_ts

    dq = _flood[(chat_id, user.id)]
    dq.append(now_ts)
    while dq and now_ts - dq[0] > FLOOD_SECONDS:
        dq.popleft()
    if len(dq) > FLOOD_LIMIT:
        dq.clear()
        try:
            await msg.delete()
        except Exception:
            pass
        mute_secs = _escalated_mute_seconds(chat_id, user.id, FLOOD_MUTE_SECONDS)
        flood_until_ts = int(now_ts) + mute_secs
        try:
            await context.bot.restrict_chat_member(
                chat_id,
                user.id,
                ChatPermissions(can_send_messages=False),
                until_date=flood_until_ts,
            )
        except Exception as exc:
            log.warning("flood mute %s", exc)
        else:
            _track_mute(chat_id, user.id, flood_until_ts)
            await _notify_mute(context, chat_id, user.id, flood_until_ts, "sending messages too fast (flooding)")
        if mute_secs > FLOOD_MUTE_SECONDS:
            await _log(
                context,
                f"⏱ Escalated flood-mute: {_esc(user.full_name)} muted for "
                f"{mute_secs // 60}m (repeat offender).",
                chat_id,
            )
        await _strike(context, chat_id, user.id, "flooding")
        return

    if not is_edit and text_raw:
        dup_reason = _check_duplicate_spam(chat_id, user.id, text_raw, now_ts)
        if dup_reason:
            try:
                await msg.delete()
            except Exception as exc:
                log.warning("dup spam delete %s", exc)
            for cand_val, cand_kind in _extract_scam_candidates(text_raw):
                if _federate_report(cand_val, cand_kind, chat_id):
                    await _log(
                        context,
                        f"🌐 Federated new ecosystem scam entry ({cand_kind}): {_esc(cand_val)} "
                        f"— flagged by {FEDERATION_THRESHOLD}+ groups as coordinated spam.",
                    )
            await _strike(context, chat_id, user.id, f"coordinated spam — {dup_reason}")
            return

    shortlink_expanded = False
    if text_raw and any(_url_host(u) in SHORTENER_DOMAINS for u in URL_RE.findall(text_raw)):
        resolved = await _resolve_shortlinks(text_raw)
        if resolved:
            text_raw = f"{text_raw} {' '.join(resolved)}".strip()
            text = text_raw.lower()
            shortlink_expanded = True

    for val, kind in _scam_list():
        if val and val in text:
            try:
                await msg.delete()
            except Exception as exc:
                log.warning("scamlist delete %s", exc)
            note = " (behind a shortened link)" if shortlink_expanded else ""
            await _strike(context, chat_id, user.id, f"ecosystem scam list ({kind}): {val}{note}")
            return

    for val, kind in _local_scam_list(chat_id):
        if val and val in text:
            try:
                await msg.delete()
            except Exception as exc:
                log.warning("local scamlist delete %s", exc)
            note = " (behind a shortened link)" if shortlink_expanded else ""
            await _strike(context, chat_id, user.id, f"scam list ({kind}): {val}{note}")
            return

    sticker_key = None
    if msg.sticker and msg.sticker.set_name:
        sticker_key = msg.sticker.set_name.lower()
    elif msg.animation and msg.animation.file_unique_id:
        sticker_key = msg.animation.file_unique_id
    if sticker_key and _sticker_blocked(chat_id, sticker_key):
        try:
            await msg.delete()
        except Exception as exc:
            log.warning("sticker blocklist delete %s", exc)
        await _strike(context, chat_id, user.id, f"blocked sticker/GIF pack: {sticker_key}")
        return

    link_text = _strip_whitelisted_links(chat_id, text_raw)

    if link_text and _linkscan_enabled(chat_id):
        for u in URL_RE.findall(link_text):
            domain = _url_host(u)
            if domain and _linkscan_check(domain):
                try:
                    await msg.delete()
                except Exception as exc:
                    log.warning("linkscan delete %s", exc)
                await _strike(context, chat_id, user.id, f"link safety scan flagged: {domain}")
                return

    locks = _locks(chat_id)
    if locks["links"] and LINK_RE.search(link_text):
        try:
            await msg.delete()
        except Exception as exc:
            log.warning("lock link delete %s", exc)
        await _strike(context, chat_id, user.id, "locked: links")
        return
    if locks["forwards"] and getattr(msg, "forward_origin", None):
        try:
            await msg.delete()
        except Exception as exc:
            log.warning("lock forward delete %s", exc)
        await _strike(context, chat_id, user.id, "locked: forwards")
        return
    if locks["stickers"] and msg.sticker:
        try:
            await msg.delete()
        except Exception as exc:
            log.warning("lock sticker delete %s", exc)
        await _strike(context, chat_id, user.id, "locked: stickers")
        return
    if locks["photos"] and msg.photo:
        try:
            await msg.delete()
        except Exception as exc:
            log.warning("lock photo delete %s", exc)
        await _strike(context, chat_id, user.id, "locked: photos")
        return
    if locks["voice"] and msg.voice:
        try:
            await msg.delete()
        except Exception as exc:
            log.warning("lock voice delete %s", exc)
        await _strike(context, chat_id, user.id, "locked: voice notes")
        return
    if locks["videonote"] and msg.video_note:
        try:
            await msg.delete()
        except Exception as exc:
            log.warning("lock video note delete %s", exc)
        await _strike(context, chat_id, user.id, "locked: video notes")
        return

    if LINK_RE.search(link_text) and _antilink_enabled(chat_id):
        con = _db()
        jrow = con.execute(
            "SELECT joined_ts FROM joins WHERE chat_id=? AND user_id=?", (chat_id, user.id)
        ).fetchone()
        con.close()
        if jrow and now_ts - jrow[0] < NEW_MEMBER_LOCK_SECONDS:
            try:
                await msg.delete()
            except Exception as exc:
                log.warning("link lock delete %s", exc)
            await _strike(context, chat_id, user.id, "link within new-member lock window")
            return

    con = _db()
    extra = con.execute("SELECT word, action, is_regex FROM filters WHERE chat_id=?", (chat_id,)).fetchall()
    con.close()
    hit_action, hit_word = None, None
    for w in DEFAULT_WORDS:
        if w and w in text:
            hit_action, hit_word = "mute", w
            break
    if not hit_action:
        for w, act, is_regex in extra:
            if not w:
                continue
            if is_regex:
                try:
                    matched = re.search(w, text_raw, re.I) is not None
                except re.error:
                    matched = False
            else:
                matched = w in text
            if matched:
                hit_action, hit_word = (act or "mute"), w
                break
    if not hit_action and not _is_trusted(user.id):
        # Fuzzy pass — catches l33t/spacing evasions ("fr33 m1nt", "s e e d  p h r a s e") that
        # dodge exact substring matching. Only runs against short phrases, skipping regex filters.
        # Skipped for users with a 30+ day clean record across Ferzan groups — fewer false positives
        # for known-good veterans.
        for w in DEFAULT_WORDS:
            if w and _fuzzy_hit(text, w):
                hit_action, hit_word = "mute", w
                break
        if not hit_action:
            for w, act, is_regex in extra:
                if is_regex or not w:
                    continue
                if _fuzzy_hit(text, w):
                    hit_action, hit_word = (act or "mute"), w
                    break
    if hit_action:
        try:
            await msg.delete()
        except Exception as exc:
            log.warning("filter delete %s", exc)
        if hit_action == "ban":
            try:
                await context.bot.ban_chat_member(chat_id, user.id)
            except Exception as exc:
                log.warning("filter ban %s", exc)
            con = _db()
            con.execute(
                "INSERT OR REPLACE INTO global_bans(user_id, reason, by_id, ts) VALUES(?,?,?,strftime('%s','now'))",
                (user.id, f"filter: {hit_word}", 0),
            )
            con.commit()
            con.close()
            await _log(
                context,
                f"\U0001F528 Instant-banned {user.id} in {chat_id} — filter '{_esc(hit_word)}'.",
                chat_id,
                _qa_unban_kb(chat_id, user.id),
            )
        elif hit_action == "mute":
            try:
                await context.bot.restrict_chat_member(chat_id, user.id, ChatPermissions(can_send_messages=False))
            except Exception as exc:
                log.warning("filter mute %s", exc)
            else:
                _track_mute(chat_id, user.id, int(now_ts) + 3650 * 86400)  # indefinite, sentinel far-future
                await _notify_mute(context, chat_id, user.id, None, f"blocked phrase: {hit_word}")
            await _strike(context, chat_id, user.id, f"blocked phrase: {hit_word}")
        else:
            await _strike(context, chat_id, user.id, f"blocked phrase (warn): {hit_word}")
        return

    con = _db()
    triggers = con.execute("SELECT trig, reply FROM autoreply WHERE chat_id=?", (chat_id,)).fetchall()
    con.close()
    autoreplied = False
    for trig, rep in triggers:
        if trig and trig in text:
            try:
                await msg.reply_text(rep)
            except Exception as exc:
                log.warning("autoreply %s", exc)
            autoreplied = True
            break

    if not autoreplied and not is_edit and text_raw and "?" in text_raw:
        faq_hit = _match_faq(chat_id, text)
        if faq_hit:
            faq_id, answer = faq_hit
            if _check_cooldown(user.id, chat_id, f"faq:{faq_id}", FAQ_COOLDOWN_SECONDS) == 0:
                try:
                    await msg.reply_text(answer)
                except Exception as exc:
                    log.warning("faq reply %s", exc)


async def del_pin_notice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-delete Telegram's 'X pinned a message' service notices to keep channels clean."""
    try:
        await update.effective_message.delete()
    except Exception as exc:
        log.warning("del pin notice %s", exc)


async def clean_service_notice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Auto-delete Telegram's native 'X joined/left the group' service notices, if enabled."""
    if not _cleanservice_enabled(update.effective_chat.id):
        return
    try:
        await update.effective_message.delete()
    except Exception as exc:
        log.warning("clean service %s", exc)


# ---- Watchdog: alert the owner on crashes, and an optional "still alive" heartbeat ----

_last_error_alert = 0.0
ERROR_ALERT_COOLDOWN = 300


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    global _last_error_alert
    log.error("Unhandled exception", exc_info=context.error)
    now = time.time()
    if now - _last_error_alert < ERROR_ALERT_COOLDOWN:
        return
    _last_error_alert = now
    for owner_id in OWNER_IDS:
        try:
            await context.bot.send_message(
                owner_id,
                f"🛑 Guardian hit an unhandled error:\n<code>{_esc(str(context.error))[:500]}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def _heartbeat(context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    n = con.execute("SELECT COUNT(*) FROM known_chats").fetchone()[0]
    con.close()
    for owner_id in OWNER_IDS:
        try:
            await context.bot.send_message(owner_id, f"✅ Guardian heartbeat — alive, watching {n} group(s).")
        except Exception:
            pass


async def _db_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not DB.exists():
        return
    try:
        data = DB.read_bytes()
    except Exception as exc:
        log.warning("db backup read %s", exc)
        return
    fname = f"guardian_backup_{time.strftime('%Y%m%d_%H%M')}.db"
    for owner_id in OWNER_IDS:
        try:
            await context.bot.send_document(
                owner_id, BytesIO(data), filename=fname, caption="🗄 Guardian DB backup"
            )
        except Exception as exc:
            log.warning("db backup send %s", exc)


async def _weekly_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    con = _db()
    chats = con.execute("SELECT chat_id, title FROM known_chats").fetchall()
    now_ts = int(time.time())
    week_ago = now_ts - 7 * 86400
    days = [time.strftime("%Y-%m-%d", time.gmtime(now_ts - i * 86400)) for i in range(7)]
    placeholders = ",".join("?" * len(days))
    for chat_id, title in chats:
        msgs = con.execute(
            f"SELECT COALESCE(SUM(count),0) FROM msg_counts WHERE chat_id=? AND day IN ({placeholders})",
            (chat_id, *days),
        ).fetchone()[0]
        joins = con.execute(
            "SELECT COUNT(*) FROM joins WHERE chat_id=? AND joined_ts>=?", (chat_id, week_ago)
        ).fetchone()[0]
        struck = con.execute(
            "SELECT COUNT(*) FROM warns WHERE chat_id=? AND last_ts>=?", (chat_id, week_ago)
        ).fetchone()[0]
        text = (
            f"📈 <b>Weekly digest — {_esc(title or chat_id)}</b>\n\n"
            f"Messages: {msgs}\n"
            f"New joins: {joins}\n"
            f"Users struck: {struck}\n"
        )
        await _log(context, text, chat_id)
    con.close()


def _fetch_phishing_feed_sync() -> list:
    req = urllib.request.Request(PHISHING_FEED_URL, headers={"User-Agent": "FerzanGuardian/1.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    data = json.loads(raw.decode("utf-8"))
    if isinstance(data, dict):
        return list(data.get("blacklist") or [])
    if isinstance(data, list):
        return data
    return []


async def _sync_phishing_feed(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not PHISHING_FEED_URL:
        return
    try:
        loop = asyncio.get_event_loop()
        domains = await loop.run_in_executor(None, _fetch_phishing_feed_sync)
    except Exception as exc:
        log.warning("phishing feed sync %s", exc)
        return
    if not domains:
        return
    con = _db()
    added = 0
    for d in domains:
        d = str(d).strip().lower()
        if not d:
            continue
        cur = con.execute("SELECT 1 FROM scam_list WHERE value=?", (d,)).fetchone()
        if not cur:
            con.execute(
                "INSERT OR IGNORE INTO scam_list(value, kind, added_by, ts) VALUES(?,?,0,strftime('%s','now'))",
                (d, "domain"),
            )
            added += 1
    con.commit()
    con.close()
    if added:
        _scam_cache["ts"] = 0
        log.info("phishing feed sync: %d new domain(s)", added)
        await _log(context, f"🌐 Synced phishing feed — {added} new domain(s) added to the ecosystem scam list.")


def _build_owner_digest_text() -> str:
    con = _db()
    chats = con.execute("SELECT chat_id, title FROM known_chats ORDER BY title").fetchall()
    con.close()
    if not chats:
        return "🛡 <b>Ecosystem digest</b>\n\nNo known groups yet."
    lines = ["🛡 <b>Ecosystem digest</b> — group health across all Guardian chats\n"]
    scored = []
    for cid, title in chats:
        h = _compute_health(cid)
        scored.append((h["score"], title or str(cid), h))
    scored.sort(key=lambda t: t[0])
    for score, title, h in scored:
        lines.append(
            f"{h['label']} <b>{_esc(title)}</b> — {score}/100 "
            f"(mod: {h['mod_actions_7d']}/7d, strikes: {h['strikes_total']}, open reports: {h['reports_open']})"
        )
    return "\n".join(lines)


async def _owner_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    if OWNER_DIGEST_HOURS <= 0:
        return
    text = _build_owner_digest_text()
    for owner_id in OWNER_IDS:
        try:
            await context.bot.send_message(owner_id, text, parse_mode="HTML")
        except Exception as exc:
            log.warning("owner digest %s %s", owner_id, exc)


async def gdigest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual trigger for the ecosystem-wide owner digest — owner-only, works regardless of
    whether the scheduled GUARDIAN_OWNER_DIGEST_HOURS job is enabled."""
    if update.effective_user.id not in OWNER_IDS:
        return
    text = _build_owner_digest_text()
    await update.effective_message.reply_text(text, parse_mode="HTML")


def main() -> None:
    token = (os.getenv("GUARDIAN_TOKEN") or os.getenv("FERZAN_GUARDIAN_TOKEN") or "").strip()
    if not token:
        raise SystemExit("Set GUARDIAN_TOKEN in /opt/ferzan/.env")
    _db()
    app = Application.builder().token(token).build()
    app.add_handler(_TypeHandler(Update, _debug_log_all), group=-1000)
    app.add_handler(MessageHandler(filters.Regex(r"^/(filter|save)(@\w+)?(\s|$)"), _filter_cmd_first), group=-100)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("gmenu", gmenu))
    app.add_handler(CommandHandler("menu", gmenu))
    app.add_handler(CommandHandler("gfilter", gfilter))
    app.add_handler(CommandHandler("gunfilter", gunfilter))
    app.add_handler(CommandHandler("gfilters", gfilters))
    app.add_handler(CommandHandler("gban", gban))
    app.add_handler(CommandHandler("gunban", gunban))
    app.add_handler(CommandHandler("glink", glink))
    app.add_handler(CommandHandler("gwarn", gwarn))
    app.add_handler(CommandHandler("gwarns", gwarns))
    app.add_handler(CommandHandler("mystats", mystats_cmd))
    app.add_handler(CommandHandler("gunwarn", gunwarn))
    app.add_handler(CommandHandler("gsetwarns", gsetwarns))
    app.add_handler(CommandHandler("gsetdecay", gsetdecay))
    app.add_handler(CommandHandler("graidoff", graidoff))
    app.add_handler(CommandHandler("grevokeinvite", grevokeinvite))
    app.add_handler(CommandHandler("glockdown", glockdown))
    app.add_handler(CommandHandler("gsetperm", gsetperm_cmd))
    app.add_handler(CommandHandler("gmute", gmute))
    app.add_handler(CommandHandler("gunmute", gunmute))
    app.add_handler(CommandHandler("gkick", gkick))
    app.add_handler(CommandHandler("gunlockadmin", gunlockadmin))
    app.add_handler(CommandHandler("gapprove", gapprove))
    app.add_handler(CommandHandler("gunapprove", gunapprove))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("setwelcome", setwelcome))
    app.add_handler(CommandHandler("testwelcome", testwelcome_cmd))
    app.add_handler(CommandHandler("addwelcome", addwelcome_cmd))
    app.add_handler(CommandHandler("welcomevariants", welcomevariants_cmd))
    app.add_handler(CommandHandler("delwelcome", delwelcome_cmd))
    app.add_handler(CommandHandler("welcome", welcome_toggle))
    app.add_handler(CommandHandler("setwelcomebtn", setwelcomebtn))
    app.add_handler(CommandHandler("delwelcomebtn", delwelcomebtn))
    app.add_handler(CommandHandler("setgoodbye", setgoodbye))
    app.add_handler(CommandHandler("goodbye", goodbye_toggle))
    app.add_handler(CommandHandler("cleanservice", cleanservice_toggle))
    app.add_handler(CommandHandler("setlogchat", setlogchat))
    app.add_handler(CommandHandler("slowmode", slowmode_cmd))
    app.add_handler(CommandHandler("gadaptiveslowmode", gadaptiveslowmode_cmd))
    app.add_handler(CommandHandler("gquiethours", gquiethours_cmd))
    app.add_handler(CommandHandler("gcaptcha", gcaptcha))
    app.add_handler(CommandHandler("grulesgate", grulesgate_cmd))
    app.add_handler(CommandHandler("rules", rules_cmd))
    app.add_handler(CommandHandler("setrules", setrules))
    app.add_handler(CommandHandler("del", del_cmd))
    app.add_handler(CommandHandler("purge", purge_cmd))
    app.add_handler(CommandHandler("lock", lock_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))
    app.add_handler(CommandHandler("info", info_cmd))
    app.add_handler(CommandHandler("admins", admins_cmd))
    app.add_handler(CommandHandler("pin", pin_cmd))
    app.add_handler(CommandHandler("unpin", unpin_cmd))
    app.add_handler(CommandHandler("save", save_note))
    app.add_handler(CommandHandler("filter", save_note))
    app.add_handler(CommandHandler("notes", notes_cmd))
    app.add_handler(CommandHandler("filters", notes_cmd))
    app.add_handler(CommandHandler("delnote", delnote_cmd))
    app.add_handler(CommandHandler("delfilter", delnote_cmd))
    app.add_handler(CommandHandler("setreply", setreply_cmd))
    app.add_handler(CommandHandler("delreply", delreply_cmd))
    app.add_handler(CommandHandler("replies", replies_cmd))
    app.add_handler(CommandHandler("gscamadd", gscamadd))
    app.add_handler(CommandHandler("gscamdel", gscamdel))
    app.add_handler(CommandHandler("gscamlist", gscamlist))
    app.add_handler(CommandHandler("scamadd", scamadd))
    app.add_handler(CommandHandler("scamdel", scamdel))
    app.add_handler(CommandHandler("scamlist", scamlist_cmd))
    app.add_handler(CommandHandler("gstats", gstats_cmd))
    app.add_handler(CommandHandler("ghealth", ghealth_cmd))
    app.add_handler(CommandHandler("gbroadcast", gbroadcast))
    app.add_handler(CommandHandler("gsettings", gsettings_cmd))
    app.add_handler(CommandHandler("linkwhitelist", linkwhitelist_cmd))
    app.add_handler(CommandHandler("exportconfig", exportconfig_cmd))
    app.add_handler(CommandHandler("importconfig", importconfig_cmd))
    app.add_handler(CommandHandler("cloneconfig", cloneconfig_cmd))
    app.add_handler(CommandHandler("gmodadd", gmodadd))
    app.add_handler(CommandHandler("gmodremove", gmodremove))
    app.add_handler(CommandHandler("gmods", gmods_cmd))
    app.add_handler(CommandHandler("gnewacct", gnewacct))
    app.add_handler(CommandHandler("gnewacctid", gnewacct_threshold))
    app.add_handler(CommandHandler("gshadowban", gshadowban))
    app.add_handler(CommandHandler("gunshadowban", gunshadowban))
    app.add_handler(CallbackQueryHandler(captcha_button, pattern=r"^cap:"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^gm:"))
    app.add_handler(CallbackQueryHandler(config_callback, pattern=r"^cfg:"))
    app.add_handler(CommandHandler("giveaway", giveaway_cmd))
    app.add_handler(CommandHandler("gendgiveaway", gendgiveaway))
    app.add_handler(CommandHandler("gvotemute", gvotemute_toggle))
    app.add_handler(CommandHandler("gvotemutethreshold", gvotemute_threshold))
    app.add_handler(CommandHandler("votemute", votemute_cmd))
    app.add_handler(CommandHandler("tr", tr_cmd))
    app.add_handler(CommandHandler("gsettimezone", gsettimezone))
    app.add_handler(CommandHandler("schedule", schedule_cmd))
    app.add_handler(CommandHandler("schedulelist", schedulelist_cmd))
    app.add_handler(CommandHandler("scheduledel", scheduledel_cmd))
    app.add_handler(CommandHandler("reports", reports_cmd))
    app.add_handler(CommandHandler("resolve", resolve_cmd))
    app.add_handler(CommandHandler("faqadd", faqadd_cmd))
    app.add_handler(CommandHandler("faqlist", faqlist_cmd))
    app.add_handler(CommandHandler("faqdel", faqdel_cmd))
    app.add_handler(CommandHandler("sethoneypot", sethoneypot_cmd))
    app.add_handler(CommandHandler("deletehoneypot", deletehoneypot_cmd))
    app.add_handler(CommandHandler("gclearmutes", gclearmutes))
    app.add_handler(CommandHandler("gclearbans", gclearbans))
    app.add_handler(CommandHandler("gmodstats", gmodstats))
    app.add_handler(CommandHandler("auditlog", auditlog_cmd))
    app.add_handler(CommandHandler("configauditlog", configauditlog_cmd))
    app.add_handler(CommandHandler("exportauditlog", exportauditlog_cmd))
    app.add_handler(CommandHandler("exportmembers", exportmembers_cmd))
    app.add_handler(CommandHandler("gdigest", gdigest_cmd))
    app.add_handler(CommandHandler("glinkscan", glinkscan_cmd))
    app.add_handler(CommandHandler("gblocksticker", gblocksticker))
    app.add_handler(CommandHandler("gunblocksticker", gunblocksticker))
    app.add_handler(CommandHandler("gstickerblocklist", gstickerblocklist))
    app.add_handler(CallbackQueryHandler(quickaction_callback, pattern=r"^qa:"))
    app.add_handler(CallbackQueryHandler(giveaway_enter, pattern=r"^gw:"))
    app.add_handler(CallbackQueryHandler(votemute_vote, pattern=r"^vm:"))
    app.add_handler(CallbackQueryHandler(report_resolve_callback, pattern=r"^rp:"))
    app.add_handler(CallbackQueryHandler(rules_gate_button, pattern=r"^rg:"))
    app.add_handler(CallbackQueryHandler(appeal_button, pattern=r"^ap:"))
    app.add_handler(InlineQueryHandler(inline_lookup))
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.ANIMATION | filters.VIDEO, _welcome_media_upload),
        group=-1,
    )
    app.add_handler(
        MessageHandler(filters.PHOTO | filters.ANIMATION | filters.VIDEO, _note_media_upload),
        group=-2,
    )
    app.add_handler(MessageHandler(filters.COMMAND, _slash_filter_trigger), group=10)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _pending_text_capture),
        group=-3,
    )
    app.add_handler(ChatMemberHandler(on_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(
        MessageHandler(
            (
                filters.TEXT
                | filters.CAPTION
                | filters.Sticker.ALL
                | filters.ANIMATION
                | filters.PHOTO
                | filters.FORWARDED
                | filters.VOICE
                | filters.VIDEO_NOTE
            )
            & (filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE),
            on_text,
        )
    )
    app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, del_pin_notice))
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.NEW_CHAT_MEMBERS | filters.StatusUpdate.LEFT_CHAT_MEMBER,
            clean_service_notice,
        )
    )

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
            BotCommand("glink", "Toggle new-member link lock"),
            BotCommand("gwarn", "Add a strike to a user"),
            BotCommand("gwarns", "Check a user's strikes"),
            BotCommand("mystats", "Check your own strikes and mute status"),
            BotCommand("gunwarn", "Remove one strike from a user"),
            BotCommand("gsetwarns", "Set the strike limit for auto-ban"),
            BotCommand("gsetdecay", "Set how many days strikes take to expire"),
            BotCommand("graidoff", "Clear an active raid lock"),
            BotCommand("grevokeinvite", "Revoke an invite link (auto-detects raid link)"),
            BotCommand("gsetperm", "Set which tier (mod/admin) runs a command"),
            BotCommand("gmute", "Mute a user for a set duration"),
            BotCommand("gunmute", "Lift a mute"),
            BotCommand("gkick", "Remove a user (not a ban)"),
            BotCommand("gunlockadmin", "Restore an admin's restrict/ban rights"),
            BotCommand("report", "Report a message to admins"),
            BotCommand("setwelcome", "Set the welcome message"),
            BotCommand("addwelcome", "Add a rotating welcome-message variant"),
            BotCommand("welcomevariants", "List welcome-message variants"),
            BotCommand("delwelcome", "Delete a welcome-message variant"),
            BotCommand("welcome", "Toggle welcome messages"),
            BotCommand("setwelcomebtn", "Set a button on the welcome message"),
            BotCommand("delwelcomebtn", "Remove the welcome button"),
            BotCommand("setgoodbye", "Set the goodbye message"),
            BotCommand("goodbye", "Toggle goodbye messages"),
            BotCommand("cleanservice", "Toggle auto-delete of join/left notices"),
            BotCommand("setlogchat", "Set where mod alerts get posted"),
            BotCommand("slowmode", "Limit how often each member can post"),
            BotCommand("gadaptiveslowmode", "Auto-slowmode when the chat spikes"),
            BotCommand("gquiethours", "Set a recurring low-traffic window"),
            BotCommand("gcaptcha", "Toggle join captcha"),
            BotCommand("grulesgate", "Require agreeing to rules before posting"),
            BotCommand("rules", "Show group rules"),
            BotCommand("setrules", "Set group rules"),
            BotCommand("del", "Delete a replied message"),
            BotCommand("purge", "Bulk-delete up to a replied message"),
            BotCommand("lock", "Lock a content type"),
            BotCommand("unlock", "Unlock a content type"),
            BotCommand("info", "Look up a user"),
            BotCommand("admins", "List group admins"),
            BotCommand("pin", "Pin a replied message"),
            BotCommand("unpin", "Unpin a message"),
            BotCommand("gapprove", "Exempt a user from auto-mod"),
            BotCommand("gunapprove", "Remove a user's exemption"),
            BotCommand("save", "Save a note (#name to trigger)"),
            BotCommand("filter", "Save a filter/note (#name to trigger)"),
            BotCommand("notes", "List saved notes"),
            BotCommand("delnote", "Delete a saved note"),
            BotCommand("setreply", "Set a trigger word auto-reply"),
            BotCommand("delreply", "Delete an auto-reply"),
            BotCommand("replies", "List auto-reply triggers"),
            BotCommand("scamadd", "Block a CA/domain in this group"),
            BotCommand("scamdel", "Unblock a CA/domain in this group"),
            BotCommand("scamlist", "List this group's blocked CAs/domains"),
            BotCommand("gstats", "Group activity stats"),
            BotCommand("ghealth", "Composite group health score"),
            BotCommand("gsettings", "Live settings panel (buttons)"),
            BotCommand("gmodadd", "Add a Guardian-only mod"),
            BotCommand("gmodremove", "Remove a Guardian-only mod"),
            BotCommand("gmods", "List this group's Guardian mods"),
            BotCommand("gnewacct", "Toggle scrutiny of very-new accounts on join"),
            BotCommand("gnewacctid", "Set the new-account id threshold"),
            BotCommand("gshadowban", "Silently drop a user's messages"),
            BotCommand("gunshadowban", "Lift a shadowban"),
            BotCommand("glockdown", "Manually trigger a full-chat lockdown"),
            BotCommand("giveaway", "Start a giveaway — /giveaway 1h Prize"),
            BotCommand("gendgiveaway", "End a giveaway early"),
            BotCommand("gvotemute", "Toggle member vote-to-mute"),
            BotCommand("gvotemutethreshold", "Set votes needed to pass"),
            BotCommand("votemute", "Start a vote to mute someone (reply)"),
            BotCommand("tr", "Translate a replied message"),
            BotCommand("schedule", "Schedule a recurring announcement"),
            BotCommand("reports", "List open reports (mods)"),
            BotCommand("resolve", "Resolve a report by id"),
            BotCommand("faqadd", "Add a smart FAQ auto-answer"),
            BotCommand("faqlist", "List this group's FAQs"),
            BotCommand("gclearmutes", "Lift every Guardian-issued mute here"),
            BotCommand("gclearbans", "Lift every Guardian-issued ban here"),
            BotCommand("gmodstats", "Mod action leaderboard (7 days)"),
            BotCommand("auditlog", "View mod action history"),
            BotCommand("exportmembers", "Export recorded joins for this group (CSV)"),
            BotCommand("gdigest", "Owner: ecosystem-wide health digest now"),
            BotCommand("glinkscan", "Toggle live link safety scanning"),
            BotCommand("gblocksticker", "Block a sticker pack or GIF (reply)"),
            BotCommand("gunblocksticker", "Unblock a sticker pack or GIF"),
        ]
        # Telegram hard-caps set_my_commands at 100 entries and rejects the WHOLE call over
        # the limit — which previously crashed the entire bot on every startup (Bot_commands_too_much)
        # since nothing caught it. Never let a menu-list problem take the bot down again.
        try:
            if len(cmds) > 100:
                log.warning("guardian cmds list has %d entries (>100) — trimming for set_my_commands", len(cmds))
                cmds = cmds[:100]
            await application.bot.set_my_commands(cmds)
        except Exception as exc:
            log.error("set_my_commands failed, continuing without updating the menu: %s", exc)

    app.post_init = _post
    app.add_error_handler(_error_handler)
    if app.job_queue and HEARTBEAT_HOURS > 0:
        app.job_queue.run_repeating(_heartbeat, interval=HEARTBEAT_HOURS * 3600, first=300)
    if app.job_queue and DB_BACKUP_HOURS > 0:
        app.job_queue.run_repeating(_db_backup, interval=DB_BACKUP_HOURS * 3600, first=600)
    if app.job_queue and WEEKLY_DIGEST_ENABLED:
        app.job_queue.run_daily(_weekly_digest, time=dtime(hour=13, minute=0), days=(0,))
    if app.job_queue and PHISHING_SYNC_HOURS > 0:
        app.job_queue.run_repeating(_sync_phishing_feed, interval=PHISHING_SYNC_HOURS * 3600, first=120)
    if app.job_queue and OWNER_DIGEST_HOURS > 0:
        app.job_queue.run_repeating(_owner_digest, interval=OWNER_DIGEST_HOURS * 3600, first=180)
    _reschedule_giveaways(app)
    _reload_scheduled_posts(app)

    if WEBHOOK_URL:
        log.info("Ferzan Guardian running (webhook mode, %s)", WEBHOOK_URL)
        app.run_webhook(
            listen="127.0.0.1",
            port=WEBHOOK_PORT,
            url_path=WEBHOOK_PATH.lstrip("/"),
            webhook_url=WEBHOOK_URL.rstrip("/") + "/" + WEBHOOK_PATH.lstrip("/"),
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        log.info("Ferzan Guardian running (polling mode)")
        app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
