"""Ferzan Guardian Mini App backend (Telegram Web App).

The page (miniapp/guardian.html) is static. These routes give it Telegram-authenticated data:
initData is verified with the GUARDIAN bot token (HMAC-SHA256). Every read and every change
re-asks Telegram whether the signed-in person is STILL an admin of that group (nothing the page
says is trusted). Changes go straight into the Guardian bot's own database, using the same columns
the chat commands use, and are written to its config audit log with the admin's id.
No money moves here and no keys are held.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import launch_app as _la

router = APIRouter()

INIT_DATA_MAX_AGE_S = 12 * 3600
MAX_GROUPS_CHECKED = 40
MAX_CAS = 20
_rate: dict = {}
_rate_lock = threading.Lock()
_groups_cache: dict = {}

EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# key -> (settings column, kind). "slow" is on/off stored as 10 seconds / 0.
TOGGLES = {
    "newcheck": ("captcha_enabled", "bool"),
    "antilink": ("antilink", "bool"),
    "caguard": ("caguard_enabled", "bool"),
    "antiforward": ("lock_forwards", "bool"),
    "nostickers": ("lock_stickers", "bool"),
    "slow": ("slowmode_seconds", "slow"),
}
CAPTCHA_STORE = {"tap": "tap", "full": "button", "math": "math"}  # what guardian_bot._captcha_mode reads back
WARN_CHOICES = (2, 3, 5)
LABELS = {
    "newcheck": "New-member check", "antilink": "Block links", "caguard": "Contract guard",
    "antiforward": "Block forwards", "nostickers": "Block stickers", "slow": "Slow mode",
}


_file_tokens: dict = {}


def _token() -> str:
    """The bot token: this service's environment first, else the shared server env file the bots themselves load
    (the launch API's own env file does not carry the other bots' tokens). Read-only; never logged."""
    v = (os.environ.get("GUARDIAN_TOKEN") or "").strip()
    if v:
        return v
    path = os.environ.get("FERZAN_ENV_FILE", "/opt/ferzan/.env")
    try:
        mt = os.stat(path).st_mtime
        if _file_tokens.get("k") != (path, mt):
            val = ""
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.startswith("GUARDIAN_TOKEN="):
                        val = line.split("=", 1)[1].strip().strip("'\"")
            _file_tokens.update(k=(path, mt), v=val)
        return _file_tokens.get("v", "")
    except OSError:
        return ""


def _db_path() -> Path:
    return Path(os.environ.get("GUARDIAN_DB", "/opt/ferzan/app/guardian.db"))


def _auth(init_data: str) -> dict:
    try:
        user = _la.verify_init_data(init_data, token=_token())
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(401, "Open this from the Ferzan Guardian bot in Telegram")
    return user


def _rate_ok(uid: int, bucket: str, limit: int, window: int) -> bool:
    now = time.time()
    key = (bucket, uid)
    with _rate_lock:
        hits = [t for t in _rate.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _rate[key] = hits
            return False
        hits.append(now)
        _rate[key] = hits
    return True


def _tg(method: str, **params):
    """One Bot API call with the Guardian token. Returns the result or raises HTTPException."""
    tok = _token()
    if not tok:
        raise HTTPException(503, "Guardian is not set up on this server")
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/{method}", json=params, timeout=8)
        j = r.json()
    except Exception:
        raise HTTPException(502, "Telegram did not answer. Try again in a moment.")
    if not j.get("ok"):
        raise HTTPException(400, (j.get("description") or "Telegram refused")[:160])
    return j.get("result")


_bot_cache: dict = {}


def _bot_username() -> str:
    """The bot's @name (for the add-to-group link), asked once and remembered."""
    if "u" not in _bot_cache:
        try:
            r = _tg("getMe")
            _bot_cache["u"] = re.sub(r"[^A-Za-z0-9_]", "", str(r.get("username") or "")) if isinstance(r, dict) else ""
        except HTTPException:
            return ""
    return _bot_cache["u"]


def _is_admin(chat_id: int, uid: int) -> bool:
    try:
        m = _tg("getChatMember", chat_id=chat_id, user_id=uid)
    except HTTPException:
        return False
    return (m or {}).get("status") in ("creator", "administrator")


