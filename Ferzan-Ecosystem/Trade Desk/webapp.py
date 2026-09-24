"""Ferzan Mini App backend (Telegram Web App).

Serves the dashboard page and a read-only portfolio API. Every API call
must carry Telegram's signed initData; we verify it with the bot token
(HMAC-SHA256, per core.telegram.org/bots/webapps#validating-data-received-
via-the-mini-app) and apply the same allowlist as the bot. Responses hold
addresses, balances and positions only -- never keys. Trading stays in the
bot: the page's Sell button deep-links to the bot's sell pad, so every trade
still goes through the per-user lock, safety gates and confirmations.

Run (loopback; Caddy terminates HTTPS in front):
    uvicorn webapp:app --host 127.0.0.1 --port 8021
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

import db  # noqa: E402
import portfolio  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ferzan_webapp")

app = FastAPI(title="Ferzan Mini App", docs_url=None, redoc_url=None, openapi_url=None)
db.init_db()  # schema/migrations present even if this starts before the bot

INIT_DATA_MAX_AGE_S = 24 * 3600
CACHE_TTL_S = 15
_cache: dict[int, tuple[float, dict]] = {}
_cache_lock = threading.Lock()



# ---------------------------------------------------------------- auth ----
def _bot_token() -> str:
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def verify_init_data(init_data: str, token: str | None = None, now: float | None = None) -> dict:
    """Return the Telegram user dict if initData is authentic and fresh,
    else raise ValueError. Pure function (token/now injectable for tests)."""
    token = token if token is not None else _bot_token()
    if not token or not init_data:
        raise ValueError("missing init data")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    got = pairs.pop("hash", "")
    if not got:
        raise ValueError("no hash")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    want = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, got):
        raise ValueError("bad signature")
    auth_date = int(pairs.get("auth_date") or 0)
    if (now or time.time()) - auth_date > INIT_DATA_MAX_AGE_S:
        raise ValueError("expired")
    user = json.loads(pairs.get("user") or "{}")
    if not user.get("id"):
        raise ValueError("no user")
    return user


def _allowed(user_id: int) -> bool:
    """Same rule as bot.py _allowed(): FERZAN_PUBLIC or ALLOWED_USER_IDS."""
    if os.getenv("FERZAN_PUBLIC", "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    allow = {int(x.strip()) for x in raw.split(",") if x.strip()} if raw else set()
    return (not allow) or user_id in allow


# ----------------------------------------------------------- portfolio ----
def build_portfolio(uid: int) -> dict:
    data = portfolio.build_portfolio(uid)
    data["bot"] = _bot_username()
    data["presets"] = {"sol": db.buy_presets(uid, "sol"), "sell": db.sell_presets(uid)}
    data["multi"] = len(db.multi_buy_slots(uid))
    return data


_BOT_USERNAME = ""


def _bot_username() -> str:
    global _BOT_USERNAME
    if not _BOT_USERNAME:
        _BOT_USERNAME = (os.getenv("FERZAN_BOT_USERNAME") or "").lstrip("@")
    if not _BOT_USERNAME and _bot_token():
        try:
            r = requests.get(f"https://api.telegram.org/bot{_bot_token()}/getMe", timeout=10)
            _BOT_USERNAME = ((r.json() or {}).get("result") or {}).get("username") or ""
        except Exception:
            pass
    return _BOT_USERNAME


# -------------------------------------------------------------- routes ----
@app.get("/")
def index() -> FileResponse:
    return FileResponse(HERE / "webapp" / "index.html", headers={"Cache-Control": "no-cache"})


@app.post("/api/portfolio")
async def api_portfolio(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        user = verify_init_data(str(body.get("initData") or ""))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=f"Open this from the Ferzan bot ({exc}).")
    uid = int(user["id"])
    if not _allowed(uid):
        raise HTTPException(status_code=403, detail="This desk is locked to an allowlist.")
    fresh = bool(body.get("refresh")) and _rate_ok(uid, "refresh", 6)
    with _cache_lock:
        hit = _cache.get(uid)
    if hit and not fresh and time.time() - hit[0] < CACHE_TTL_S:
        return JSONResponse(hit[1])
    try:
        data = await asyncio.to_thread(build_portfolio, uid)
    except LookupError:
        raise HTTPException(status_code=404, detail="No wallet yet — open the bot and tap /start first.")
    except Exception:
        log.exception("portfolio build failed for %s", uid)
        raise HTTPException(status_code=502, detail="Couldn't load your desk right now — pull to retry.")
    with _cache_lock:
        _cache[uid] = (time.time(), data)
    return JSONResponse(data)


# ------------------------------------------------------------- trading ----
# The page never signs anything. An order is a row in webapp_orders; the BOT
# process (which holds the keys) claims it within ~2s, runs it through the
# same path as a button tap (per-user lock, rug/honeypot gates, size cap) and
# writes the result back. Orders older than 45s expire unexecuted.
ORDER_MAX_AGE_S = 3600  # trading needs initData from the last hour
MAX_ORDERS_PER_MIN = 10
_TOKEN_CACHE: dict[str, tuple[float, dict | None]] = {}
_HITS: dict[tuple[int, str], list[float]] = {}
_HITS_LOCK = threading.Lock()


def _rate_ok(uid: int, bucket: str, per_min: int) -> bool:
    """Per-user sliding-window limit: lookups here cost the bot's RPC quota."""
    now = time.time()
    with _HITS_LOCK:
        hits = [t for t in _HITS.get((uid, bucket), []) if now - t < 60]
        ok = len(hits) < per_min
        if ok:
            hits.append(now)
        _HITS[(uid, bucket)] = hits
        if len(_HITS) > 5000:  # prune idle users only; active limits survive
            for k in [k for k, v in _HITS.items() if not v or now - v[-1] >= 60]:
                _HITS.pop(k, None)
    return ok


