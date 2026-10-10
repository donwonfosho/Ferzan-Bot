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


_file_tokens: dict = {}


def _token() -> str:
    """The bot token: this service's environment first, else the shared server env file the bots themselves load
    (the launch API's own env file does not carry the other bots' tokens). Read-only; never logged."""
    v = (os.environ.get("BUYBOT_TOKEN") or "").strip()
    if v:
        return v
    path = os.environ.get("FERZAN_ENV_FILE", "/opt/ferzan/.env")
    try:
        mt = os.stat(path).st_mtime
        if _file_tokens.get("k") != (path, mt):
            val = ""
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.startswith("BUYBOT_TOKEN="):
                        val = line.split("=", 1)[1].strip().strip("'\"")
            _file_tokens.update(k=(path, mt), v=val)
        return _file_tokens.get("v", "")
    except OSError:
        return ""


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
        raise HTTPException(503, "The Buy bot has no data yet")
    con = sqlite3.connect(p, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.row_factory = sqlite3.Row
    con.execute(
        "CREATE TABLE IF NOT EXISTS miniapp_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, "
        "admin_id INTEGER, action TEXT, detail TEXT, ts INTEGER)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS known_groups (chat_id INTEGER PRIMARY KEY, title TEXT, ts INTEGER)"
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


# ---- adding a token: the same pool look-up the bot's /track uses (DexScreener, then GeckoTerminal, then the Ferzan curve),
# run here on the server so the page never decides which pool gets watched.
MAX_TOKENS_PER_GROUP = 5
DS_CHAIN = {"sol": "solana", "eth": "ethereum", "base": "base", "bsc": "bsc", "arb": "arbitrum", "avax": "avalanche",
            "pol": "polygon", "arc": "arc", "tron": "tron", "ton": "ton"}
GT_NET = {"sol": "solana", "eth": "eth", "base": "base", "bsc": "bsc", "arb": "arbitrum", "avax": "avax",
          "pol": "polygon", "arc": "arc", "tron": "tron", "ton": "ton"}
EVM_CHAINS = ("eth", "base", "bsc", "arb", "avax", "pol", "arc")
CHAIN_LABEL = {"sol": "Solana", "eth": "Ethereum", "base": "Base", "bsc": "BNB Chain", "arb": "Arbitrum",
               "avax": "Avalanche", "pol": "Polygon", "arc": "Arc", "tron": "Tron", "ton": "TON"}
_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_TRON_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")
_TON_RE = re.compile(r"^((EQ|UQ)[A-Za-z0-9_-]{46}|0:[0-9a-f]{64})$")
FERZAN_API = (os.environ.get("FERZAN_LAUNCH_API") or "https://launch.ferzaneco.com/api").rstrip("/")


def _clean_token(chain, ca) -> tuple[str, str]:
    c = str(chain or "").strip().lower()
    a = str(ca or "").strip()
    if c not in DS_CHAIN:
        raise HTTPException(400, "Pick a chain from the list")
    ok = (
        bool(_EVM_RE.match(a)) if c in EVM_CHAINS
        else bool(_SOL_RE.match(a)) if c == "sol"
        else bool(_TRON_RE.match(a)) if c == "tron"
        else bool(_TON_RE.match(a))
    )
    if not ok:
        raise HTTPException(400, "That address does not look right for " + CHAIN_LABEL[c])
    return c, (a.lower() if c in EVM_CHAINS else a)


def _get_json(url: str, **kw):
    try:
        r = requests.get(url, timeout=10, headers={"Accept": "application/json"}, **kw)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def _lookup_pool(chain: str, ca: str) -> dict | None:
    """{'pool','name','symbol','dex'} or None. chain and ca must already be validated."""
    want = DS_CHAIN[chain]
    for url in (f"https://api.dexscreener.com/latest/dex/tokens/{ca}", f"https://api.dexscreener.com/latest/dex/search?q={ca}"):
        pairs = [p for p in ((_get_json(url) or {}).get("pairs") or []) if str(p.get("chainId") or "").lower() == want]
        if pairs and pairs[0].get("pairAddress"):
            p = pairs[0]
            base = p.get("baseToken") or {}
            return {"pool": p["pairAddress"], "name": base.get("name") or base.get("symbol") or "",
                    "symbol": base.get("symbol") or "", "dex": str(p.get("dexId") or "")}
    rows = ((_get_json(f"https://api.geckoterminal.com/api/v2/networks/{GT_NET[chain]}/tokens/{ca}/pools?page=1") or {}).get("data")) or []
    if rows:
        pid = str(rows[0].get("id") or "")
        a = rows[0].get("attributes") or {}
        nm = str(a.get("name") or "")
        return {"pool": pid.split("_", 1)[-1] if "_" in pid else pid, "name": nm, "symbol": "", "dex": ""}
    if chain in EVM_CHAINS or chain == "tron":
        key = ca if chain == "tron" else ca.lower()
        d = _get_json(f"{FERZAN_API}/curve-by-token/{key}", params={"since": 0, "kind": "buy"}) or {}
        if d.get("found") and not d.get("graduated"):
            return {"pool": "ferzan:" + key, "name": d.get("name") or "", "symbol": d.get("symbol") or "", "dex": "ferzan curve"}
    return None


class TrackBody(GroupBody):
    chain: str
    ca: str
    min_usd: float | None = None
    sell: bool | None = None
    whale: float | None = None




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
        return {"name": user.get("first_name") or "", "groups": hit[1], "bot": _bot_username()}
    con = _con()
    try:
        ids = [int(r[0]) for r in con.execute(
            "SELECT chat_id, MAX(m) mm FROM (SELECT chat_id, MAX(last_ts) m FROM watches WHERE chat_id<0 GROUP BY chat_id "
            "UNION ALL SELECT chat_id, ts m FROM known_groups WHERE chat_id<0) GROUP BY chat_id ORDER BY mm DESC LIMIT ?",
            (MAX_GROUPS_CHECKED,),
        )]
    finally:
        con.close()
    with ThreadPoolExecutor(max_workers=8) as ex:
        flags = list(ex.map(lambda c: _is_admin(c, uid), ids))
    out = [{"chat_id": c, "title": _title(c)} for c, ok in zip(ids, flags) if ok]
    _groups_cache[uid] = (time.time(), out)
    return {"name": user.get("first_name") or "", "groups": out, "bot": _bot_username()}


@router.post("/api/buybot/group")
def group(body: GroupBody):
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "read", 60, 60):
        raise HTTPException(429, "Slow down a little")
    _require_admin(body.chat_id, uid)
    con = _con()
    try:
        st = _state(con, body.chat_id)
        st["tracking"] = bool(st["tokens"])
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
        con.execute("INSERT OR REPLACE INTO known_groups(chat_id, title, ts) VALUES(?,?,strftime('%s','now'))",
                    (body.chat_id, _title(body.chat_id)))
        _audit(con, body.chat_id, uid, "untrack", f"{body.chain} {_short(body.ca)}")
        con.commit()
        left = _watching(con, body.chat_id)
        return {"ok": True, "left": left, **(_state(con, body.chat_id) if left else {})}
    finally:
        con.close()


@router.post("/api/buybot/lookup")
def lookup(body: TrackBody):
    """Check a token before adding it: does the same pool look-up the bot's /track does. Changes nothing."""
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "lookup", 20, 600):
        raise HTTPException(429, "Too many look-ups. Wait a few minutes.")
    _require_admin(body.chat_id, uid)
    chain, ca = _clean_token(body.chain, body.ca)
    hit = _lookup_pool(chain, ca)
    if not hit:
        return {"found": False, "chain": chain, "ca": ca}
    return {"found": True, "chain": chain, "ca": ca, "name": hit["name"], "symbol": hit["symbol"], "dex": hit["dex"]}