def _require_admin(chat_id: int, uid: int) -> None:
    if not _is_admin(chat_id, uid):
        raise HTTPException(403, "You are not an admin of that group")


def _con() -> sqlite3.Connection:
    p = _db_path()
    if not p.exists():
        raise HTTPException(503, "Guardian has no data yet")
    con = sqlite3.connect(p, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    return con


def _need_columns(con: sqlite3.Connection) -> None:
    have = {r[1] for r in con.execute("PRAGMA table_info(settings)")}
    need = {c for c, _ in TOGGLES.values()} | {"captcha_mode", "warn_limit", "official_cas"}
    if not need <= have:
        raise HTTPException(503, "Guardian is still updating its database. Try again in a minute.")


def _ensure_row(con: sqlite3.Connection, chat_id: int) -> None:
    con.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))


def _is_chat_known(con: sqlite3.Connection, chat_id: int) -> bool:
    return con.execute("SELECT 1 FROM known_chats WHERE chat_id=?", (chat_id,)).fetchone() is not None


def _valid_ca(value: str) -> str:
    v = (value or "").strip()
    if v.upper() == "FERZAN":
        return "FERZAN"
    if EVM_RE.match(v) or SOL_RE.match(v):
        return v
    raise HTTPException(400, "That does not look like a contract address (0x… for EVM chains, or a Solana address)")


def _short(a: str) -> str:
    return a if len(a) <= 14 else f"{a[:6]}…{a[-4:]}"


def _chain_label(a: str) -> str:
    if a.upper() == "FERZAN":
        return "FERZAN"
    return "EVM" if a.startswith("0x") else "Solana"


def _state(con: sqlite3.Connection, chat_id: int) -> dict:
    r = con.execute(
        "SELECT antilink, captcha_enabled, captcha_mode, caguard_enabled, official_cas, lock_forwards, "
        "lock_stickers, slowmode_seconds, warn_limit FROM settings WHERE chat_id=?",
        (chat_id,),
    ).fetchone()
    t = {"newcheck": False, "antilink": True, "caguard": False, "antiforward": False, "nostickers": False, "slow": False}
    mode, warn, cas = "full", 3, []
    if r:
        t.update(
            newcheck=bool(r["captcha_enabled"]),
            antilink=True if r["antilink"] is None else bool(r["antilink"]),
            caguard=bool(r["caguard_enabled"]),
            antiforward=bool(r["lock_forwards"]),
            nostickers=bool(r["lock_stickers"]),
            slow=bool(r["slowmode_seconds"]),
        )
        m = r["captcha_mode"] or "button"
        mode = "tap" if m == "tap" else "math" if m == "math" else "full"
        warn = int(r["warn_limit"]) if r["warn_limit"] is not None else 3
        cas = [x for x in (r["official_cas"] or "").split() if x]
    return {"toggles": t, "captcha_mode": mode, "warn_limit": warn,
            "cas": [{"value": c, "short": _short(c), "chain": _chain_label(c)} for c in cas]}


# ---- protection levels (same three as the bot's /gsetup) and who may run each moderation command (/gsetperm)
# Kept in step with guardian_bot.TIERS by a test that reads the bot's source.
TIER_ORDER = ("standard", "shield", "fortress")
_STANDARD = {"captcha_enabled": 1, "captcha_mode": "button", "antilink": 1, "welcome_enabled": 1,
             "goodbye_enabled": 1, "cleanservice_enabled": 1, "warn_limit": 3}
_SHIELD = {**_STANDARD, "linkscan_enabled": 1, "newacct_enabled": 1, "adaptive_slowmode_enabled": 1,
           "rules_gate_enabled": 1}