def _auth(body: dict, max_age: int | None = None) -> int:
    try:
        user = verify_init_data(str(body.get("initData") or ""))
        if max_age is not None:
            pairs = dict(parse_qsl(str(body.get("initData") or "")))
            if time.time() - int(pairs.get("auth_date") or 0) > max_age:
                raise ValueError("session is over an hour old — close and reopen the app")
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=f"Open this from the Ferzan bot ({exc}).")
    uid = int(user["id"])
    if not _allowed(uid):
        raise HTTPException(status_code=403, detail="This desk is locked to an allowlist.")
    return uid


def _valid_mint(mint: str) -> bool:
    import re

    mint = (mint or "").strip()
    if re.fullmatch(r"0x[0-9a-fA-F]{40}", mint):
        return True
    if re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint):
        return True
    return bool(re.fullmatch(r"(EQ|UQ|kQ)[A-Za-z0-9_-]{46}", mint))


def token_info(mint: str) -> dict:
    now = time.time()
    hit = _TOKEN_CACHE.get(mint)
    if hit and now - hit[0] < (60 if hit[1] is None else 20):
        if hit[1] is None:
            raise LookupError("not found")
        return hit[1]
    marks = portfolio._ds_prices([mint])
    m = marks.get(mint) or {}
    if not m:
        _TOKEN_CACHE[mint] = (now, None)  # negative cache: no re-lookup spam
        raise LookupError("not found")
    cid = portfolio.DS_TO_CID.get(m.get("chain", ""), "")
    if m.get("chain") == "solana":
        cid = "sol"
    elif m.get("chain") == "ton":
        cid = "ton"
    safety = ""
    if cid == "sol":
        try:
            import rugcheck

            safety = rugcheck.security_line(rugcheck.sol_report(mint))
        except Exception:
            safety = ""
    info = {
        "mint": mint,
        "symbol": m.get("symbol") or "?",
        "name": m.get("name") or "",
        "chain": cid or m.get("chain") or "",
        "unit": {"sol": "SOL", "bsc": "BNB", "avax": "AVAX", "ton": "TON"}.get(cid, "ETH"),
        "price": m.get("price") or 0,
        "mc": m.get("mc") or 0,
        "liq": m.get("liq") or 0,
        "chg24": m.get("chg24"),
        "chart": m.get("url") or "",
        "safety": safety,
        "native_usd": portfolio._price({"sol": "solana", "bsc": "binancecoin", "avax": "avalanche-2",
                                        "ton": "the-open-network"}.get(cid, "ethereum")),
        "max_usd": _max_usd(),
    }
    _TOKEN_CACHE[mint] = (now, info)
    if len(_TOKEN_CACHE) > 2000:
        for k in sorted(_TOKEN_CACHE, key=lambda k: _TOKEN_CACHE[k][0])[:1000]:
            _TOKEN_CACHE.pop(k, None)
    return info