@router.post("/api/buybot/track")
def track(body: TrackBody):
    """Start buy alerts for a token in this group (same row the /track command writes)."""
    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid, "write", 60, 3600):
        raise HTTPException(429, "Too many changes in an hour. Try again later.")
    if not _rate_ok(uid, "lookup", 20, 600):
        raise HTTPException(429, "Too many look-ups. Wait a few minutes.")
    _require_admin(body.chat_id, uid)
    chain, ca = _clean_token(body.chain, body.ca)
    cid = body.chat_id
    min_usd = None if body.min_usd is None else _money(body.min_usd, 1, MIN_FLOOR_MAX, "Minimum buy")
    whale = None if body.whale is None else _money(body.whale, 1, WHALE_MAX, "Whale level", allow_zero=True)
    con = _con()
    try:
        have = con.execute("SELECT chain, ca FROM watches WHERE chat_id=?", (cid,)).fetchall()
        if any(r["chain"] == chain and str(r["ca"]).lower() == ca.lower() for r in have):
            raise HTTPException(409, "That token is already tracked in this group")
        if len(have) >= MAX_TOKENS_PER_GROUP:
            raise HTTPException(400, f"A group can track up to {MAX_TOKENS_PER_GROUP} tokens. Stop one first.")
    finally:
        con.close()
    hit = _lookup_pool(chain, ca)  # outside the db connection: network calls can be slow
    if not hit:
        raise HTTPException(404, "No pool found for that address yet. Check the chain and address.")
    con = _con()
    try:
        first = con.execute(
            "SELECT min_usd, emoji, tg_url, x_url, discord_url, whale_usd, sell_alerts FROM watches WHERE chat_id=? "
            "ORDER BY last_ts DESC LIMIT 1", (cid,),
        ).fetchone()
        floor = min_usd if min_usd is not None else (float(first["min_usd"]) if first and first["min_usd"] is not None else 15.0)
        con.execute(
            "INSERT OR REPLACE INTO watches(chat_id, chain, ca, pool, last_ts, min_usd) VALUES(?,?,?,?,?,?)",
            (cid, chain, ca, hit["pool"], int(time.time()), floor),
        )
        if first:  # a second token inherits the group's look: emoji, links, whale level, sell alerts
            con.execute(
                "UPDATE watches SET emoji=?, tg_url=?, x_url=?, discord_url=?, whale_usd=?, sell_alerts=? "
                "WHERE chat_id=? AND chain=? AND ca=?",
                (first["emoji"], first["tg_url"], first["x_url"], first["discord_url"], first["whale_usd"],
                 first["sell_alerts"], cid, chain, ca),
            )
        else:
            con.execute(
                "UPDATE watches SET whale_usd=?, sell_alerts=? WHERE chat_id=? AND chain=? AND ca=?",
                (whale or 0, int(bool(body.sell)), cid, chain, ca),
            )
        _audit(con, cid, uid, "track", f"{chain} {_short(ca)} min ${floor:g}")
        con.commit()
        st = _state(con, cid)
        st["tracking"] = True
        return {**st, "added": {"chain": chain, "name": hit["name"], "symbol": hit["symbol"]}}
    finally:
        con.close()
