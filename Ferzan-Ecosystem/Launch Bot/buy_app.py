"""Ferzan Buy Bot Mini App backend (Telegram Web App).

Same rules as the Guardian app: the page (miniapp/buybot.html) is static; every call carries Telegram initData,
verified with the BUY bot token; every read and every change re-asks Telegram whether the person is STILL an admin of
that group; changes go straight into the Buy bot's own database using the same columns its chat commands use, and
each one is written to an audit table with the admin's id. No money moves here and no keys are held.
"""

from __future__ import annotations

import json
import math
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

MAX_GROUPS_CHECKED = 40
MIN_FLOOR_MAX = 1_000_000.0
WHALE_MAX = 100_000_000.0
LINK_HOSTS = {
    "link_tg": ("t.me", "telegram.me"),
    "link_x": ("x.com", "twitter.com"),
    "link_discord": ("discord.gg", "discord.com"),
}
LINK_COL = {"link_tg": "tg_url", "link_x": "x_url", "link_discord": "discord_url"}
_rate: dict = {}
_rate_lock = threading.Lock()
_groups_cache: dict = {}
_title_cache: dict = {}


def _token() -> str:
    return (os.environ.get("BUYBOT_TOKEN") or "").strip()


def _db_path() -> Path:
    return Path(os.environ.get("BUYBOT_DB", "/opt/ferzan/app/buybot.db"))


def _auth(init_data: str) -> dict:
    try:
        return _la.verify_init_data(init_data, token=_token())
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(401, "Open this from the Ferzan Buy bot in Telegram")


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
    tok = _token()
    if not tok:
        raise HTTPException(503, "The Buy bot is not set up on this server")
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/{method}", json=params, timeout=8)
        j = r.json()
    except Exception:
        raise HTTPException(502, "Telegram did not answer. Try again in a moment.")
    if not j.get("ok"):
        raise HTTPException(400, (j.get("description") or "Telegram refused")[:160])
    return j.get("result")


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
        raise HTTPException(503, "The Buy bot has no data yet")
    con = sqlite3.connect(p, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS miniapp_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, "
        "admin_id INTEGER, action TEXT, detail TEXT, ts INTEGER)"
    )
    have = {r[1] for r in con.execute("PRAGMA table_info(watches)")}
    need = {"min_usd", "emoji", "tg_url", "discord_url", "x_url", "whale_usd", "sell_alerts"}
    if not need <= have:
        con.close()
        raise HTTPException(503, "The Buy bot is still updating its database. Try again in a minute.")
    return con


def _audit(con: sqlite3.Connection, chat_id: int, uid: int, action: str, detail: str) -> None:
    con.execute(
        "INSERT INTO miniapp_audit(chat_id, admin_id, action, detail, ts) VALUES(?,?,?,?,strftime('%s','now'))",
        (chat_id, uid, action, detail[:200]),
    )


def _watching(con: sqlite3.Connection, chat_id: int) -> bool:
    return con.execute("SELECT 1 FROM watches WHERE chat_id=? LIMIT 1", (chat_id,)).fetchone() is not None


def _short(a: str) -> str:
    return a if len(a) <= 14 else f"{a[:6]}…{a[-4:]}"


def _clean_link(key: str, value) -> str:
    v = str(value or "").strip()
    if not v:
        return ""
    if len(v) > 200 or re.search(r"[\s<>\"']", v):
        raise HTTPException(400, "That link does not look right")
    if not v.lower().startswith("http"):
        v = "https://" + v
    m = re.match(r"^https://([A-Za-z0-9.-]+)(/[^\s]*)?$", v)
    if not m:
        raise HTTPException(400, "Links must start with https://")
    host = m.group(1).lower()
    if host not in LINK_HOSTS[key] and not any(host == h or host.endswith("." + h) for h in LINK_HOSTS[key]):
        raise HTTPException(400, "Only " + ", ".join(LINK_HOSTS[key]) + " links are accepted here")
    return v


def _money(value, lo: float, hi: float, label: str, allow_zero: bool = False) -> float:
    try:
        x = float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        raise HTTPException(400, f"{label} must be a number")
    if not math.isfinite(x):
        raise HTTPException(400, f"{label} must be a number")
    if x == 0 and allow_zero:
        return 0.0
    if x < lo or x > hi:
        raise HTTPException(400, f"{label} must be between ${lo:,.0f} and ${hi:,.0f}")
    return round(x, 2)