_FORTRESS = {**_SHIELD, "lock_links": 1, "lock_forwards": 1, "warn_limit": 2, "votemute_enabled": 1}
TIERS = {
    "standard": {"name": "Standard", "set": _STANDARD, "min": {},
                 "blurb": "Clean joins for a normal community: one-message join check, new-member link lock, welcome and goodbye cleanup, 3 strikes."},
    "shield": {"name": "Shield", "set": _SHIELD, "min": {},
               "blurb": "Standard plus scam-link scanning, extra checks on brand-new accounts, adaptive slow mode when the chat floods, a rules gate, and the contract guard once you add the official address."},
    "fortress": {"name": "Fortress", "set": _FORTRESS, "min": {"slowmode_seconds": 10},
                 "blurb": "Shield plus no links or forwards from members, vote-mute, 2 strikes and 10s slow mode. For launch day or a raid."},
}
PERM_DEFAULTS = {"gban": "admin", "gunban": "admin", "glockdown": "admin", "gmute": "mod", "gunmute": "mod", "gkick": "mod"}
PERM_LABELS = {"gban": "Ban", "gunban": "Unban", "glockdown": "Lock the group", "gmute": "Mute", "gunmute": "Unmute", "gkick": "Kick"}
_bot_id: dict = {}


def _bot_is_admin(chat_id: int) -> bool:
    try:
        if "id" not in _bot_id:
            _bot_id["id"] = int(_tg("getMe")["id"])
        m = _tg("getChatMember", chat_id=chat_id, user_id=_bot_id["id"])
    except (HTTPException, KeyError, TypeError, ValueError):
        return False
    return (m or {}).get("status") in ("creator", "administrator")


def _tier_plan(con: sqlite3.Connection, chat_id: int, tier: str) -> tuple[dict, list[str]]:
    """({column: value}, [things left alone and why]). Same rules as the bot: texts, media, filters and mods are never touched."""
    spec = TIERS[tier]
    plan = dict(spec["set"])
    notes: list[str] = []
    row = con.execute("SELECT rules_text, official_cas, slowmode_seconds FROM settings WHERE chat_id=?", (chat_id,)).fetchone()
    rules, cas, slow = (row["rules_text"], row["official_cas"], row["slowmode_seconds"]) if row else (None, None, 0)
    if plan.get("rules_gate_enabled") and not (rules or "").strip():
        plan.pop("rules_gate_enabled")
        notes.append("Rules gate skipped: no rules are set yet (set them with /setrules in the group).")
    for col, floor in spec["min"].items():
        if (slow or 0) < floor:
            plan[col] = floor
    if tier in ("shield", "fortress"):
        if (cas or "").strip():
            plan["caguard_enabled"] = 1
        else:
            notes.append("Contract guard skipped: add the official address first (Settings tab).")
    return plan, notes


def _setup_info(con: sqlite3.Connection, chat_id: int) -> dict:
    row = con.execute("SELECT protection_tier FROM settings WHERE chat_id=?", (chat_id,)).fetchone() if _has_col(con, "protection_tier") else None
    cur = (row["protection_tier"] if row and row["protection_tier"] else "") or ""
    try:
        over = {r["command"]: r["tier"] for r in con.execute("SELECT command, tier FROM command_perms WHERE chat_id=?", (chat_id,))}
    except sqlite3.OperationalError:  # the bot has not created the table yet: everything is at its default
        over = {}
    return {
        "tier": cur,
        "tiers": [{"id": t, "name": TIERS[t]["name"], "blurb": TIERS[t]["blurb"]} for t in TIER_ORDER],
        "perms": [{"command": c, "label": PERM_LABELS[c], "default": d, "tier": over.get(c) if over.get(c) in ("mod", "admin") else d}
                  for c, d in PERM_DEFAULTS.items()],
    }


def _has_col(con: sqlite3.Connection, col: str) -> bool:
    return col in {r[1] for r in con.execute("PRAGMA table_info(settings)")}


def _day_start() -> int:
    return int(time.time()) // 86400 * 86400


class AppBody(BaseModel):
    initData: str = ""


class GroupBody(AppBody):
    chat_id: int


class SetBody(GroupBody):
    key: str
    value: str | int | bool = ""


class TierBody(GroupBody):
    tier: str  # standard | shield | fortress | undo


class PermBody(GroupBody):
    command: str
    tier: str  # mod | admin


class ReportBody(GroupBody):
    report_id: int
    action: str  # dismiss | warn | ban


