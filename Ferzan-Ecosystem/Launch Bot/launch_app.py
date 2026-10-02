"""Ferzan Launch Mini App backend (Telegram Web App).

The page itself (miniapp/app.html) is static, like the signing pages. These routes give it
Telegram-authenticated data: initData is verified with the Launch bot token (HMAC-SHA256, per
core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app). Nothing here holds
keys; the creator's own wallet still signs every launch on evm.html / solana.html.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()
INIT_DATA_MAX_AGE_S = 24 * 3600
_rate: dict = {}
_rate_lock = threading.Lock()

# Same defaults the chat flow offers (launch_bot.py GRAD_PRESETS / MAXBUY_PRESETS).
GRAD_DEFAULT = {"bsc": "10", "arc": "10000", "default": "2.5"}
GRAD_PRESETS = {"bsc": ["5", "10", "20"], "arc": ["5000", "10000", "25000"], "default": ["1", "2.5", "5"]}
DEVBUY_PRESETS = {"solana": ["0.1", "0.5", "1", "2"], "bsc": ["0.01", "0.05", "0.1", "0.5"], "arc": ["10", "50", "100", "500"],
                  "default": ["0.001", "0.005", "0.01", "0.05"]}
MAXBUY_PCT = Decimal("0.02")  # Fair Launch Shield: no single buy above 2% of the graduation target
EVM_CHAINS = ("base", "bsc", "ethereum", "robinhood", "arc")
SOL_U64_MAX = 2**64 - 1


def _token() -> str:
    return (os.environ.get("LAUNCHBOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


def verify_init_data(init_data: str, token: str | None = None, now: float | None = None) -> dict:
    """Telegram user dict if initData is authentic and fresh, else ValueError. Pure (token/now injectable)."""
    token = token if token is not None else _token()
    if not token or not init_data:
        raise ValueError("missing init data")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    got = pairs.pop("hash", "")
    if not got:
        raise ValueError("no hash")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
        raise ValueError("bad signature")
    if (now or time.time()) - int(pairs.get("auth_date") or 0) > INIT_DATA_MAX_AGE_S:
        raise ValueError("expired")
    user = json.loads(pairs.get("user") or "{}")
    if not user.get("id"):
        raise ValueError("no user")
    return user


def _auth(init_data: str) -> dict:
    try:
        return verify_init_data(init_data)
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(401, "Open this from the Ferzan Launch bot in Telegram")


def _rate_ok(uid: int, limit: int = 10, window: int = 3600) -> bool:
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate.get(uid, []) if now - t < window]
        if len(hits) >= limit:
            _rate[uid] = hits
            return False
        hits.append(now)
        _rate[uid] = hits
    return True


class AppBody(BaseModel):
    initData: str = ""


class AppLaunchBody(AppBody):
    chain: str
    name: str
    symbol: str
    description: str = ""
    image: str = ""         # data:image/...;base64,...
    supply_whole: str = "1000000000"
    grad_native: str = ""   # EVM curves; blank = chat-flow default
    dev_buy: str = "0"
    mode: str = "curve"     # "curve" or "plain" (standard fixed-supply coin)
    allocs: str = ""        # team wallets, EVM only: "0xAddress:5" per line (percent of supply, 0.01 to 20 each)
    max_buy: str = ""       # custom per-buy cap (overrides the Shield default)
    start_minutes: str = "0"  # trading opens this many minutes after launch
    shield: bool = True     # Fair Launch Shield: cap any single buy
    website: str = ""
    x: str = ""
    telegram: str = ""


def _num(raw: str, label: str, lo: Decimal, hi: Decimal, allow_zero: bool = False) -> Decimal:
    try:
        v = Decimal(str(raw or "0").strip().replace(",", "") or "0")
    except InvalidOperation:
        raise HTTPException(400, f"{label} looks wrong")
    if v == 0 and allow_zero:
        return v
    if v < lo or v > hi:
        raise HTTPException(400, f"{label} must be between {lo:f} and {hi:f}")
    return v


@router.post("/api/app/me")
def app_me(body: AppBody):
    """The signed-in creator's launches with live progress, plus the chains open for new launches."""
    import api as A

    user = _auth(body.initData)
    uid = int(user["id"])
    rows = A.db.get_user_launch_history(uid, 40)
    c = A._idx_db()
    out, graduated, best = [], 0, 0.0
    try:
        for r in rows:
            item = {"id": r.id, "chain": r.chain, "mode": r.mode, "name": r.name, "symbol": r.symbol, "status": r.status,
                    "image": r.image_url or "", "token": r.result_token_address or "", "created_at": r.created_at,
                    "progress": None, "graduated": False, "mcap_usd": 0.0, "trades": 0, "url": "", "share": ""}
            if r.status == "confirmed" and r.result_token_address:
                item["share"] = f"{A._PUBLIC_ORIGIN}/api/share/{r.chain}/{r.result_token_address}"
            if r.status == "confirmed" and r.result_token_address and c is not None:
                cur = c.execute("SELECT * FROM curves WHERE chain = ? AND lower(token) = lower(?)",
                                (r.chain, r.result_token_address)).fetchone()
                if cur is not None:
                    g = int(cur["grad_target"] or 0)
                    item["graduated"] = bool(cur["graduated"])
                    item["progress"] = 100.0 if cur["graduated"] else (min(100.0, int(cur["real_eth"] or 0) * 100.0 / g) if g else 0.0)
                    item["mcap_usd"] = float((cur["mcap"] or 0) * A._native_usd(r.chain))
                    item["trades"] = int(cur["trades"] or 0)
                    item["url"] = A._trade_url(r.chain, cur["curve"], r.result_token_address)
                    graduated += 1 if cur["graduated"] else 0
                    best = max(best, item["mcap_usd"])
            out.append(item)
    finally:
        if c is not None:
            c.close()
    chains = A.chain_status()["chains"]
    return {"user": {"id": uid, "name": user.get("first_name") or ""}, "launches": out,
            "stats": {"launched": sum(1 for x in out if x["status"] == "confirmed"), "graduated": graduated, "best_mcap_usd": best},
            "chains": chains, "native": A._NATIVE_SYM, "grad_default": GRAD_DEFAULT, "grad_presets": GRAD_PRESETS, "dev_presets": DEVBUY_PRESETS, "base": A.MINI_APP_BASE, "origin": A._PUBLIC_ORIGIN,
            "bots": {"buy": (os.environ.get("FERZAN_BUY_BOT") or "Ferzan_Buy_Bot").lstrip("@"),
                     "guardian": (os.environ.get("FERZAN_GUARDIAN_BOT") or "Ferzan_Guardian_Bot").lstrip("@"),
                     "chat": os.environ.get("FERZAN_CHAT_URL") or "https://t.me/Ferzan_Chat"}}