def _max_usd() -> float:
    import signer  # env-only read; no key is touched

    return signer.max_usd()


@app.post("/api/token")
async def api_token(request: Request) -> JSONResponse:
    body = await _json(request)
    uid = _auth(body)
    mint = str(body.get("mint") or "").strip()
    if not _valid_mint(mint):
        raise HTTPException(status_code=400, detail="That doesn't look like a token address.")
    if not _rate_ok(uid, "token", 20):
        raise HTTPException(status_code=429, detail="Too many lookups — give it a minute.")
    try:
        info = await asyncio.to_thread(token_info, mint)
    except LookupError:
        raise HTTPException(status_code=404, detail="No market found for that token yet.")
    except Exception:
        log.exception("token lookup failed")
        raise HTTPException(status_code=502, detail="Lookup failed — try again.")
    info["presets"] = db.buy_presets(uid, info["chain"] or "sol")
    return JSONResponse(info)


@app.post("/api/order")
async def api_order(request: Request) -> JSONResponse:
    body = await _json(request)
    uid = _auth(body, max_age=ORDER_MAX_AGE_S)
    side = str(body.get("side") or "")
    mint = str(body.get("mint") or "").strip()
    unit = str(body.get("unit") or "")
    chain = str(body.get("chain") or "")[:12]
    try:
        amount = float(body.get("amount"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Bad amount.")
    if side not in ("buy", "sell") or not _valid_mint(mint):
        raise HTTPException(status_code=400, detail="Bad order.")
    if side == "sell" and (unit != "pct" or not 1 <= amount <= 100):
        raise HTTPException(status_code=400, detail="Sell 1–100%.")
    if side == "buy" and (unit not in ("native", "usd") or not 0 < amount <= 1_000_000):
        raise HTTPException(status_code=400, detail="Bad buy size.")
    if db.open_webapp_orders(uid) > 0:
        raise HTTPException(status_code=409, detail="You already have a trade running — wait for it to land.")
    if db.recent_webapp_orders(uid, 60) >= MAX_ORDERS_PER_MIN:
        raise HTTPException(status_code=429, detail="Too many orders this minute — slow down.")
    multi = side == "buy" and bool(body.get("multi")) and bool(db.multi_buy_slots(uid))
    oid = db.add_webapp_order(uid, side, mint, chain, amount, unit, multi=multi)
    if not oid:
        raise HTTPException(status_code=409, detail="You already have a trade running — wait for it to land.")
    return JSONResponse({"id": oid, "status": "pending"})


@app.post("/api/order/status")
async def api_order_status(request: Request) -> JSONResponse:
    body = await _json(request)
    uid = _auth(body)
    try:
        oid = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Bad id.")
    row = db.get_webapp_order(oid, uid)
    if not row:
        raise HTTPException(status_code=404, detail="No such order.")
    with _cache_lock:
        if row["status"] in ("done", "failed"):
            _cache.pop(uid, None)  # next portfolio load is fresh
    return JSONResponse({"id": oid, "status": row["status"], "result": row.get("result") or ""})


# ------------------------------------------------ rules / wallets / cards --
# None of these move money directly: they save the same exit rules, DCA
# plans and limit buys the bot's commands save, and the bot's own jobs run
# them (with every safety check). The PnL card is queued for the bot too.
DCA_EVERY = {"hourly": 3600, "daily": 86400, "weekly": 604800}


def _num(v, lo: float, hi: float, what: str) -> float | None:
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{what} must be a number.")
    if not (lo <= x <= hi) or x != x:
        raise HTTPException(status_code=400, detail=f"{what} must be between {lo:g} and {hi:g}.")
    return x


def _rules_for(uid: int, mint: str) -> dict:
    ex = db.get_live_exit(uid, mint) or {}
    plan = db.get_dca_plan(uid, mint)
    every = None
    if plan:
        every = next((k for k, v in DCA_EVERY.items() if v == int(plan["interval_seconds"])), None)
    return {
        "mint": mint,
        "has_cost": db.live_cost(uid, mint) > 0,
        "exit": {"tp": ex.get("tp_pct"), "sl": ex.get("sl_pct"), "trail": ex.get("trail_pct")} if ex else None,
        "dca": {"usd": float(plan["usd_per_buy"]), "every": every,
                "next_in": max(0, int(plan["next_run_at"]) - int(time.time()))} if plan else None,
        "limits": [{"id": r["id"], "usd": float(r["usd"]), "target_px": float(r["target_px"])}
                   for r in db.armed_buy_limits(uid, mint)],
        "max_usd": _max_usd(),
    }


@app.post("/api/rules")
async def api_rules(request: Request) -> JSONResponse:
    body = await _json(request)
    uid = _auth(body)
    mint = str(body.get("mint") or "").strip()
    if not _valid_mint(mint):
        raise HTTPException(status_code=400, detail="Bad token address.")
    return JSONResponse(await asyncio.to_thread(_rules_for, uid, mint))


@app.post("/api/rules/save")
async def api_rules_save(request: Request) -> JSONResponse:
    body = await _json(request)
    uid = _auth(body, max_age=ORDER_MAX_AGE_S)
    if not _rate_ok(uid, "rules", 20):
        raise HTTPException(status_code=429, detail="Too many changes — give it a minute.")
    mint = str(body.get("mint") or "").strip()
    if not _valid_mint(mint):
        raise HTTPException(status_code=400, detail="Bad token address.")
    kind = str(body.get("kind") or "")
    cap = _max_usd()
    if kind == "exit":
        if db.live_cost(uid, mint) <= 0:
            raise HTTPException(status_code=400, detail="Exit rules need a bag bought through Ferzan (no cost basis here).")
        tp = _num(body.get("tp"), 1, 10000, "Take profit")
        sl = _num(body.get("sl"), 1, 99, "Stop loss")
        trail = _num(body.get("trail"), 1, 90, "Trailing stop")
        db.replace_live_exit(uid, mint, tp, sl, trail)
    elif kind == "dca":
        usd = _num(body.get("usd"), 1, cap, "DCA amount")
        every = str(body.get("every") or "")
        if usd is None or every not in DCA_EVERY:
            raise HTTPException(status_code=400, detail="Pick an amount and hourly / daily / weekly.")
        db.set_dca_plan(uid, mint, "base" if mint.startswith("0x") else "solana", usd, DCA_EVERY[every])
    elif kind == "dca_off":
        db.clear_dca_plan(uid, mint)
    elif kind == "limit":
        usd = _num(body.get("usd"), 1, cap, "Limit size")
        px = _num(body.get("target_px"), 1e-18, 1e9, "Target price")
        if usd is None or px is None:
            raise HTTPException(status_code=400, detail="Pick a target price and an amount.")
        if len(db.armed_buy_limits(uid)) >= db.MAX_BUY_LIMITS:
            raise HTTPException(status_code=400, detail=f"Limit reached ({db.MAX_BUY_LIMITS} open limit buys).")
        db.add_buy_limit(uid, mint, "bsc" if mint.startswith("0x") else "sol", usd, px)
    elif kind == "limit_cancel":
        try:
            lid = int(body.get("id"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Bad limit id.")
        db.cancel_buy_limit(uid, lid)
    else:
        raise HTTPException(status_code=400, detail="Unknown change.")
    return JSONResponse(await asyncio.to_thread(_rules_for, uid, mint))


@app.post("/api/wallet")
async def api_wallet(request: Request) -> JSONResponse:
    body = await _json(request)
    uid = _auth(body, max_age=ORDER_MAX_AGE_S)
    try:
        slot_id = int(body.get("id"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Bad wallet.")
    if db.open_webapp_orders(uid) > 0:
        raise HTTPException(status_code=409, detail="A trade is running — switch wallets after it lands.")
    if not db.set_active_wallet(uid, slot_id):
        raise HTTPException(status_code=404, detail="No such wallet.")
    with _cache_lock:
        _cache.pop(uid, None)
    return JSONResponse({"ok": True})


@app.post("/api/card")
async def api_card(request: Request) -> JSONResponse:
    body = await _json(request)
    uid = _auth(body)
    mint = str(body.get("mint") or "").strip()
    if not _valid_mint(mint):
        raise HTTPException(status_code=400, detail="Bad token address.")
    if db.live_cost(uid, mint) <= 0:
        raise HTTPException(status_code=400, detail="PnL cards are for bags bought through Ferzan.")
    if not db.add_card_request(uid, mint):
        raise HTTPException(status_code=429, detail="A card is already on its way — check the chat.")
    return JSONResponse({"queued": True})


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