@router.post("/api/guardian/groups")
def groups(body: AppBody):
    """The groups the signed-in person admins (checked live with Telegram, cached for a minute)."""
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "read", 60, 60):
        raise HTTPException(429, "Slow down a little")
    hit = _groups_cache.get(uid)
    if hit and time.time() - hit[0] < 60:
        return {"name": user.get("first_name") or "", "groups": hit[1], "bot": _bot_username()}
    con = _con()
    try:
        rows = con.execute(
            "SELECT chat_id, title FROM known_chats ORDER BY last_seen DESC LIMIT ?", (MAX_GROUPS_CHECKED,)
        ).fetchall()
    finally:
        con.close()
    with ThreadPoolExecutor(max_workers=8) as ex:
        flags = list(ex.map(lambda r: _is_admin(int(r["chat_id"]), uid), rows))
    out = [{"chat_id": int(r["chat_id"]), "title": r["title"] or str(r["chat_id"])} for r, ok in zip(rows, flags) if ok]
    _groups_cache[uid] = (time.time(), out)
    return {"name": user.get("first_name") or "", "groups": out, "bot": _bot_username()}


@router.post("/api/guardian/group")
def group(body: GroupBody):
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "read", 60, 60):
        raise HTTPException(429, "Slow down a little")
    _require_admin(body.chat_id, uid)
    con = _con()
    try:
        _need_columns(con)
        if not _is_chat_known(con, body.chat_id):
            raise HTTPException(404, "Guardian is not in that group")
        st = _state(con, body.chat_id)
        st.update(_setup_info(con, body.chat_id))
        t0 = _day_start()
        acts = con.execute("SELECT COUNT(*) FROM audit_log WHERE chat_id=? AND ts>=?", (body.chat_id, t0)).fetchone()[0]
        joins = con.execute("SELECT COUNT(*) FROM joins WHERE chat_id=? AND joined_ts>=?", (body.chat_id, t0)).fetchone()[0]
        reps = con.execute(
            "SELECT id, target_id, text, ts FROM reports WHERE chat_id=? AND status='open' ORDER BY id DESC LIMIT 30",
            (body.chat_id,),
        ).fetchall()
        a = con.execute(
            "SELECT 'mod' k, mod_id who, target_id tgt, action act, reason det, ts FROM audit_log WHERE chat_id=? "
            "UNION ALL SELECT 'cfg', admin_id, 0, action, detail, ts FROM config_audit_log WHERE chat_id=? "
            "ORDER BY ts DESC LIMIT 30",
            (body.chat_id, body.chat_id),
        ).fetchall()
    finally:
        con.close()
    try:
        members = int(_tg("getChatMemberCount", chat_id=body.chat_id))
    except HTTPException:
        members = 0
    st["bot_admin"] = _bot_is_admin(body.chat_id)
    return {
        **st,
        "members": members, "actions_today": acts, "joined_today": joins,
        "reports": [{"id": r["id"], "target": r["target_id"], "text": (r["text"] or "")[:160], "ts": r["ts"]} for r in reps],
        "activity": [{"kind": x["k"], "who": x["who"], "target": x["tgt"], "action": x["act"], "detail": (x["det"] or "")[:160], "ts": x["ts"]} for x in a],
        "now": int(time.time()),
    }


def _cfg_log(con: sqlite3.Connection, chat_id: int, uid: int, action: str, detail: str) -> None:
    con.execute(
        "INSERT INTO config_audit_log(chat_id, admin_id, action, detail, ts) VALUES(?,?,?,?,strftime('%s','now'))",
        (chat_id, uid, f"app:{action}", detail[:200]),
    )


