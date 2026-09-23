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
import evm_signer  # noqa: E402
import signer  # noqa: E402
import user_wallets  # noqa: E402
from chains import CHAINS  # noqa: E402

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("ferzan_webapp")

app = FastAPI(title="Ferzan Mini App", docs_url=None, redoc_url=None, openapi_url=None)

INIT_DATA_MAX_AGE_S = 24 * 3600
CACHE_TTL_S = 15
_cache: dict[int, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

# DexScreener chainId -> Ferzan chain id (EVM chains we can read balances on)
DS_TO_CID = {"base": "base", "ethereum": "eth", "bsc": "bsc", "arbitrum": "arb", "avalanche": "avax"}


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
def _ds_prices(mints: list[str]) -> dict[str, dict]:
    """Best pair (highest liquidity) per token from DexScreener, batched 30/call."""
    out: dict[str, dict] = {}
    for i in range(0, len(mints), 30):
        chunk = mints[i : i + 30]
        try:
            r = requests.get(
                "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk), timeout=10
            )
            pairs = (r.json() or {}).get("pairs") or []
        except Exception:
            continue
        for p in pairs:
            addr = ((p.get("baseToken") or {}).get("address") or "").strip()
            key = next((m for m in chunk if m.lower() == addr.lower()), None)
            if not key:
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            if key in out and out[key]["liq"] >= liq:
                continue
            out[key] = {
                "liq": liq,
                "price": float(p.get("priceUsd") or 0),
                "symbol": ((p.get("baseToken") or {}).get("symbol") or "?")[:16],
                "chain": p.get("chainId") or "",
                "chg24": float((p.get("priceChange") or {}).get("h24") or 0),
                "url": p.get("url") or "",
            }
    return out


def _erc20_amount(cid: str, token: str, owner: str) -> float:
    rpc = (CHAINS.get(cid) or {}).get("rpc")
    if not rpc or not owner:
        return 0.0
    raw = evm_signer._erc20_balance(rpc, token, owner)
    if raw <= 0:
        return 0.0
    try:
        dec_hex = evm_signer._rpc(rpc, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]).get("result")
        dec = int(dec_hex, 16) if dec_hex and dec_hex != "0x" else 18
    except Exception:
        dec = 18
    return raw / (10 ** min(max(dec, 0), 36))


def _price(coin: str) -> float | None:
    try:
        from price_fetcher import get_price_usd

        px = float(get_price_usd(coin) or 0)
        return px if px > 0 else None
    except Exception:
        return None


def build_portfolio(uid: int) -> dict:
    wallet = user_wallets.ensure(uid)
    sol_secret, _evm_secret = user_wallets.secrets(uid)
    slots = db.list_wallet_slots(uid)
    sol_pub, evm_pub = wallet.get("sol_pub", ""), wallet.get("evm_pub", "")

    sol_bal = signer.sol_balance_lamports(sol_pub) / 1e9 if sol_pub else 0.0
    try:
        eth_bal, _ = evm_signer.native_balance("base", evm_pub)
    except Exception:
        eth_bal = 0.0
    sol_px, eth_px = _price("solana"), _price("ethereum")

    try:
        sol_holds = signer.holdings(sol_secret)
    except Exception:
        sol_holds = []
    amounts = {h["mint"]: float(h.get("amount") or 0) for h in sol_holds[:25]}
    evm_mints = [m for m in db.live_mints(uid) if str(m).startswith("0x")][:10]
    marks = _ds_prices(list(amounts) + evm_mints)

    positions = []
    for mint in list(amounts) + evm_mints:
        m = marks.get(mint) or {}
        if mint.startswith("0x"):
            cid = DS_TO_CID.get(m.get("chain", ""), "")
            amt = _erc20_amount(cid, mint, evm_pub) if cid else 0.0
            chain = (cid or "evm").upper()
        else:
            amt = amounts.get(mint, 0.0)
            chain = "SOL"
        if amt <= 0:
            continue
        value = amt * float(m.get("price") or 0)
        cost = float(db.live_cost(uid, mint) or 0)
        pnl = (value - cost) if cost > 0 else None
        positions.append(
            {
                "mint": mint,
                "symbol": m.get("symbol") or mint[:4] + "…",
                "chain": chain,
                "amount": amt,
                "price": m.get("price") or 0,
                "value": value,
                "cost": cost,
                "pnl": pnl,
                "pnl_pct": (pnl / cost * 100) if (pnl is not None and cost > 0) else None,
                "chg24": m.get("chg24"),
                "chart": m.get("url") or "",
                "priced": bool(m.get("price")),
            }
        )
    positions.sort(key=lambda p: p["value"], reverse=True)

    native_usd = (sol_bal * sol_px if sol_px else 0) + (float(eth_bal) * eth_px if eth_px else 0)
    total = native_usd + sum(p["value"] for p in positions)
    costed = [p for p in positions if p["pnl"] is not None]
    return {
        "user_id": uid,
        "total_usd": total,
        "pnl_usd": sum(p["pnl"] for p in costed) if costed else None,
        "balances": {
            "sol": sol_bal,
            "sol_usd": sol_bal * sol_px if sol_px else None,
            "eth_base": float(eth_bal),
            "eth_usd": float(eth_bal) * eth_px if eth_px else None,
        },
        "wallets": [
            {"label": s["label"], "sol": s["sol_pub"], "evm": s["evm_pub"], "active": bool(s["active"])}
            for s in slots
        ],
        "positions": positions,
        "bot": _bot_username(),
        "ts": int(time.time()),
    }


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
async def portfolio(request: Request) -> JSONResponse:
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
    fresh = bool(body.get("refresh"))
    with _cache_lock:
        hit = _cache.get(uid)
    if hit and not fresh and time.time() - hit[0] < CACHE_TTL_S:
        return JSONResponse(hit[1])
    try:
        data = await asyncio.to_thread(build_portfolio, uid)
    except Exception:
        log.exception("portfolio build failed for %s", uid)
        raise HTTPException(status_code=502, detail="Couldn't load your desk right now — pull to retry.")
    with _cache_lock:
        _cache[uid] = (time.time(), data)
    return JSONResponse(data)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}
