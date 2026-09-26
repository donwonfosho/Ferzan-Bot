"""
launch_extras.py -- helpers for the Launch Bot:
  * per-user time zone
  * "8pm" / "tomorrow 9:30am" / "in 2h" / "9/27 8pm" time parsing
  * scheduled launch drafts (stored in the launch DB)
  * token logo storage (served by api.py at /api/media/<file>)
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta, date
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import launch_bot_db as db

MEDIA_DIR = Path(__file__).resolve().with_name("media")

TZ_CHOICES = [
    ("Eastern (New York)", "America/New_York"),
    ("Central (Chicago)", "America/Chicago"),
    ("Mountain (Denver)", "America/Denver"),
    ("Pacific (Los Angeles)", "America/Los_Angeles"),
    ("UTC", "UTC"),
    ("London", "Europe/London"),
    ("Central Europe", "Europe/Berlin"),
    ("Dubai", "Asia/Dubai"),
    ("Singapore", "Asia/Singapore"),
]

_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


# --------------------------------------------------------------- storage --
def init_tables() -> None:
    with db._get_conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS launch_prefs (user_id INTEGER PRIMARY KEY, tz TEXT NOT NULL)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS launch_drafts (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                payload TEXT NOT NULL,
                run_at INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_drafts_due ON launch_drafts (status, run_at)")


def get_tz(user_id: int) -> str | None:
    with db._get_conn() as conn:
        row = conn.execute("SELECT tz FROM launch_prefs WHERE user_id = ?", (int(user_id),)).fetchone()
    return row[0] if row else None


def set_tz(user_id: int, tz: str) -> None:
    ZoneInfo(tz)  # raises if unknown
    with db._get_conn() as conn:
        conn.execute(
            "INSERT INTO launch_prefs (user_id, tz) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET tz = excluded.tz",
            (int(user_id), tz),
        )


def valid_tz(name: str) -> str | None:
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z_]+(/[A-Za-z_\-+0-9]+){0,2}", name):
        return None
    try:
        ZoneInfo(name)
        return name
    except (ZoneInfoNotFoundError, ValueError):
        return None


def save_draft(user_id: int, chat_id: int, payload: dict, run_at: int) -> str:
    did = uuid.uuid4().hex[:10]
    with db._get_conn() as conn:
        conn.execute(
            "INSERT INTO launch_drafts (id, user_id, chat_id, payload, run_at, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'scheduled', ?)",
            (did, int(user_id), int(chat_id), json.dumps(payload), int(run_at), int(time.time())),
        )
    return did


def get_draft(did: str) -> dict | None:
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT id, user_id, chat_id, payload, run_at, status FROM launch_drafts WHERE id = ?", (did,)
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "user_id": row[1], "chat_id": row[2], "payload": json.loads(row[3]),
            "run_at": row[4], "status": row[5]}


def list_drafts(user_id: int) -> list[dict]:
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM launch_drafts WHERE user_id = ? AND status IN ('scheduled','reminded') "
            "ORDER BY run_at", (int(user_id),),
        ).fetchall()
    return [d for d in (get_draft(r[0]) for r in rows) if d]


def set_draft_status(did: str, status: str) -> None:
    with db._get_conn() as conn:
        conn.execute("UPDATE launch_drafts SET status = ? WHERE id = ?", (status, did))


def reschedule_draft(did: str, run_at: int) -> None:
    with db._get_conn() as conn:
        conn.execute("UPDATE launch_drafts SET run_at = ?, status = 'scheduled' WHERE id = ?", (int(run_at), did))


def claim_due_drafts(now: int | None = None) -> list[dict]:
    """Drafts whose time has come; marks them 'reminded' so each reminder is sent once."""
    now = int(now or time.time())
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM launch_drafts WHERE status = 'scheduled' AND run_at <= ?", (now,)
        ).fetchall()
        ids = [r[0] for r in rows]
        for did in ids:
            conn.execute("UPDATE launch_drafts SET status = 'reminded' WHERE id = ? AND status = 'scheduled'", (did,))
    return [d for d in (get_draft(i) for i in ids) if d]


# ---------------------------------------------------------------- times --
def _parse_clock(s: str) -> tuple[int, int] | None:
    s = s.strip()
    if s in {"noon", "12noon"}:
        return 12, 0
    if s == "midnight":
        return 0, 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm|a|p)?", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if mi > 59:
        return None
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap.startswith("p") else 0)
    elif h > 23:
        return None
    return h, mi


def parse_when(text: str, tz_name: str, now: datetime | None = None) -> tuple[datetime | None, str]:
    """-> (aware datetime, "") or (None, error message)."""
    tz = ZoneInfo(tz_name)
    now = (now or datetime.now(tz)).astimezone(tz)
    s = re.sub(r"\s+", " ", (text or "").strip().lower().replace(",", " ").replace(" at ", " "))
    s = re.sub(r"^(at|on) ", "", s)
    if not s:
        return None, "Type a time like 8pm, tomorrow 9am, or in 2h."
    m = re.fullmatch(r"(?:in )?(\d+(?:\.\d+)?) ?(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)", s)
    if m:
        n = float(m.group(1))
        unit = m.group(2)[0]
        delta = timedelta(minutes=n) if unit == "m" else timedelta(hours=n) if unit == "h" else timedelta(days=n)
        return now + delta, ""

    day: date | None = None
    rest = s
    if rest.startswith("today"):
        day, rest = now.date(), rest[5:]
    elif rest.startswith("tonight"):
        day, rest = now.date(), rest[7:]
    elif rest.startswith(("tomorrow", "tmrw", "tmr")):
        day, rest = now.date() + timedelta(days=1), re.sub(r"^(tomorrow|tmrw|tmr)", "", rest)
    else:
        wd = re.match(r"(mon|tue|wed|thu|fri|sat|sun)[a-z]*\b", rest)
        dm = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})\b", rest) or re.match(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", rest)
        if wd:
            target = _WEEKDAYS.index(wd.group(1))
            ahead = (target - now.weekday()) % 7
            day, rest = now.date() + timedelta(days=ahead), rest[wd.end():]
        elif dm:
            try:
                if "-" in dm.group(0):
                    day = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)))
                else:
                    y = dm.group(3)
                    year = now.year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
                    day = date(year, int(dm.group(1)), int(dm.group(2)))
                    if not y and day < now.date():
                        day = date(year + 1, day.month, day.day)
            except ValueError:
                return None, "That date doesn't exist - try 9/27 8pm."
            rest = rest[dm.end():]
    clock = _parse_clock(rest.strip()) if rest.strip() else None
    if rest.strip() and clock is None:
        return None, "I couldn't read that time. Try 8pm, 20:00, tomorrow 9:30am, sat 8pm, 9/27 8pm, or in 2h."
    if clock is None:
        return None, "Add a time too, like tomorrow 8pm."
    base = day or now.date()
    when = datetime(base.year, base.month, base.day, clock[0], clock[1], tzinfo=tz)
    if when <= now:
        if day is None:
            when += timedelta(days=1)  # "8pm" after 8pm means tomorrow
        elif re.match(r"(mon|tue|wed|thu|fri|sat|sun)", s):
            when += timedelta(days=7)  # "fri 8pm" on a Friday evening means next Friday
    if when <= now:
        return None, "That time has already passed - pick a time in the future."
    return when, ""


def fmt_when(ts: int, tz_name: str | None) -> str:
    tz = ZoneInfo(tz_name or "UTC")
    dt = datetime.fromtimestamp(int(ts), tz)
    left = int(ts - time.time())
    rel = ""
    if left > 0:
        d, r = divmod(left, 86400)
        h, r = divmod(r, 3600)
        mi = r // 60
        parts = ([f"{d}d"] if d else []) + ([f"{h}h"] if h else []) + ([f"{mi}m"] if mi and not d else [])
        rel = f" (in {' '.join(parts) or 'under a minute'})"
    return dt.strftime("%a %b %-d, %-I:%M %p ") + (dt.tzname() or "") + rel


# ---------------------------------------------------------------- media --
def media_path(ext: str = "jpg") -> Path:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    return MEDIA_DIR / f"{uuid.uuid4().hex}.{ext}"


def public_media_url(path: Path, mini_app_base: str) -> str:
    m = re.match(r"(https?://[^/]+)", mini_app_base or "")
    origin = m.group(1) if m else ""
    return f"{origin}/api/media/{path.name}"


_URL = re.compile(r"(https?://\S+|(?:www\.)?[a-z0-9-]+\.[a-z]{2,}(?:/\S*)?|@[A-Za-z0-9_]{3,})", re.I)


def parse_info(text: str) -> dict:
    """Free text -> {'description', 'website', 'x', 'telegram'}."""
    out = {"description": "", "website": "", "x": "", "telegram": ""}
    desc_parts = []
    for line in (text or "").splitlines():
        found = False
        for tok in line.split():
            t = tok.strip().rstrip(".,;")
            low = t.lower()
            if re.match(r"^(https?://)?(www\.)?(x|twitter)\.com/", low):
                out["x"] = out["x"] or ("https://" + re.sub(r"^https?://", "", t))
                found = True
            elif re.match(r"^(https?://)?(t|telegram)\.me/", low):
                out["telegram"] = out["telegram"] or ("https://" + re.sub(r"^https?://", "", t))
                found = True
            elif re.match(r"^https?://", low) or re.match(r"^(www\.)?[a-z0-9-]+\.[a-z]{2,}(/|$)", low):
                out["website"] = out["website"] or ("https://" + re.sub(r"^https?://", "", t))
                found = True
        if not found and line.strip():
            desc_parts.append(line.strip())
        elif found:
            rest = " ".join(w for w in line.split() if not _URL.fullmatch(w.strip().rstrip(".,;")))
            if rest.strip():
                desc_parts.append(rest.strip())
    out["description"] = " ".join(desc_parts)[:500]
    return out