@router.post("/api/guardian/set")
def set_option(body: SetBody):
    """One change at a time, from a fixed list. Returns the group's settings as they are now."""
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "write", 60, 3600):
        raise HTTPException(429, "Too many changes in an hour. Try again later.")
    _require_admin(body.chat_id, uid)
    key, val = body.key, body.value
    con = _con()
    try:
        _need_columns(con)
        if not _is_chat_known(con, body.chat_id):
            raise HTTPException(404, "Guardian is not in that group")
        _ensure_row(con, body.chat_id)
        cid = body.chat_id
        if key in TOGGLES:
            col, kind = TOGGLES[key]
            on = val is True or str(val).lower() in ("1", "true", "on")
            num = (10 if on else 0) if kind == "slow" else int(on)
            con.execute(f"UPDATE settings SET {col}=? WHERE chat_id=?", (num, cid))
            _cfg_log(con, cid, uid, key, "on" if on else "off")
        elif key == "captcha_mode":
            if str(val) not in CAPTCHA_STORE:
                raise HTTPException(400, "Unknown check type")
            con.execute("UPDATE settings SET captcha_mode=? WHERE chat_id=?", (CAPTCHA_STORE[str(val)], cid))
            _cfg_log(con, cid, uid, key, str(val))
        elif key == "warn_limit":
            try:
                n = int(val)
            except (TypeError, ValueError):
                raise HTTPException(400, "Pick 2, 3 or 5")
            if n not in WARN_CHOICES:
                raise HTTPException(400, "Pick 2, 3 or 5")
            con.execute("UPDATE settings SET warn_limit=? WHERE chat_id=?", (n, cid))
            _cfg_log(con, cid, uid, key, str(n))
        elif key in ("ca_add", "ca_remove"):
            row = con.execute("SELECT official_cas FROM settings WHERE chat_id=?", (cid,)).fetchone()
            cur = [x for x in (row["official_cas"] or "").split() if x]
            if key == "ca_add":
                v = _valid_ca(str(val))
                if v.lower() in {c.lower() for c in cur}:
                    raise HTTPException(400, "That address is already on the list")
                if len(cur) >= MAX_CAS:
                    raise HTTPException(400, f"The list is full ({MAX_CAS} addresses)")
                cur.append(v)
            else:
                v = str(val).strip()
                if v not in cur:
                    raise HTTPException(404, "That address is not on the list")
                cur.remove(v)
            con.execute("UPDATE settings SET official_cas=? WHERE chat_id=?", (" ".join(cur), cid))
            _cfg_log(con, cid, uid, key, _short(v))
        else:
            raise HTTPException(400, "Unknown setting")
        con.commit()
        return _state(con, cid)
    finally:
        con.close()


@router.post("/api/guardian/report")
def report_action(body: ReportBody):
    """Dismiss a report, warn the reported member, or ban them. Warn and ban act through Telegram."""
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "write", 60, 3600):
        raise HTTPException(429, "Too many changes in an hour. Try again later.")
    if body.action not in ("dismiss", "warn", "ban"):
        raise HTTPException(400, "Unknown action")
    _require_admin(body.chat_id, uid)
    con = _con()
    try:
        r = con.execute(
            "SELECT id, target_id FROM reports WHERE id=? AND chat_id=? AND status='open'", (body.report_id, body.chat_id)
        ).fetchone()
        if not r:
            raise HTTPException(404, "That report is already closed")
        target = int(r["target_id"] or 0)
        cid = body.chat_id
        if body.action != "dismiss":
            if not target:
                raise HTTPException(400, "This report has no member to act on")
            if _is_admin(cid, target):
                raise HTTPException(400, "That member is an admin. Remove their role in Telegram first.")
        banned = False
        if body.action == "warn":
            row = con.execute("SELECT warn_limit FROM settings WHERE chat_id=?", (cid,)).fetchone()
            limit = int(row["warn_limit"]) if row and row["warn_limit"] is not None else 3
            con.execute(
                "INSERT INTO warns(chat_id, user_id, count, last_ts) VALUES(?,?,1,?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET count=count+1, last_ts=excluded.last_ts",
                (cid, target, int(time.time())),
            )
            n = con.execute("SELECT count FROM warns WHERE chat_id=? AND user_id=?", (cid, target)).fetchone()[0]
            banned = n >= limit
        if body.action == "ban" or banned:
            _tg("banChatMember", chat_id=cid, user_id=target)
        con.execute("UPDATE reports SET status='resolved', resolved_by=? WHERE id=? AND chat_id=?", (uid, r["id"], cid))
        con.execute(
            "INSERT INTO audit_log(chat_id, mod_id, target_id, action, reason, ts) VALUES(?,?,?,?,?,strftime('%s','now'))",
            (cid, uid, target, "ban" if (body.action == "ban" or banned) else body.action, f"app report #{r['id']}"),
        )
        con.commit()
        return {"ok": True, "banned": body.action == "ban" or banned}
    finally:
        con.close()