def _clean_emoji(value) -> str:
    e = str(value or "").strip()
    if not e or len(e) > 8 or re.search(r"[<>&\"'\x00-\x1f]", e) or e.isascii() and e.isalnum():
        raise HTTPException(400, "Pick one emoji (letters and numbers are not accepted)")
    return e


def _state(con: sqlite3.Connection, chat_id: int) -> dict:
    rows = con.execute(
        "SELECT chain, ca, min_usd, emoji, tg_url, x_url, discord_url, whale_usd, sell_alerts FROM watches "
        "WHERE chat_id=? ORDER BY last_ts DESC",
        (chat_id,),
    ).fetchall()
    first = rows[0] if rows else None
    fl = con.execute("SELECT tape, mute_until, raid_pin FROM chat_flags WHERE chat_id=?", (chat_id,)).fetchone()
    now = int(time.time())
    return {
        "min_usd": float(first["min_usd"] if first and first["min_usd"] is not None else 15),
        "whale_usd": float(first["whale_usd"] or 0) if first else 0.0,  # 0 = default (10x the minimum, at least $500)
        "emoji": (first["emoji"] if first and first["emoji"] else "🟢"),
        "sell": bool(first and first["sell_alerts"]),
        "tape": True if not fl or fl["tape"] is None else bool(fl["tape"]),
        "muted": bool(fl and int(fl["mute_until"] or 0) > now),
        "pin": bool(fl and int(fl["raid_pin"] or 0)),
        "links": {
            "link_tg": (first["tg_url"] or "") if first else "",
            "link_x": (first["x_url"] or "") if first else "",
            "link_discord": (first["discord_url"] or "") if first else "",
        },
        "tokens": [{"chain": r["chain"], "ca": r["ca"], "short": _short(r["ca"])} for r in rows],
    }


class AppBody(BaseModel):
    initData: str = ""


class GroupBody(AppBody):
    chat_id: int


class SetBody(GroupBody):
    key: str
    value: str | int | float | bool = ""


class UntrackBody(GroupBody):
    chain: str
    ca: str


def _title(chat_id: int) -> str:
    hit = _title_cache.get(chat_id)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        t = str((_tg("getChat", chat_id=chat_id) or {}).get("title") or chat_id)
    except HTTPException:
        t = str(chat_id)
    _title_cache[chat_id] = (time.time(), t)
    return t


@router.post("/api/buybot/groups")
def groups(body: AppBody):
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "read", 60, 60):
        raise HTTPException(429, "Slow down a little")
    hit = _groups_cache.get(uid)
    if hit and time.time() - hit[0] < 60:
        return {"name": user.get("first_name") or "", "groups": hit[1]}
    con = _con()
    try:
        ids = [int(r[0]) for r in con.execute(
            "SELECT chat_id, MAX(last_ts) m FROM watches WHERE chat_id<0 GROUP BY chat_id ORDER BY m DESC LIMIT ?",
            (MAX_GROUPS_CHECKED,),
        )]
    finally:
        con.close()
    with ThreadPoolExecutor(max_workers=8) as ex:
        flags = list(ex.map(lambda c: _is_admin(c, uid), ids))
    out = [{"chat_id": c, "title": _title(c)} for c, ok in zip(ids, flags) if ok]
    _groups_cache[uid] = (time.time(), out)
    return {"name": user.get("first_name") or "", "groups": out}


@router.post("/api/buybot/group")
def group(body: GroupBody):
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "read", 60, 60):
        raise HTTPException(429, "Slow down a little")
    _require_admin(body.chat_id, uid)
    con = _con()
    try:
        if not _watching(con, body.chat_id):
            raise HTTPException(404, "The Buy bot is not tracking a token in that group")
        st = _state(con, body.chat_id)
        t0 = int(time.time()) // 86400 * 86400
        n, vol = con.execute(
            "SELECT COUNT(*), COALESCE(SUM(usd),0) FROM buy_log WHERE chat_id=? AND ts>=? AND COALESCE(kind,'buy')='buy'",
            (body.chat_id, t0),
        ).fetchone()
        recent = con.execute(
            "SELECT ca, usd, ts, COALESCE(kind,'buy') k FROM buy_log WHERE chat_id=? ORDER BY id DESC LIMIT 12",
            (body.chat_id,),
        ).fetchall()
    finally:
        con.close()
    return {
        **st,
        "buys_today": int(n), "volume_today": round(float(vol), 2),
        "recent": [{"ca": _short(r["ca"] or ""), "usd": round(float(r["usd"] or 0), 2), "ts": r["ts"], "kind": r["k"]} for r in recent],
        "now": int(time.time()),
    }