@router.post("/api/app/launch")
def app_launch(body: AppLaunchBody):
    """Create a launch request for the signed-in user. The wallet is connected on the signing page,
    exactly like the chat flow, so announcements and milestone DMs reach this user."""
    import api as A

    user = _auth(body.initData)
    uid = int(user["id"])
    if not _rate_ok(uid):
        raise HTTPException(429, "Too many launches this hour. Try again later.")
    chain = (body.chain or "").strip().lower()
    name = (body.name or "").strip()
    symbol = (body.symbol or "").strip().upper()
    if not (1 <= len(name) <= 32) or not re.fullmatch(r"[A-Z0-9]{1,10}", symbol):
        raise HTTPException(400, "Name or ticker looks wrong")
    live = A.chain_status()["chains"].get(chain) or {}
    extra: dict = {"source": "miniapp", "allocs": ""}
    for k in ("website", "x", "telegram"):
        link = A._site_link(getattr(body, k), k)
        if link:
            extra[k] = link
    dev = _num(body.dev_buy, "Dev buy", Decimal("0.000001"), Decimal("100000"), allow_zero=True)
    extra["dev_buy"] = f"{dev:f}" if dev > 0 else "0"
    plain = (body.mode or "curve").strip().lower() == "plain"
    if chain == "solana":
        if plain:
            whole = int(_num(body.supply_whole, "Supply", Decimal(1), Decimal(10**12)))
            if whole * 10**6 > SOL_U64_MAX:
                raise HTTPException(400, f"Too big for Solana. Max supply is {SOL_U64_MAX // 10**6:,}")
            mode, decimals, total_supply = "plain", 6, str(whole * 10**6)
            extra["dev_buy"] = "0"
        else:
            if not (os.environ.get("METEORA_CONFIG") or "").strip():
                raise HTTPException(501, "Solana curves are not configured")
            mode, decimals, total_supply = "meteora", 6, str(10**9 * 10**6)
    elif chain in EVM_CHAINS:
        if not live.get("plain" if plain else "curve"):
            raise HTTPException(501, f"{'Standard tokens' if plain else 'Bonding curves'} on {chain} are not open yet")
        decimals = 18
        whole = int(_num(body.supply_whole, "Supply", Decimal(1), Decimal(10**15)))
        total_supply = str(whole * 10**18)
        if (body.allocs or "").strip():
            out, total = [], 0
            for part in re.split(r"[,;\n]+", body.allocs):
                part = part.strip()
                if not part:
                    continue
                m = re.fullmatch(r"(0x[0-9a-fA-F]{40})\s*[: ]\s*(\d+(?:\.\d+)?)\s*%?", part)
                if not m:
                    raise HTTPException(400, f"Couldn't read team wallet '{part[:44]}'. Use 0xAddress:5")
                bps = int(round(float(m.group(2)) * 100))
                if bps <= 0 or bps > 2000:
                    raise HTTPException(400, "Each team wallet can get between 0.01% and 20%")
                total += bps
                out.append(f"{m.group(1)}:{bps}")
            if total >= 10_000:
                raise HTTPException(400, "Team wallets can't take 100% of the supply")
            extra["allocs"] = ",".join(out)
        if plain:
            mode = "plain"
            extra.pop("dev_buy", None)
        else:
            mode = "bonding_curve"
            grad = _num(body.grad_native or GRAD_DEFAULT.get(chain, GRAD_DEFAULT["default"]), "Graduation",
                        Decimal("0.001"), Decimal(1_000_000))
            extra.update(graduation_eth_threshold=str(int(grad * 10**18)), graduation_display=f"{grad.normalize():f} {A._NATIVE_SYM.get(chain, '')}",
                         virtual_eth_reserve=str(10**18), virtual_token_reserve=str(int(total_supply) * 80 // 100),
                         )
            custom = _num(body.max_buy, "Max buy", Decimal("0.000001"), Decimal(1_000_000), allow_zero=True)
            cap = custom if custom > 0 else ((grad * MAXBUY_PCT).quantize(Decimal("0.0001")) if body.shield else Decimal(0))
            extra["max_buy"] = f"{max(cap, Decimal('0.000001')).normalize():f}" if cap > 0 else "0"
            mins = int(_num(body.start_minutes, "Trading opens", Decimal(0), Decimal(10080 - 10), allow_zero=True))
            if mins and dev > 0:
                raise HTTPException(400, "A dev buy needs trading to open right away")
            extra["start_minutes"] = str(mins)
    else:
        raise HTTPException(400, "Tron and TON launches run through your Trade Bot wallet. Continue in the chat.")
    ref = A.db.get_referrer(uid) if hasattr(A.db, "get_referrer") else None
    if ref:
        extra["referrer_id"] = str(ref)
    image_url = A._site_image(body.image)
    req = A.db.create_launch_request(
        telegram_user_id=uid, chat_id=uid, chain=chain, mode=mode, name=name, symbol=symbol, total_supply=total_supply,
        decimals=decimals, description=(body.description or "").strip()[:500], image_url=image_url, extra_params=extra)
    page = "solana.html" if chain == "solana" else "evm.html"
    return {"ok": True, "request_id": req.id, "sign_url": f"{A.MINI_APP_BASE}/{page}?request_id={req.id}",
            "shield": body.shield and mode == "bonding_curve", "max_buy": extra.get("max_buy", "0")}