@router.post("/api/guardian/tier")
def set_tier(body: TierBody):
    """Switch the group to a protection level (or undo the last switch). Works like /gsetup in the chat."""
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "write", 60, 3600):
        raise HTTPException(429, "Too many changes in an hour. Try again later.")
    if body.tier != "undo" and body.tier not in TIERS:
        raise HTTPException(400, "Unknown protection level")
    _require_admin(body.chat_id, uid)
    cid = body.chat_id
    con = _con()
    try:
        _need_columns(con)
        if not _is_chat_known(con, cid):
            raise HTTPException(404, "Guardian is not in that group")
        if not _has_col(con, "protection_tier") or not _has_col(con, "tier_backup"):
            raise HTTPException(503, "Guardian is still updating its database. Try again in a minute.")
        _ensure_row(con, cid)
        valid = {r[1] for r in con.execute("PRAGMA table_info(settings)")}
        notes: list[str] = []
        if body.tier == "undo":
            r = con.execute("SELECT tier_backup FROM settings WHERE chat_id=?", (cid,)).fetchone()
            try:
                backup = json.loads(r["tier_backup"]) if r and r["tier_backup"] else None
            except ValueError:
                backup = None
            if not isinstance(backup, dict):
                raise HTTPException(400, "There is nothing to undo")
            prev = backup.pop("__tier", None)
            cols = [c for c in backup if c in valid and c not in ("protection_tier", "tier_backup")]
            sets = ", ".join(f"{c}=?" for c in cols + ["protection_tier"]) + ", tier_backup=NULL"
            con.execute(f"UPDATE settings SET {sets} WHERE chat_id=?", [backup[c] for c in cols] + [prev, cid])
            _cfg_log(con, cid, uid, "tier_undo", "")
        else:
            plan, notes = _tier_plan(con, cid, body.tier)
            missing = [c for c in plan if c not in valid]
            if missing:
                raise HTTPException(503, "Guardian is still updating its database. Try again in a minute.")
            cols = list(plan)
            old = con.execute(f"SELECT {', '.join(cols)}, protection_tier FROM settings WHERE chat_id=?", (cid,)).fetchone()
            backup = {c: old[c] for c in cols}
            backup["__tier"] = old["protection_tier"]
            sets = ", ".join(f"{c}=?" for c in cols) + ", protection_tier=?, tier_backup=?"
            con.execute(f"UPDATE settings SET {sets} WHERE chat_id=?",
                        [plan[c] for c in cols] + [body.tier, json.dumps(backup), cid])
            _cfg_log(con, cid, uid, "tier", body.tier)
        con.commit()
        return {**_state(con, cid), **_setup_info(con, cid), "notes": notes}
    finally:
        con.close()


@router.post("/api/guardian/perm")
def set_perm(body: PermBody):
    """Choose who may run one moderation command: any Guardian mod, or full admins only."""
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "write", 60, 3600):
        raise HTTPException(429, "Too many changes in an hour. Try again later.")
    if body.command not in PERM_DEFAULTS:
        raise HTTPException(400, "Unknown command")
    if body.tier not in ("mod", "admin"):
        raise HTTPException(400, "Pick mod or admin")
    _require_admin(body.chat_id, uid)
    cid = body.chat_id
    con = _con()
    try:
        if not _is_chat_known(con, cid):
            raise HTTPException(404, "Guardian is not in that group")
        con.execute("CREATE TABLE IF NOT EXISTS command_perms (chat_id INTEGER, command TEXT, tier TEXT, PRIMARY KEY (chat_id, command))")
        con.execute(
            "INSERT INTO command_perms(chat_id, command, tier) VALUES(?,?,?) "
            "ON CONFLICT(chat_id, command) DO UPDATE SET tier=excluded.tier",
            (cid, body.command, body.tier),
        )
        _cfg_log(con, cid, uid, "set_perm", f"/{body.command} -> {body.tier}")
        con.commit()
        return _setup_info(con, cid)
    finally:
        con.close()