@router.post("/api/buybot/set")
def set_option(body: SetBody):
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "write", 60, 3600):
        raise HTTPException(429, "Too many changes in an hour. Try again later.")
    _require_admin(body.chat_id, uid)
    key, val, cid = body.key, body.value, body.chat_id
    on = val is True or str(val).lower() in ("1", "true", "on")
    con = _con()
    try:
        if not _watching(con, cid):
            raise HTTPException(404, "The Buy bot is not tracking a token in that group")
        if key == "min":
            x = _money(val, 1, MIN_FLOOR_MAX, "Minimum buy")
            con.execute("UPDATE watches SET min_usd=? WHERE chat_id=?", (x, cid))
            _audit(con, cid, uid, key, f"${x:g}")
        elif key == "whale":
            x = _money(val, 1, WHALE_MAX, "Whale level", allow_zero=True)
            con.execute("UPDATE watches SET whale_usd=? WHERE chat_id=?", (x, cid))
            _audit(con, cid, uid, key, f"${x:g}" if x else "default")
        elif key == "emoji":
            e = _clean_emoji(val)
            con.execute("UPDATE watches SET emoji=? WHERE chat_id=?", (e, cid))
            _audit(con, cid, uid, key, e)
        elif key == "sell":
            con.execute("UPDATE watches SET sell_alerts=? WHERE chat_id=?", (int(on), cid))
            _audit(con, cid, uid, key, "on" if on else "off")
        elif key == "tape":
            con.execute(
                "INSERT INTO chat_flags(chat_id, tape, mute_until) VALUES(?,?,0) ON CONFLICT(chat_id) DO UPDATE SET tape=excluded.tape",
                (cid, int(on)),
            )
            _audit(con, cid, uid, key, "on" if on else "off")
        elif key == "mute":
            until = int(time.time()) + 3600 if on else 0
            con.execute(
                "INSERT INTO chat_flags(chat_id, tape, mute_until) VALUES(?,1,?) ON CONFLICT(chat_id) DO UPDATE SET mute_until=excluded.mute_until",
                (cid, until),
            )
            _audit(con, cid, uid, key, "1 hour" if on else "off")
        elif key == "pin":
            con.execute(
                "INSERT INTO chat_flags(chat_id, tape, mute_until, raid_pin) VALUES(?,1,0,?) ON CONFLICT(chat_id) DO UPDATE SET raid_pin=excluded.raid_pin",
                (cid, int(on)),
            )
            _audit(con, cid, uid, key, "on" if on else "off")
        elif key in LINK_COL:
            url = _clean_link(key, val)
            con.execute(f"UPDATE watches SET {LINK_COL[key]}=? WHERE chat_id=?", (url, cid))
            _audit(con, cid, uid, key, url or "cleared")
        else:
            raise HTTPException(400, "Unknown setting")
        con.commit()
        return _state(con, cid)
    finally:
        con.close()


@router.post("/api/buybot/untrack")
def untrack(body: UntrackBody):
    """Stop buy alerts for ONE token in the group."""
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "write", 60, 3600):
        raise HTTPException(429, "Too many changes in an hour. Try again later.")
    _require_admin(body.chat_id, uid)
    con = _con()
    try:
        cur = con.execute("DELETE FROM watches WHERE chat_id=? AND chain=? AND ca=?", (body.chat_id, body.chain, body.ca))
        if cur.rowcount == 0:
            raise HTTPException(404, "That token is not tracked in this group")
        _audit(con, body.chat_id, uid, "untrack", f"{body.chain} {_short(body.ca)}")
        con.commit()
        left = _watching(con, body.chat_id)
        return {"ok": True, "left": left, **(_state(con, body.chat_id) if left else {})}
    finally:
        con.close()
