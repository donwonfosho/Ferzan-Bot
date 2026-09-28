"""
api.py

The backend the Mini App talks to. Three jobs:
  1. Hand back launch request details (GET) so the Mini App knows what
     it's launching before asking the user to connect a wallet.
  2. Build the actual unsigned transaction for the connected wallet
     address (POST .../build-tx) -- this is where evm_launch.py /
     solana_launch.py / meteora_launch.py actually get called.
  3. Record the result once the user's wallet has signed and broadcast
     it (POST .../complete), and notify them back in Telegram chat.

Also serves token metadata JSON (GET /metadata/{id}) -- a simple
self-hosted alternative to external IPFS/Arweave pinning for a first
version. Centralized, but one fewer external dependency to get working.

Run with: uvicorn api:app --host 0.0.0.0 --port 8000
(nginx sits in front of this with HTTPS -- see DEPLOYMENT notes;
Telegram Mini Apps require the page to be served over HTTPS.)
"""

import os
import logging
import time
import html as _html
import re as _re
from pathlib import Path as _Path
from fastapi.responses import FileResponse
from decimal import Decimal, InvalidOperation

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import launch_bot_db as db
from evm_launch import (
    EvmLaunchTxBuilder,
    EvmBondingCurveTxBuilder,
    CHAIN_CONFIGS,
    parse_allocs,
    parse_native_amount,
)
from solana_launch import build_unsigned_launch_tx as build_solana_plain_tx
from meteora_launch import build_unsigned_meteora_tx
import tron_launch as _tron
from ton_launch import build_unsigned_launch_tx as build_ton_launch_tx, verify_launch as verify_ton_launch

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("LAUNCHBOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
MINI_APP_BASE = (os.environ.get("MINI_APP_BASE_URL") or "https://launch.ferzaneco.com/miniapp").rstrip("/")
_PUBLIC_ORIGIN = (_re.match(r"(https?://[^/]+)", MINI_APP_BASE) or _re.match(r"(.*)", "https://launch.ferzaneco.com")).group(1)
_MEDIA_DIR = _Path(__file__).resolve().with_name("media")

# Deployed contract addresses, per chain -- fill these in after you
# deploy LaunchTokenFactory.sol / BondingCurveFactory.sol per Part 2 of
# the Solidity setup in the project README. Each EVM chain gets its own
# deployment, so its own address.
def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


FACTORY_ADDRESSES = {
    "ethereum": {
        "plain": _env("FACTORY_ETH_PLAIN"),
        "bonding_curve": _env("FACTORY_ETH_CURVE"),
    },
    "bsc": {
        "plain": _env("FACTORY_BSC_PLAIN"),
        "bonding_curve": _env("FACTORY_BSC_CURVE"),
    },
    "base": {
        "plain": _env("FACTORY_BASE_PLAIN"),
        "bonding_curve": _env("FACTORY_BASE_CURVE"),
    },
    "robinhood": {
        "plain": _env("FACTORY_HOOD_PLAIN"),
        "bonding_curve": _env("FACTORY_HOOD_CURVE"),
    },
    "arc": {
        "plain": _env("FACTORY_ARC_PLAIN"),
        "bonding_curve": _env("FACTORY_ARC_CURVE"),
    },
}

RPC_URLS = {
    "ethereum": os.environ.get("ETHEREUM_RPC_URL", ""),
    "bsc": os.environ.get("BSC_RPC_URL", ""),
    "base": os.environ.get("BASE_RPC_URL", ""),
    "robinhood": os.environ.get("ROBINHOOD_RPC_URL", ""),
    "arc": os.environ.get("ARC_RPC_URL", "https://rpc.mainnet.arc.io"),
    "solana": os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com"),
    "tron": os.environ.get("TRONGRID_URL", "https://api.trongrid.io"),
    "ton": os.environ.get("TON_RPC_URL", ""),
}

PLATFORM_TREASURY_EVM = os.environ.get("PLATFORM_TREASURY_EVM", "")

app = FastAPI(title="Launch Bot API")

# Mini App runs in Telegram's in-app browser -- CORS needs to allow that
# origin. Tighten this to your actual Mini App domain once deployed
# rather than leaving it wide open.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class BuildTxRequest(BaseModel):
    wallet_address: str


class CompleteRequest(BaseModel):
    tx_hash: str
    result_token_address: str = ""
    curve_address: str = ""


def _internal_ok(request) -> bool:
    expected = (os.environ.get("INTERNAL_API_TOKEN") or "").strip()
    got = (request.headers.get("x-ferzan-internal") or "").strip()
    if expected and got == expected:
        return True
    if not expected:
        # Local droplet default: allow loopback only.
        client = (request.client.host if request.client else "") or ""
        return client in {"127.0.0.1", "::1"}
    return False


@app.get("/internal/referrer-wallet/{user_id}")
def internal_referrer_wallet(user_id: int, request: Request):
    if not _internal_ok(request):
        raise HTTPException(403, "internal only")
    wallet = db.get_referrer_wallet(int(user_id)) or ""
    referrer_id = db.get_referrer(int(user_id))
    return {"user_id": int(user_id), "wallet": wallet, "referrer_id": referrer_id}


@app.get("/internal/curve-for-token/{token}")
def internal_curve_for_token(token: str, request: Request):
    if not _internal_ok(request):
        raise HTTPException(403, "internal only")
    curve = db.get_curve_for_token(token) or ""
    return {"token": token, "curve": curve}


@app.get("/api/launch-requests/{request_id}")
def get_request(request_id: str):
    req = db.get_launch_request(request_id)
    if not req:
        raise HTTPException(404, "Launch request not found")
    return req


@app.get("/api/metadata/{request_id}")
def get_metadata(request_id: str):
    """Self-hosted token metadata JSON, in the shape most wallets/explorers expect."""
    req = db.get_launch_request(request_id)
    if not req:
        raise HTTPException(404, "Launch request not found")
    return {
        "name": req.name,
        "symbol": req.symbol,
        "description": req.description or "",
        "image": req.image_url or "",
        "external_url": (req.extra_params or {}).get("website", ""),
        "extensions": {k: v for k, v in (req.extra_params or {}).items() if k in ("website", "x", "telegram") and v},
    }


@app.get("/api/media/{name}")
def get_media(name: str):
    """Token logos saved by the bot (random file names, images only)."""
    m = _re.fullmatch(r"[0-9a-f]{32}\.(jpg|png|webp|gif)", name or "")
    p = _MEDIA_DIR / name if m else None
    if not p or not p.is_file():
        raise HTTPException(404, "not found")
    mt = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp", "gif": "image/gif"}[m.group(1)]
    return FileResponse(p, media_type=mt, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/curve-info/{curve}")
def _curve_info(curve: str):
    """Public token info for the trade page: name, logo, links (no private data)."""
    if not _re.fullmatch(r"0x[0-9a-fA-F]{40}", curve or ""):
        raise HTTPException(404, "not found")
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM launch_requests WHERE extra_params LIKE ? ORDER BY created_at DESC LIMIT 20",
            (f"%{curve[2:]}%",),
        ).fetchall()
    for row in rows:
        req = db.get_launch_request(row[0])
        extra = (req.extra_params or {}) if req else {}
        if req and str(extra.get("curve_address") or "").lower() == curve.lower():
            return {
                "name": req.name, "symbol": req.symbol, "chain": req.chain,
                "token": req.result_token_address or "", "image": req.image_url or "",
                "description": req.description or "",
                "website": extra.get("website", ""), "x": extra.get("x", ""), "telegram": extra.get("telegram", ""),
            }
    raise HTTPException(404, "not found")


@app.post("/api/launch-requests/{request_id}/build-tx")
def build_tx(request_id: str, body: BuildTxRequest):
    req = db.get_launch_request(request_id)
    if not req:
        raise HTTPException(404, "Launch request not found")
    if req.status not in ("pending", "built", "failed"):
        raise HTTPException(400, f"Request is already {req.status}, cannot rebuild")
    _site_wallet = str((req.extra_params or {}).get("site_wallet") or "")
    if _site_wallet and _site_wallet.lower() != (body.wallet_address or "").strip().lower():
        raise HTTPException(400, "This launch belongs to another wallet")

    total_supply = int(req.total_supply)

    try:
        if req.chain == "solana":
            if req.mode == "plain":
                from irys_upload import upload_token_metadata
                metadata_uri = ""
                try:
                    metadata_uri = upload_token_metadata(
                        name=req.name, symbol=req.symbol,
                        image_url=req.image_url, description=req.description or "",
                    )
                except Exception as e:
                    import traceback
                    print(f"IRYS_METADATA_UPLOAD_FAILED: {e}")
                    traceback.print_exc()
                if not metadata_uri:
                    metadata_uri = f"{_PUBLIC_ORIGIN}/api/metadata/{request_id}"
                result = build_solana_plain_tx(
                    creator_pubkey=body.wallet_address,
                    decimals=req.decimals,
                    initial_supply_raw=total_supply,
                    rpc_url=RPC_URLS["solana"],
                    name=req.name,
                    symbol=req.symbol,
                    metadata_uri=metadata_uri,
                )
                unsigned_tx_hex = bytes(result.unsigned_transaction).hex()
                response = {"chain": "solana", "unsigned_transaction": unsigned_tx_hex, "mint_address": result.mint_address}
            elif req.mode in ("meteora", "pumpfun", "bonding_curve"):
                from irys_upload import upload_token_metadata
                metadata_uri = ""
                try:
                    metadata_uri = upload_token_metadata(
                        name=req.name, symbol=req.symbol,
                        image_url=req.image_url, description=req.description or "",
                    )
                except Exception as e:
                    import traceback
                    print(f"IRYS_METADATA_UPLOAD_FAILED: {e}")
                    traceback.print_exc()
                if not metadata_uri:
                    metadata_uri = f"{_PUBLIC_ORIGIN}/api/metadata/{request_id}"
                result = build_unsigned_meteora_tx(
                    creator_pubkey=body.wallet_address,
                    decimals=req.decimals,
                    initial_supply_raw=total_supply,
                    rpc_url=RPC_URLS["solana"],
                    graduation_sol_lamports=int(req.extra_params.get("graduation_eth_threshold") or 0),
                    dev_buy=req.extra_params.get("dev_buy"),
                    name=req.name,
                    symbol=req.symbol,
                    metadata_uri=metadata_uri,
                )
                unsigned_tx_hex = bytes(result.unsigned_transaction).hex()
                response = {
                    "chain": "solana",
                    "mode": "meteora",
                    "unsigned_transaction": unsigned_tx_hex,
                    "mint_address": result.mint_address,
                    "program_id": result.program_id,
                    "note": result.note,
                    "cost_text": getattr(result, "cost_text", ""),
                }
            else:
                raise HTTPException(400, f"Unknown Solana mode: {req.mode}")

        elif req.chain == "tron":
            if (req.extra_params or {}).get("source") != "site":
                raise HTTPException(400, "Tron coins launch from your Ferzan Trade Bot wallet, right in the Launch Bot chat.")
            try:
                tx = _tron.build_site_launch(body.wallet_address, req.name, req.symbol, total_supply)
            except ValueError as e:
                raise HTTPException(400, str(e))
            response = {"chain": "tron", "transaction": tx["transaction"], "fee_sun": tx["fee_sun"],
                        "factory": _tron.factory(), "note": "Sign in TronLink. About 16 TRX of energy + the launch fee."}

        elif req.chain == "ton":
            if (os.environ.get("TON_LAUNCH_LIVE") or "").strip() != "1":
                raise HTTPException(501, "TON launches are not open yet.")
            extra = dict(req.extra_params or {})
            meta = extra.get("ton_meta") or ""
            if not meta:  # upload once: the coin's address depends on it, so a retry must reuse it
                try:
                    from irys_upload import upload_token_metadata
                    meta = upload_token_metadata(name=req.name, symbol=req.symbol, image_url=req.image_url or "",
                                                 description=req.description or "")
                except Exception as e:
                    print(f"IRYS_METADATA_UPLOAD_FAILED ton: {e}")
                meta = meta or f"{_PUBLIC_ORIGIN}/api/metadata/{request_id}"
            result = build_ton_launch_tx(request_id, body.wallet_address, total_supply, meta)
            extra.update(ton_meta=meta, ton_minter=result.minter)
            _set_extra(request_id, extra)
            response = {
                "chain": "ton",
                "minter": result.minter,
                "messages": result.messages,
                "valid_until": result.valid_until,
                "network": result.network,
                "note": result.note,
            }

        elif req.chain in CHAIN_CONFIGS:
            if req.chain == "arc" and req.mode == "bonding_curve" and not FACTORY_ADDRESSES["arc"]["bonding_curve"]:
                raise HTTPException(501, "Arc bonding curves open once the Arc curve factory is deployed.")
            if req.chain == "arc" and (os.environ.get("ARC_LAUNCH_LIVE") or "").strip() != "1":
                raise HTTPException(501, "Arc launches open once the Arc launch factory is deployed (ARC_LAUNCH_LIVE=1).")
            rpc = RPC_URLS.get(req.chain) or None
            if req.mode == "plain":
                factory_addr = FACTORY_ADDRESSES[req.chain]["plain"]
                if not factory_addr:
                    raise HTTPException(500, f"No plain-launch factory address configured for {req.chain}")
                builder = EvmLaunchTxBuilder(req.chain, factory_addr, rpc_url=rpc)
                aw, ab = parse_allocs((req.extra_params or {}).get("allocs") or "")
                tx = builder.build_unsigned_launch_tx(
                    creator_address=body.wallet_address,
                    name=req.name,
                    symbol=req.symbol,
                    total_supply=total_supply,
                    decimals=req.decimals,
                    project_url=req.extra_params.get("project_url", ""),
                    alloc_wallets=aw,
                    alloc_bps=ab,
                )
            elif req.mode == "bonding_curve":
                factory_addr = FACTORY_ADDRESSES[req.chain]["bonding_curve"]
                if not factory_addr:
                    raise HTTPException(501, f"Bonding curves on {req.chain} are coming soon")
                from evm_launch import FerzanCurveTxBuilder
                extra = req.extra_params or {}
                aw, ab = parse_allocs(extra.get("allocs") or "")
                mins = int(float(str(extra.get("start_minutes") or "0") or 0))
                start_time = int(time.time()) + mins * 60 if mins > 0 else 0
                start_at = int(float(str(extra.get("start_at") or "0") or 0))
                if start_at:
                    start_time = start_at if start_at > time.time() + 30 else 0
                tx = FerzanCurveTxBuilder(req.chain, factory_addr, rpc_url=rpc).build(
                    creator_address=body.wallet_address,
                    name=req.name,
                    symbol=req.symbol,
                    total_supply=total_supply,
                    grad_target_wei=int(extra.get("graduation_eth_threshold") or 0),
                    start_time=start_time,
                    max_buy_wei=_wei(extra.get("max_buy")),
                    dev_buy_wei=_wei(extra.get("dev_buy")),
                    alloc_wallets=aw,
                    alloc_bps=ab,
                )
            else:
                raise HTTPException(400, f"Unknown EVM mode: {req.mode}")

            response = {
                "chain": req.chain,
                "unsigned_transaction": tx,
                "rpc_url": rpc or RPC_URLS.get(req.chain) or "",
            }

        else:
            raise HTTPException(400, f"Unsupported chain: {req.chain}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"build-tx failed for {request_id}: {e}", exc_info=e)
        db.update_status(request_id, "failed", error_message=str(e))
        raise HTTPException(500, f"Failed to build transaction: {e}")

    db.update_status(request_id, "built", wallet_address=body.wallet_address)
    return response


def _set_extra(request_id: str, extra: dict) -> None:
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    with db._get_conn() as conn:
        conn.execute("UPDATE launch_requests SET extra_params = ?, updated_at = ? WHERE id = ?",
                     (_json.dumps(extra), _dt.now(_tz.utc).isoformat(), request_id))


def _verifiable_launch(req) -> bool:
    """VERIFY_ALL_BATCH21: which launches must be proven on chain before they are posted.
    Website launches always; Telegram launches when they are a Ferzan curve (EVM factory) or a
    Ferzan Meteora pool, the two kinds _verify_site_launch understands. Other modes are unchanged."""
    if (req.extra_params or {}).get("source") == "site":
        return True
    if not (req.wallet_address or "").strip():
        logger.warning("launch %s has no wallet yet; skipping the on-chain check", req.id)
        return False
    if req.chain == "solana":
        return req.mode == "meteora"
    return req.mode == "bonding_curve" and bool(FACTORY_ADDRESSES.get(req.chain, {}).get("bonding_curve"))


@app.post("/api/launch-requests/{request_id}/complete")
def complete_request(request_id: str, body: CompleteRequest):
    req = db.get_launch_request(request_id)
    if not req:
        raise HTTPException(404, "Launch request not found")
    if req.status == "confirmed":  # a replayed call must not post to the channel again
        return {"status": "ok", "already": True, "token": req.result_token_address or "",
                "curve": str((req.extra_params or {}).get("curve_address") or "")}
    if req.chain == "ton":
        minter = str((req.extra_params or {}).get("ton_minter") or "")
        if not minter:
            raise HTTPException(400, "This TON launch was never built.")
        res = verify_ton_launch(minter, int(req.total_supply))
        if not res.get("ok"):
            raise HTTPException(400, "TON has not confirmed the coin yet. Wait a minute and check your wallet; "
                                     "do not launch again. (" + str(res.get("error", ""))[:120] + ")")
        body = CompleteRequest(tx_hash=body.tx_hash, result_token_address=minter, curve_address="")
    elif req.chain == "tron":  # TRON_WALLET_LAUNCH: re-checked on-chain here, never taken from the caller
        creator = (req.wallet_address or "").strip()
        if req.mode == "bonding_curve":
            res = _tron.verify_curve_launch(body.tx_hash, creator)
        else:
            res = _tron.verify_launch(body.tx_hash, creator, int(req.total_supply))
        if not res.get("ok"):
            raise HTTPException(400, "Tron has not confirmed this launch: " + str(res.get("error", ""))[:120])
        body = CompleteRequest(tx_hash=body.tx_hash, result_token_address=res["token"], curve_address=res.get("curve", ""))
    elif _verifiable_launch(req):
        verified = _verify_site_launch(req, body)
        body = CompleteRequest(tx_hash=body.tx_hash, result_token_address=verified.get("token", ""),
                               curve_address=verified.get("curve", ""))

    token_addr = (body.result_token_address or "").strip()
    curve_addr = (body.curve_address or "").strip()
    if body.tx_hash and (not token_addr or not curve_addr) and req.chain != "tron":
        parsed = _parse_launch_receipt(req.chain, body.tx_hash)
        token_addr = token_addr or parsed.get("token") or ""
        curve_addr = curve_addr or parsed.get("curve") or ""

    db.update_status(
        request_id, "confirmed", tx_hash=body.tx_hash, result_token_address=token_addr
    )
    if curve_addr:
        try:
            db.set_curve_address(request_id, curve_addr)
        except Exception as exc:
            logger.warning("set_curve_address failed %s: %s", request_id, exc)
    if req.wallet_address and req.telegram_user_id and req.chain not in ("tron", "ton"):  # payouts go to EVM/Solana wallets
        try:
            db.set_payout_wallet(req.telegram_user_id, req.wallet_address)
        except Exception as exc:
            logger.warning("set_payout_wallet failed user=%s: %s", req.telegram_user_id, exc)

    text = _launch_card(req, token_addr, curve_addr, body.tx_hash)
    _notify_telegram(req.chat_id, text, photo=req.image_url or "", markup=_growth_buttons(req, token_addr, curve_addr))
    channel = (os.environ.get("FERZAN_LAUNCHES_CHANNEL") or "").strip()
    if channel:
        _notify_telegram(channel, text, photo=req.image_url or "",
                         markup=_growth_buttons(req, token_addr, curve_addr, trade_only=True))
    try:  # people who follow this creator get a DM (Launch Bot /start follow_<wallet>)
        _notify_followers(req, token_addr, curve_addr or "")
    except Exception as exc:
        logger.warning("follower alerts failed %s: %s", request_id, exc)
    return {"status": "ok", "token": token_addr, "curve": curve_addr}


# ---- SITE_LAUNCH_BATCH16: launches from ferzan-factory.grok.me (no Telegram account needed) ----
import base64 as _b64
import hmac as _hmac
import uuid as _uuid

_SITE_CHAINS = {"ethereum", "bsc", "base", "robinhood", "solana", "arc", "tron", "ton"}
_IMG_MAGIC = {"png": b"\x89PNG", "jpg": b"\xff\xd8\xff", "gif": b"GIF8", "webp": b"RIFF"}


def _site_secret_ok(request: Request) -> bool:
    expected = (os.environ.get("FERZAN_INGEST_SECRET") or "").strip()
    got = (request.headers.get("x-ferzan-ingest") or "").strip()
    return len(expected) >= 24 and _hmac.compare_digest(expected.encode(), got.encode())


class SiteLaunchBody(BaseModel):
    chain: str
    name: str
    symbol: str
    wallet_address: str
    description: str = ""
    image: str = ""            # data:image/...;base64,... (the site stores images inline)
    supply_whole: str = "1000000000"
    grad_native: str = ""      # EVM only: native raised before graduation, e.g. "5"
    dev_buy: str = "0"
    max_buy: str = "0"
    start_minutes: str = "0"
    website: str = ""
    x: str = ""
    telegram: str = ""
    validate_only: bool = False


def _site_num(raw: str, label: str, lo: Decimal, hi: Decimal, allow_zero: bool = False) -> Decimal:
    try:
        v = Decimal(str(raw or "0").strip().replace(",", "") or "0")
    except InvalidOperation:
        raise HTTPException(400, f"{label} looks wrong")
    if allow_zero and v == 0:
        return v
    if not v.is_finite() or v < lo or v > hi:
        raise HTTPException(400, f"{label} must be between {lo} and {hi}")
    return v


def _site_link(raw: str, kind: str) -> str:
    v = (raw or "").strip()[:200]
    if not v:
        return ""
    if kind in ("x", "telegram") and _re.fullmatch(r"@?[A-Za-z0-9_]{3,32}", v):
        host = "https://x.com/" if kind == "x" else "https://t.me/"
        return host + v.lstrip("@")
    if not _re.fullmatch(r"https://[^\s<>\"']{3,190}", v):
        raise HTTPException(400, f"{kind} link must start with https://")
    return v


def _site_image(data_url: str) -> str:
    if not data_url:
        return ""
    m = _re.fullmatch(r"data:image/(png|jpeg|jpg|webp|gif);base64,([A-Za-z0-9+/=\s]+)", data_url.strip())
    if not m:
        raise HTTPException(400, "Image must be a PNG, JPEG, WebP or GIF")
    ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
    try:
        raw = _b64.b64decode(m.group(2), validate=False)
    except Exception:
        raise HTTPException(400, "Image looks wrong")
    if len(raw) > 300_000 or not raw.startswith(_IMG_MAGIC[ext]):
        raise HTTPException(400, "Image is too big or not a real image")
    import hashlib as _hashlib
    import shutil as _shutil
    _MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{_hashlib.sha256(raw).hexdigest()[:32]}.{ext}"
    target = _MEDIA_DIR / name
    if not target.exists():
        if _shutil.disk_usage(_MEDIA_DIR).free < 1_000_000_000:
            logger.warning("media disk nearly full; launch saved without its picture")
            return ""
        target.write_bytes(raw)
    return f"{_PUBLIC_ORIGIN}/api/media/{name}"


@app.post("/api/site/launch-requests")
def site_launch(body: SiteLaunchBody, request: Request):
    """Called by the website (SITE_OPEN_BATCH19: no shared key). Creates a launch request with
    no Telegram user; the visitor's own wallet then signs the tx from /build-tx. Nothing is posted
    anywhere until /complete has verified the launch on chain."""
    chain = (body.chain or "").strip().lower()
    if chain not in _SITE_CHAINS:
        raise HTTPException(400, "That chain is not open for website launches")
    name = (body.name or "").strip()
    symbol = (body.symbol or "").strip().upper()
    if not (1 <= len(name) <= 32) or not _re.fullmatch(r"[A-Z0-9]{1,10}", symbol):
        raise HTTPException(400, "Name or ticker looks wrong")
    wallet = (body.wallet_address or "").strip()
    if chain == "solana":
        if not _re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", wallet):
            raise HTTPException(400, "Solana wallet looks wrong")
    elif chain == "tron":
        if not _re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", wallet):
            raise HTTPException(400, "Tron wallet looks wrong")
        if not _tron.live():
            raise HTTPException(501, "Tron launches are not open yet")
    elif chain == "ton":
        if not _re.fullmatch(r"(0|-1):[0-9a-fA-F]{64}|[A-Za-z0-9_-]{48}", wallet):
            raise HTTPException(400, "TON wallet looks wrong")
        if (os.environ.get("TON_LAUNCH_LIVE") or "").strip() != "1":
            raise HTTPException(501, "TON launches are not open yet")
    elif not _re.fullmatch(r"0x[0-9a-fA-F]{40}", wallet):
        raise HTTPException(400, "Wallet looks wrong")
    dev = _site_num(body.dev_buy, "Dev buy", Decimal("0.000001"), Decimal("100000"), allow_zero=True)
    extra = {"source": "site", "site_wallet": wallet, "dev_buy": f"{dev:f}" if dev > 0 else "0", "allocs": ""}
    for k in ("website", "x", "telegram"):
        link = _site_link(getattr(body, k), k)
        if link:
            extra[k] = link
    if chain in ("tron", "ton"):  # SITE_TRON_TON: standard fixed-supply coins, signed by TronLink / TON Connect
        decimals = 6 if chain == "tron" else 9
        whole = int(_site_num(body.supply_whole, "Supply", Decimal(1), Decimal(10**12)))
        mode, total_supply = "plain", str(whole * 10**decimals)
        extra["dev_buy"] = "0"
    elif chain == "solana":
        mode, decimals, total_supply = "meteora", 6, str(10**9 * 10**6)  # fixed by the Ferzan Meteora config
        if not (os.environ.get("METEORA_CONFIG") or "").strip():
            raise HTTPException(501, "Solana curves are not configured")
    else:
        mode, decimals = "bonding_curve", 18
        if not FACTORY_ADDRESSES.get(chain, {}).get("bonding_curve"):
            raise HTTPException(501, f"Bonding curves on {chain} are not configured")
        whole = int(_site_num(body.supply_whole, "Supply", Decimal(1), Decimal(10**15)))
        total_supply = str(whole * 10**18)
        grad = _site_num(body.grad_native, "Graduation", Decimal("0.001"), Decimal(1_000_000))
        extra["graduation_eth_threshold"] = str(int(grad * 10**18))
        extra["graduation_display"] = f"{grad.normalize():f}"
        extra["virtual_eth_reserve"] = str(10**18)
        extra["virtual_token_reserve"] = str(int(total_supply) * 80 // 100)
        mb = _site_num(body.max_buy, "Max buy", Decimal("0.000001"), Decimal(1_000_000), allow_zero=True)
        extra["max_buy"] = f"{mb:f}" if mb > 0 else "0"
        mins = int(_site_num(body.start_minutes, "Start delay", Decimal(0), Decimal(10080), allow_zero=True))
        if mins and dev > 0:
            raise HTTPException(400, "A dev buy needs trading to open right away")
        extra["start_minutes"] = str(mins)
    description = (body.description or "").strip()[:500]
    if body.validate_only:
        return {"ok": True, "validated": True, "chain": chain, "mode": mode}
    image_url = _site_image(body.image)
    req = db.create_launch_request(
        telegram_user_id=0, chat_id=0, chain=chain, mode=mode, name=name, symbol=symbol,
        total_supply=total_supply, decimals=decimals, description=description,
        image_url=image_url, extra_params=extra,
    )
    logger.info("site launch request %s chain=%s", req.id, chain)
    return {"ok": True, "request_id": req.id, "image_url": image_url}


def _verify_site_launch(req, body) -> dict:
    """The launch tx must be confirmed on chain, sent by the request's wallet, through our factory
    (EVM) or creating the given mint (Solana). Returns the real token/curve."""
    wallet = str((req.extra_params or {}).get("site_wallet") or req.wallet_address or "").strip()
    tx = (body.tx_hash or "").strip()
    if req.chain == "solana":
        mint = (body.result_token_address or "").strip()
        if not _re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", mint) or not _re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{64,90}", tx):
            raise HTTPException(400, "Launch signature or mint looks wrong")
        res = {}
        for _ in range(10):
            try:
                res = requests.post(RPC_URLS["solana"], json={"jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                    "params": [tx, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]},
                    timeout=20).json().get("result") or {}
            except Exception:
                res = {}
            if res:
                break
            time.sleep(2)
        if not res:
            raise HTTPException(400, "The launch is not confirmed on Solana")
        if (res.get("meta") or {}).get("err") is not None:
            raise HTTPException(400, "The launch transaction failed on Solana")
        keys = [k.get("pubkey") if isinstance(k, dict) else k for k in (res.get("transaction") or {}).get("message", {}).get("accountKeys") or []]
        if not keys or keys[0] != wallet or mint not in keys:
            raise HTTPException(400, "That launch was not made by this wallet")
        return {"token": mint}
    if not _re.fullmatch(r"0x[0-9a-fA-F]{64}", tx):
        raise HTTPException(400, "Launch transaction looks wrong")
    factory = (FACTORY_ADDRESSES.get(req.chain, {}).get("bonding_curve") or "").lower()
    rcpt = {}
    for _ in range(12):
        try:
            rcpt = requests.post(RPC_URLS.get(req.chain) or "", json={"jsonrpc": "2.0", "id": 1,
                "method": "eth_getTransactionReceipt", "params": [tx]}, timeout=20).json().get("result") or {}
        except Exception:
            rcpt = {}
        if rcpt:
            break
        time.sleep(2)
    if not rcpt or str(rcpt.get("status")) != "0x1":
        raise HTTPException(400, "The launch is not confirmed on chain")
    if str(rcpt.get("from") or "").lower() != wallet.lower() or str(rcpt.get("to") or "").lower() != factory:
        raise HTTPException(400, "That launch was not made by this wallet through the Ferzan factory")
    for log in rcpt.get("logs") or []:
        t = log.get("topics") or []
        if (str(log.get("address") or "").lower() == factory and len(t) >= 4
                and str(t[0]).lower() == CURVE_LAUNCHED_TOPIC and _topic_addr(t[3]).lower() == wallet.lower()):
            return {"curve": _topic_addr(t[1]), "token": _topic_addr(t[2])}
    raise HTTPException(400, "No Ferzan launch found in that transaction")


class SolBroadcastBody(BaseModel):
    signed_tx_b64: str


class SolConfirmBody(BaseModel):
    signature: str


async def _sol_rpc(method: str, params: list, timeout: int = 20) -> dict:
    import asyncio

    def call():
        r = requests.post(
            RPC_URLS["solana"],
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=timeout,
        )
        return r.json() if r.content else {}

    return await asyncio.to_thread(call)


async def _require_launch_request(request_id: str) -> None:
    import inspect

    res = get_request(request_id)  # raises 404 for unknown/expired requests
    if inspect.isawaitable(res):
        await res


@app.post("/api/launch-requests/{request_id}/sol-broadcast")
async def sol_broadcast(request_id: str, body: SolBroadcastBody):
    """For wallets that can only SIGN: the Mini App hands us the signed tx and
    we send it through our own Solana RPC (the key never reaches the browser)."""
    await _require_launch_request(request_id)
    out = await _sol_rpc(
        "sendTransaction",
        [body.signed_tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 5}],
        timeout=30,
    )
    if out.get("error"):
        err = out["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise HTTPException(400, f"Solana rejected the launch: {str(msg)[:300]}")
    return {"signature": out.get("result")}


@app.post("/api/launch-requests/{request_id}/sol-confirm")
async def sol_confirm(request_id: str, body: SolConfirmBody):
    """pending | confirmed | failed (+ a readable reason from the tx logs)."""
    await _require_launch_request(request_id)
    out = await _sol_rpc("getSignatureStatuses", [[body.signature], {"searchTransactionHistory": True}])
    st = (((out.get("result") or {}).get("value")) or [None])[0]
    if not st:
        return {"status": "pending"}
    if st.get("err"):
        reason = ""
        try:
            tx = await _sol_rpc("getTransaction", [body.signature, {"encoding": "json", "maxSupportedTransactionVersion": 0}])
            logs = (((tx.get("result") or {}).get("meta") or {}).get("logMessages")) or []
            hits = [l for l in logs if "insufficient" in l.lower() or "error" in l.lower() or "failed" in l.lower()]
            reason = (hits[0] if hits else "")[:200]
        except Exception:
            pass
        if "insufficient lamports" in reason:
            reason = "not enough SOL in the wallet for this launch"
        return {"status": "failed", "err": st.get("err"), "reason": reason or str(st.get("err"))[:200]}
    if st.get("confirmationStatus") in ("confirmed", "finalized"):
        return {"status": "confirmed"}
    return {"status": "pending"}


@app.post("/api/launch-requests/{request_id}/fail")
def fail_request(request_id: str, body: CompleteRequest):
    req = db.get_launch_request(request_id)
    if not req:
        raise HTTPException(404, "Launch request not found")

    db.update_status(request_id, "failed", error_message=body.tx_hash)  # tx_hash field reused as message here
    _notify_telegram(
        chat_id=req.chat_id,
        text=f"⚠️ Launch of <b>{_html.escape(req.name)}</b> failed or was cancelled in your wallet.",
    )
    return {"status": "ok"}


CURVE_LAUNCHED_TOPIC = "0x188ae4cd8aa7c0376e9501e76fb7a19dd1454add5c88bffbf75f391807a14475"
TOKEN_LAUNCHED_TOPIC = "0x1a8ab442384acdb09c73bc5f71549c099dad32203f07e1655e0ebce05831f749"


def _topic_addr(topic: str) -> str:
    raw = (topic or "").replace("0x", "").replace("0X", "")
    if len(raw) < 40:
        return ""
    return "0x" + raw[-40:]


def _parse_launch_receipt(chain: str, tx_hash: str, tries: int = 12) -> dict:
    rpc = (RPC_URLS.get(chain) or "").strip()
    if not rpc or not tx_hash:
        return {}
    out = {}
    for attempt in range(max(1, tries)):
        try:
            r = requests.post(
                rpc,
                json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionReceipt", "params": [tx_hash]},
                timeout=20,
            )
            receipt = (r.json() or {}).get("result") or {}
        except Exception as exc:
            logger.warning("receipt fetch failed: %s", exc)
            receipt = {}
        for log in receipt.get("logs") or []:
            topics = log.get("topics") or []
            if not topics:
                continue
            sig = str(topics[0]).lower()
            if sig == CURVE_LAUNCHED_TOPIC and len(topics) >= 3:
                out["curve"] = _topic_addr(topics[1])
                out["token"] = _topic_addr(topics[2])
            elif sig == TOKEN_LAUNCHED_TOPIC and len(topics) >= 2:
                out.setdefault("token", _topic_addr(topics[1]))
        if out.get("token") or out.get("curve") or receipt.get("status"):
            return out
        time.sleep(2)
    return out


def _wei(raw) -> int:
    try:
        v = Decimal(str(raw or "0").strip().replace(",", "") or "0")
    except InvalidOperation:
        return 0
    return int(v * 10**18) if v > 0 else 0


_EXPLORER = {
    "solana": "https://solscan.io/token/", "bsc": "https://bscscan.com/token/",
    "base": "https://basescan.org/token/", "ethereum": "https://etherscan.io/token/",
    "robinhood": "https://robinhoodchain.blockscout.com/token/", "arc": "https://explorer.arc.io/token/",
    "ton": "https://tonviewer.com/", "tron": "https://tronscan.org/#/token20/",
}
_CHAIN_NAME = {"solana": "Solana", "bsc": "BNB Chain", "base": "Base", "ethereum": "Ethereum", "robinhood": "Robinhood Chain", "arc": "Arc", "ton": "TON", "tron": "Tron"}


def _launch_card(req, token_addr: str, curve_addr: str, tx_hash: str) -> str:
    esc = _html.escape
    mode_txt = {"plain": "Standard token", "meteora": "Meteora bonding curve",
                "bonding_curve": "Bonding curve"}.get(req.mode, req.mode)
    if req.mode == "bonding_curve" and req.chain == "tron":
        safety = ("Fixed supply, no owner. Trades only on the curve until it fills, then moves to a SunSwap pool at "
                  "the same price and the LP is burned forever.")
    elif req.mode == "bonding_curve":
        safety = ("Fixed supply, no owner. Trades on the curve, then moves to a DEX pool at the same price "
                  "and the pool liquidity is burned forever. Team tokens stay locked until graduation.")
    elif req.mode == "meteora":
        safety = "Meteora curve with anti-sniper fee; moves to a Meteora DAMM v2 pool when it fills."
    elif req.chain == "tron":
        safety = ("Fixed supply, no owner, can never be minted again. Creator: send /liquidity to open a SunSwap pool "
                  "from your Trade Bot wallet (LP burned by default) so anyone can trade it.")
    else:
        safety = "Fixed supply, no owner, can never be minted again. Use /lplock after you add liquidity."
    lines = [
        "🚀 <b>New Ferzan launch</b>",
        f"<b>{esc(req.name)}</b> (${esc(req.symbol)}) on {esc(_CHAIN_NAME.get(req.chain, req.chain))}",
        f"Type: {esc(mode_txt)}",
        f"CA: <code>{esc(token_addr or 'see transaction')}</code>",
    ]
    if token_addr and req.chain in _EXPLORER:
        lines.append(f"Explorer: {_EXPLORER[req.chain]}{esc(token_addr)}")
    if curve_addr and req.mode == "bonding_curve" and req.chain != "tron":
        url = f"{MINI_APP_BASE}/curve.html?chain={req.chain}&curve={curve_addr}"
        lines.append(f"📈 Buy / sell on the curve: {esc(url)}")
    else:
        trade = (os.environ.get("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")
        lines.append(f"Trade: https://t.me/{esc(trade)}")
    extra = req.extra_params or {}
    links = [f'<a href="{esc(extra[k])}">{n}</a>' for k, n in (("website", "Website"), ("x", "X"), ("telegram", "Telegram"))
             if str(extra.get(k) or "").startswith("https://")]
    if links:
        lines.append("🔗 " + " · ".join(links))
    if req.description:
        lines.append(f"<i>{esc(req.description[:200])}</i>")
    lines += [f"Tx: <code>{esc(tx_hash or '')}</code>", "", f"🛡 {esc(safety)}"]
    try:  # creator track record (the same check the Trade Bot shows on every Ferzan coin)
        _SCORE_CACHE.pop(_norm_addr(token_addr), None)
        cs = creator_score(token_addr)
        if cs.get("found"):
            hist = [x for x in cs.get("lines") or [] if "launch" in x.lower()][:2]
            lines.append(f"🧑‍💻 Creator score {cs['score']}/100 ({cs['label']})" + ("\n" + "\n".join(esc(x) for x in hist) if hist else ""))
            _SCORE_CACHE.pop(_norm_addr(token_addr), None)
    except Exception as e:  # never block a launch card
        logger.info("creator score skipped: %s", e)
    return "\n".join(lines)


def _growth_buttons(req, token_addr: str, curve_addr: str = "", trade_only: bool = False):
    """Buttons under a launch card: trade (curve page + Trade Bot), then Buy Bot and Guardian."""
    buy = (os.environ.get("FERZAN_BUY_BOT") or "Ferzan_Buy_Bot").lstrip("@")
    guard = (os.environ.get("FERZAN_GUARDIAN_BOT") or "Ferzan_Guardian_Bot").lstrip("@")
    trade = (os.environ.get("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")
    key = {"bsc": "bsc", "base": "base", "solana": "sol", "ethereum": "eth"}.get(req.chain)
    rows = []
    ok_ca = bool(token_addr) and bool(_re.fullmatch(r"[0-9A-Za-z]{32,44}", token_addr.replace("0x", "", 1)))
    if curve_addr and req.mode == "bonding_curve" and _re.fullmatch(r"0x[0-9a-fA-F]{40}", curve_addr):
        rows.append([{"text": "📈 Buy / Sell on the curve",
                      "url": f"{MINI_APP_BASE}/curve.html?chain={req.chain}&curve={curve_addr}"}])
    if ok_ca:
        rows.append([{"text": "⚡ Buy in Ferzan Trade Bot", "url": f"https://t.me/{trade}?start=buy_{token_addr}"}])
    if trade_only:
        return {"inline_keyboard": rows} if rows else None
    if key and token_addr and _re.fullmatch(r"[0-9A-Za-z]{32,44}", token_addr.replace("0x", "", 1)):
        rows.append([{"text": "🟢 Add Buy Bot to your group", "url": f"https://t.me/{buy}?startgroup=trk_{key}_{token_addr}"}])
    rows.append([{"text": "🛡 Add Guardian to your group", "url": f"https://t.me/{guard}?startgroup=ferzan"}])
    return {"inline_keyboard": rows}


def _notify_telegram(chat_id: int, text: str, photo: str = "", markup: dict | None = None):
    if not chat_id:
        return  # website launches have no Telegram chat
    if photo and TELEGRAM_BOT_TOKEN and len(text) <= 1024:
        try:
            body = {"chat_id": chat_id, "photo": photo, "caption": text, "parse_mode": "HTML"}
            if markup:
                body["reply_markup"] = markup
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto",
                json=body,
                timeout=15,
            )
            if r.ok:
                return
        except requests.RequestException:
            pass
    _notify_text(chat_id, text, markup)


def _notify_text(chat_id: int, text: str, markup: dict | None = None):
    if not chat_id:
        return
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set -- cannot notify user")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
                  **({"reply_markup": markup} if markup else {})},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.error(f"Failed to notify Telegram chat {chat_id}: {e}")


# ------------------------------------------------ Solana fee claiming --
# Meteora DBC keeps each pool's trading fees in the pool until they're claimed:
# the creator's share by the creator wallet, the platform share by the partner
# fee wallet. We only build unsigned transactions; the claiming wallet signs.
_FEES_HELPER = _Path(__file__).resolve().parent / "dbc" / "fees.mjs"
_DBC_PROGRAM = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_FEE_CACHE: dict = {}


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + raw


def _sol_addr_ok(a: str) -> bool:
    return bool(_re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,44}", a or ""))


def _run_fees(payload: dict, timeout: int = 90) -> dict:
    import json as _json
    import subprocess as _sp

    if not _FEES_HELPER.exists():
        raise HTTPException(503, "Fee claiming isn't installed (dbc/fees.mjs missing).")
    payload = {**payload, "rpc": RPC_URLS["solana"], "config": (os.environ.get("METEORA_CONFIG") or "").strip()}
    try:
        proc = _sp.run(["node", str(_FEES_HELPER)], input=_json.dumps(payload), capture_output=True,
                       text=True, timeout=timeout, cwd=str(_FEES_HELPER.parent))
    except _sp.TimeoutExpired:
        raise HTTPException(504, "Solana was slow to answer - try again in a minute.")
    out_s = proc.stdout or ""
    start = out_s.find("{")
    try:
        out = _json.loads(out_s[start:]) if start >= 0 else {}
    except ValueError:
        out = {}
    if proc.returncode != 0 or out.get("error"):
        detail = out.get("error") or (proc.stderr or "").strip()[-300:] or "unknown error"
        logger.warning("SOL_FEES_FAILED: %s", detail)
        raise HTTPException(400, str(detail)[:300])
    return out


def _token_names(mints: list) -> dict:
    mints = [m for m in mints if m][:500]
    if not mints:
        return {}
    q = ",".join("?" for _ in mints)
    with db._get_conn() as conn:
        rows = conn.execute(
            f"SELECT result_token_address, name, symbol FROM launch_requests WHERE result_token_address IN ({q})",
            mints,
        ).fetchall()
    return {r[0]: {"name": r[1], "symbol": r[2]} for r in rows}


@app.get("/api/sol-fees")
def sol_fees(wallet: str, role: str = "creator"):
    role = "partner" if role == "partner" else "creator"
    if not _sol_addr_ok(wallet):
        raise HTTPException(400, "That isn't a Solana wallet address.")
    key = (role, wallet)
    hit = _FEE_CACHE.get(key)
    if hit and time.time() - hit[0] < 15:
        return hit[1]
    out = _run_fees({"action": "list", "role": role, "wallet": wallet})
    names = _token_names([p.get("mint") for p in out.get("pools", [])])
    for p in out.get("pools", []):
        n = names.get(p.get("mint")) or {}
        p["name"], p["symbol"] = n.get("name", ""), n.get("symbol", "")
        p["sol"] = int(p.get("quote_fee") or 0) / 1e9
    out["total_sol"] = int(out.get("total_quote") or 0) / 1e9
    _FEE_CACHE[key] = (time.time(), out)
    return out


class SolFeesBuildBody(BaseModel):
    wallet: str
    role: str = "creator"
    pools: list = []


@app.post("/api/sol-fees/build")
def sol_fees_build(body: SolFeesBuildBody):
    role = "partner" if body.role == "partner" else "creator"
    pools = [str(p) for p in (body.pools or []) if _sol_addr_ok(str(p))][:12]
    if not _sol_addr_ok(body.wallet) or not pools:
        raise HTTPException(400, "Nothing to claim.")
    _FEE_CACHE.pop((role, body.wallet), None)
    return _run_fees({"action": "build", "role": role, "wallet": body.wallet, "pools": pools})


@app.post("/api/sol-fees/send")
async def sol_fees_send(body: SolBroadcastBody):
    """Relay a wallet-signed claim tx through our RPC (only Meteora DBC claims)."""
    import base64 as _b64

    try:
        raw = _b64.b64decode(body.signed_tx_b64, validate=True)
    except Exception:
        raise HTTPException(400, "Bad transaction encoding.")
    if len(raw) > 1232 or _b58decode(_DBC_PROGRAM) not in raw:
        raise HTTPException(400, "Only fee-claim transactions can be sent here.")
    out = await _sol_rpc(
        "sendTransaction",
        [body.signed_tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 5}],
        timeout=30,
    )
    if out.get("error"):
        err = out["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise HTTPException(400, f"Solana rejected the claim: {str(msg)[:300]}")
    return {"signature": out.get("result")}


@app.post("/api/sol-fees/confirm")
async def sol_fees_confirm(body: SolConfirmBody):
    out = await _sol_rpc("getSignatureStatuses", [[body.signature], {"searchTransactionHistory": True}])
    st = (((out.get("result") or {}).get("value")) or [None])[0]
    if not st:
        return {"status": "pending"}
    if st.get("err"):
        return {"status": "failed", "reason": str(st.get("err"))[:200]}
    if st.get("confirmationStatus") in ("confirmed", "finalized"):
        return {"status": "confirmed"}
    return {"status": "pending"}


# --------------------------------------- curve index: chart, feed, track record --
# curve_indexer.py (its own service) fills this read-only index from chain logs.
_NATIVE_USD: dict = {"t": 0.0, "bsc": 0.0, "base": 0.0, "solana": 0.0, "tron": 0.0}
_NATIVE_SYM = {"bsc": "BNB", "base": "ETH", "ethereum": "ETH", "robinhood": "ETH", "solana": "SOL", "arc": "USDC",
               "tron": "TRX", "ton": "TON"}


def _idx_db():
    import sqlite3 as _sq

    path = os.environ.get("CURVE_INDEX_DB") or os.path.join(os.path.dirname(db.DB_PATH) or ".", "curve_index.db")
    if not os.path.exists(path):
        return None
    c = _sq.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    c.row_factory = _sq.Row
    return c


def _native_usd(chain: str) -> float:
    if chain == "arc":
        return 1.0  # Arc's gas token is USDC
    chain = "base" if chain in ("ethereum", "robinhood") else chain  # all ETH-gas chains share the ETH price
    if time.time() - _NATIVE_USD["t"] > 300:
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids": "binancecoin,ethereum,solana,tron", "vs_currencies": "usd"}, timeout=8).json()
            _NATIVE_USD.update(t=time.time(), bsc=float(r["binancecoin"]["usd"]), base=float(r["ethereum"]["usd"]),
                               solana=float(r["solana"]["usd"]), tron=float((r.get("tron") or {}).get("usd") or 0))
        except Exception:
            _NATIVE_USD["t"] = time.time() - 240  # retry in a minute
    return float(_NATIVE_USD.get(chain) or 0.0)


def _launch_rows_by_curve() -> dict:
    import json as _json

    out = {}
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT extra_params, image_url, description, telegram_user_id, wallet_address FROM launch_requests "
            "WHERE mode IN ('bonding_curve', 'meteora') AND status = 'confirmed'").fetchall()
    for r in rows:
        try:
            cv = (_json.loads(r[0] or "{}").get("curve_address") or "").strip()
        except ValueError:
            cv = ""
        if cv:
            try:
                src = "site" if _json.loads(r[0] or "{}").get("source") == "site" else "telegram"
            except ValueError:
                src = "telegram"
            out[cv] = out[cv.lower()] = {"image": r[1] or "", "description": r[2] or "", "source": src}
    return out


def _creator_stats(c, creators: list) -> dict:
    creators = list({x for x in creators if x})[:200]
    if not creators or c is None:
        return {}
    q = ",".join("?" for _ in creators)
    rows = c.execute(
        f"SELECT creator, COUNT(*) AS n, SUM(graduated) AS g, MAX(mcap) AS best, MAX(chain) AS chain "
        f"FROM curves WHERE creator IN ({q}) GROUP BY creator", creators).fetchall()
    return {r["creator"]: {"launches": r["n"], "graduated": r["g"] or 0, "best_mcap": r["best"] or 0} for r in rows}


def _trade_url(chain: str, curve: str, token: str) -> str:
    """Where to trade an indexed launch: Solana tokens trade on Jupiter, EVM curves on our trade page."""
    if chain == "solana":
        return f"https://jup.ag/tokens/{token}"
    if chain == "tron":  # Tron curves trade in the Ferzan Trade Bot
        return f"https://t.me/{(os.environ.get('FERZAN_BOT_USERNAME') or 'Ferzan_Trade_Bot').lstrip('@')}?start=buy_{token}"
    return f"{MINI_APP_BASE}/curve.html?chain={chain}&curve={curve}"


def _curve_item(r, usd: float, extra: dict, vol24: float, stats: dict) -> dict:
    grad = int(r["grad_target"] or 0)
    prog = 100.0 if r["graduated"] else (min(100.0, int(r["real_eth"] or 0) * 100.0 / grad) if grad else 0.0)
    return {
        "chain": r["chain"], "curve": r["curve"], "token": r["token"], "name": r["name"], "symbol": r["symbol"],
        "image": extra.get("image", ""), "progress": round(prog, 2), "graduated": bool(r["graduated"]),
        "mcap_native": r["mcap"] or 0, "mcap_usd": (r["mcap"] or 0) * usd, "native": _NATIVE_SYM.get(r["chain"], ""),
        "volume_native": r["volume"] or 0, "vol24_native": vol24, "vol24_usd": vol24 * usd, "trades": r["trades"] or 0,
        "launched_ts": r["launched_ts"], "last_trade_ts": r["last_trade_ts"], "start_time": r["start_time"],
        "creator": r["creator"], "creator_stats": stats.get(r["creator"], {}),
        "url": _trade_url(r["chain"], r["curve"], r["token"]), "source": extra.get("source", "telegram"),
    }


@app.get("/api/launches")
def launches_feed(sort: str = "new", limit: int = 30, chain: str = "", q: str = ""):
    """New launches (all chains), King of the Hill (closest to graduating) and top 24h volume."""
    limit = max(1, min(int(limit or 30), 60))
    c = _idx_db()
    items: list = []
    if c is not None:
        where, args = "1=1", []
        if chain in _NATIVE_SYM:
            where, args = "chain = ?", [chain]
        qn = (q or "").strip().lower()[:44]
        if qn:
            where += " AND (LOWER(name) LIKE ? OR LOWER(symbol) LIKE ? OR LOWER(token) = ? OR LOWER(curve) = ?)"
            args += [f"%{qn}%", f"%{qn}%", qn, qn]
        if sort == "graduated":
            rows = c.execute(f"SELECT * FROM curves WHERE {where} AND graduated = 1 "
                             "ORDER BY COALESCE(grad_ts, launched_ts) DESC LIMIT ?", args + [limit]).fetchall()
        elif sort == "koth":
            rows = c.execute(
                f"SELECT * FROM curves WHERE {where} AND graduated = 0 AND trades > 0 "
                "ORDER BY CAST(real_eth AS REAL) / MAX(CAST(grad_target AS REAL), 1) DESC LIMIT ?", args + [limit]).fetchall()
        elif sort == "trending":
            # momentum: native volume in the last hour, in USD (EVM trades + Solana poll deltas), still on the curve
            since = int(time.time()) - 3600
            mom = {}
            for ch_, cv_, v_, n_ in c.execute(
                    "SELECT chain, curve, SUM(native), COUNT(*) FROM trades WHERE ts > ? GROUP BY chain, curve", (since,)):
                mom[(ch_, cv_)] = [float(v_ or 0), int(n_ or 0)]
            try:
                for cv_, v_, n_ in c.execute("SELECT pool, SUM(vol), SUM(trades) FROM sol_vol WHERE ts > ? GROUP BY pool", (since,)):
                    mom[("solana", cv_)] = [float(v_ or 0), int(n_ or 0)]
            except Exception:
                pass  # no Solana momentum table yet
            ranked = sorted(mom.items(), key=lambda kv: (kv[1][0] * _native_usd(kv[0][0]), kv[1][1]), reverse=True)
            rows = []
            for (ch_, cv_), _m in ranked:
                if chain in _NATIVE_SYM and ch_ != chain:
                    continue
                r_ = c.execute("SELECT * FROM curves WHERE chain = ? AND curve = ? AND graduated = 0", (ch_, cv_)).fetchone()
                if r_ and (not qn or qn in (r_["name"] or "").lower() or qn in (r_["symbol"] or "").lower()
                           or qn in (r_["token"].lower(), r_["curve"].lower())):
                    rows.append(r_)
                if len(rows) >= limit:
                    break
        elif sort == "volume":
            rows = c.execute(
                f"SELECT curves.* FROM curves JOIN (SELECT chain AS vc, curve AS vv, SUM(native) AS v FROM trades "
                f"WHERE ts > ? GROUP BY chain, curve) ON vc = curves.chain AND vv = curves.curve WHERE {where} "
                "ORDER BY v DESC LIMIT ?", [int(time.time()) - 86400] + args + [limit]).fetchall()
        else:
            rows = c.execute(f"SELECT * FROM curves WHERE {where} ORDER BY launched_ts DESC LIMIT ?", args + [limit]).fetchall()
        vol = {}
        if rows:
            q = ",".join("?" for _ in rows)
            for v in c.execute(f"SELECT curve, SUM(native) FROM trades WHERE ts > ? AND curve IN ({q}) GROUP BY curve",
                               [int(time.time()) - 86400] + [r["curve"] for r in rows]):
                vol[v[0]] = v[1] or 0.0
        stats = _creator_stats(c, [r["creator"] for r in rows])
        extra = _launch_rows_by_curve()
        items = [_curve_item(r, _native_usd(r["chain"]), extra.get(r["curve"], {}), vol.get(r["curve"], 0.0), stats) for r in rows]
        c.close()
    if sort == "new" and chain in ("", "solana") and not (q or "").strip():
        with db._get_conn() as conn:
            sol = conn.execute(
                "SELECT name, symbol, image_url, result_token_address, created_at FROM launch_requests "
                "WHERE chain = 'solana' AND mode IN ('meteora', 'pumpfun') AND status = 'confirmed' "
                "AND result_token_address IS NOT NULL ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        from datetime import datetime as _dt
        have = {it.get("token") for it in items}
        for r in sol:
            if r[3] in have:  # already in the list from the curve index (Meteora launches)
                continue
            try:
                ts = int(_dt.fromisoformat(str(r[4]).replace("Z", "+00:00")).timestamp())
            except ValueError:
                ts = 0
            items.append({"chain": "solana", "token": r[3], "name": r[0], "symbol": r[1], "image": r[2] or "",
                          "launched_ts": ts, "native": "SOL", "progress": None, "graduated": False,
                          "url": f"https://jup.ag/tokens/{r[3]}"})
        items.sort(key=lambda x: x.get("launched_ts") or 0, reverse=True)
        items = items[:limit]
    if sort == "new" and chain in ("",) + _PLAIN_FEED_CHAINS:
        have = {(it.get("chain"), it.get("token")) for it in items}
        items += [it for it in _plain_items(chain, q, limit) if (it["chain"], it["token"]) not in have]
        items.sort(key=lambda x: x.get("launched_ts") or 0, reverse=True)
        items = items[:limit]
    hide = {h.strip().lower() for h in (os.environ.get("FEED_HIDE") or _env_file_value("/opt/ferzan/.env", "FEED_HIDE")
                                        or "").split(",") if h.strip()}
    if hide:  # test coins kept off the public board (FEED_HIDE = comma list of token or curve addresses)
        items = [it for it in items if str(it.get("token", "")).lower() not in hide and str(it.get("curve", "")).lower() not in hide]
    return {"sort": sort, "items": items, "now": int(time.time())}


# ---- PLAIN_COINS_FEED: fixed-supply launches on chains with no Ferzan curve (Tron, TON) and Arc standard tokens ----
_PLAIN_FEED_CHAINS = ("tron", "ton", "arc")
_PLAIN_URL = {"tron": "https://tronscan.org/#/token20/{t}", "ton": "https://tonviewer.com/{t}",
              "arc": "https://explorer.arc.io/token/{t}"}


def _ts_of(created: str) -> int:
    from datetime import datetime as _dt

    try:
        return int(_dt.fromisoformat(str(created).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _plain_items(chain: str, q: str, limit: int) -> list:
    chains = [chain] if chain else list(_PLAIN_FEED_CHAINS)
    qn = (q or "").strip().lower()[:44]
    marks = ",".join("?" for _ in chains)
    sql = ("SELECT chain, name, symbol, image_url, result_token_address, created_at, wallet_address FROM launch_requests "
           f"WHERE chain IN ({marks}) AND mode = 'plain' AND status = 'confirmed' AND result_token_address IS NOT NULL "
           "AND result_token_address != ''")
    args: list = list(chains)
    if qn:
        sql += " AND (LOWER(name) LIKE ? OR LOWER(symbol) LIKE ? OR LOWER(result_token_address) = ?)"
        args += [f"%{qn}%", f"%{qn}%", qn]
    with db._get_conn() as conn:
        rows = conn.execute(sql + " ORDER BY created_at DESC LIMIT ?", args + [limit]).fetchall()
    return [{"chain": r[0], "token": r[4], "name": r[1], "symbol": r[2], "image": r[3] or "",
             "launched_ts": _ts_of(r[5]), "native": _NATIVE_SYM.get(r[0], ""), "progress": None, "graduated": False,
             "creator": r[6] or "", "url": _PLAIN_URL[r[0]].format(t=r[4]), "source": "telegram"} for r in rows]


@app.get("/api/coin/{chain}/{token}")
def plain_coin(chain: str, token: str):
    """One standard (fixed-supply) Ferzan launch, for the website's coin page."""
    if chain not in _PLAIN_FEED_CHAINS or not _re.fullmatch(r"[0-9A-Za-z_:\-]{20,70}", token or ""):
        raise HTTPException(404, "not a Ferzan coin")
    with db._get_conn() as conn:
        r = conn.execute("SELECT * FROM launch_requests WHERE chain = ? AND mode = 'plain' AND status = 'confirmed' "
                         "AND LOWER(result_token_address) = LOWER(?) ORDER BY created_at DESC LIMIT 1",
                         (chain, token)).fetchone()
    if not r:
        raise HTTPException(404, "not a Ferzan coin")
    import json as _json

    try:
        extra = _json.loads(r["extra_params"] or "{}")
    except ValueError:
        extra = {}
    dec = int(r["decimals"] or 0)
    trade = (os.environ.get("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")
    return {
        "chain": chain, "chain_name": _CHAIN_NAME.get(chain, chain), "token": r["result_token_address"],
        "name": r["name"], "symbol": r["symbol"], "image": r["image_url"] or "", "description": r["description"] or "",
        "links": {k: extra[k] for k in ("website", "x", "telegram") if str(extra.get(k) or "").startswith("https://")},
        "supply": str(int(r["total_supply"]) // (10 ** dec)) if dec else str(r["total_supply"]), "decimals": dec,
        "creator": r["wallet_address"] or "", "launched_ts": _ts_of(r["created_at"]), "tx": r["tx_hash"] or "",
        "native": _NATIVE_SYM.get(chain, ""), "explorer": _PLAIN_URL[chain].format(t=r["result_token_address"]),
        "trade_bot": f"https://t.me/{trade}?start=buy_{r['result_token_address']}",
        "fixed_supply": True,
    }


@app.get("/api/leaderboard")
def leaderboard(period: str = "all", chain: str = "", limit: int = 50):
    """Top creators: graduations first, then trading volume on their curves (USD)."""
    limit = max(1, min(int(limit or 50), 100))
    cutoff = {"7d": 7, "30d": 30}.get(period, 0)
    c = _idx_db()
    if c is None:
        return {"period": period, "items": [], "now": int(time.time())}
    try:
        where, args = "1=1", []
        if cutoff:
            where, args = "launched_ts > ?", [int(time.time()) - cutoff * 86400]
        if chain in _NATIVE_SYM:
            where += " AND chain = ?"
            args.append(chain)
        rows = c.execute(f"SELECT chain, curve, token, name, symbol, creator, graduated, mcap, volume, trades "
                         f"FROM curves WHERE {where}", args).fetchall()
    finally:
        c.close()
    agg: dict = {}
    for r in rows:
        if not r["creator"]:
            continue
        px = _native_usd(r["chain"])
        a = agg.setdefault(r["creator"], {"creator": r["creator"], "launches": 0, "graduated": 0, "volume_usd": 0.0,
                                          "trades": 0, "best": None, "chains": set()})
        a["launches"] += 1
        a["graduated"] += int(r["graduated"] or 0)
        a["volume_usd"] += float(r["volume"] or 0) * px
        a["trades"] += int(r["trades"] or 0)
        a["chains"].add(r["chain"])
        mc = float(r["mcap"] or 0) * px
        if a["best"] is None or mc > a["best"]["mcap_usd"]:
            a["best"] = {"name": r["name"], "symbol": r["symbol"], "chain": r["chain"], "token": r["token"], "mcap_usd": mc,
                         "graduated": bool(r["graduated"]),
                         "url": _trade_url(r["chain"], r["curve"], r["token"])}
    items = sorted(agg.values(), key=lambda a: (a["graduated"], a["volume_usd"], a["launches"]), reverse=True)[:limit]
    for i, a in enumerate(items, 1):
        a["rank"] = i
        a["chains"] = sorted(a["chains"])
        a["short"] = a["creator"][:6] + "…" + a["creator"][-4:]
    return {"period": period, "items": items, "now": int(time.time())}


_EVM_LAUNCH_FEE = {"bsc": 0.015, "base": 0.003, "ethereum": 0.003, "robinhood": 0.003, "arc": 1.0}  # native, fixed in the v3 factories
_REV_RPC = {"bsc": "https://bsc-rpc.publicnode.com", "base": "https://base-rpc.publicnode.com",
            "ethereum": "https://ethereum-rpc.publicnode.com", "robinhood": "https://rpc.mainnet.chain.robinhood.com",
            "arc": "https://rpc.mainnet.arc.io"}


def _env_file_value(path: str, key: str) -> str:
    try:
        for line in open(path):
            if line.strip().startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _rpc_balance(chain: str, addr: str) -> float | None:
    urls = [u for u in (RPC_URLS.get(chain) or "", _REV_RPC.get(chain, "")) if u]
    for u in urls:
        try:
            r = requests.post(u, json={"jsonrpc": "2.0", "id": 1, "method": "eth_getBalance", "params": [addr, "latest"]}, timeout=6).json()
            if r.get("result"):
                return int(r["result"], 16) / 1e18
        except Exception:
            continue
    return None


@app.get("/internal/revenue")
def internal_revenue(request: Request):
    """Platform income by period and source (admin report). Curve trade fees are exact once recorded,
    estimated (1% fee rule) for older trades; launch fees use the fixed factory fees; Trade Bot = volume x FEE_BPS."""
    import json as _json
    import sqlite3 as _sq
    from datetime import datetime as _dt

    if not _internal_ok(request):
        raise HTTPException(403, "internal only")
    now = int(time.time())
    periods = {"24h": now - 86400, "7d": now - 7 * 86400, "30d": now - 30 * 86400, "all": 0}
    px = {ch: _native_usd(ch) for ch in ("bsc", "base", "ethereum", "robinhood", "solana", "arc")}
    rev = {p: {"curve": 0.0, "launch": 0.0, "desk": 0.0} for p in periods}
    by_chain = {}          # 30d curve+launch income per chain, native
    launches_30d = 0
    estimated = False
    # 1) curve trading fees (platform share) from the index
    c = _idx_db()
    if c is not None:
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
            exact = "fee" in cols
            fee_expr = ("CASE WHEN fee IS NOT NULL THEN fee * (CASE WHEN referrer IS NOT NULL AND referrer NOT IN ('', "
                        "'0x0000000000000000000000000000000000000000') AND referrer != trader THEN 0.4 ELSE 0.5 END) "
                        "ELSE (CASE WHEN is_buy = 1 THEN native * 0.01 ELSE native / 0.99 * 0.01 END) * 0.5 END") if exact else \
                       "(CASE WHEN is_buy = 1 THEN native * 0.01 ELSE native / 0.99 * 0.01 END) * 0.5"
            for p, since in periods.items():
                for ch, amt, n_est in c.execute(
                        f"SELECT chain, SUM({fee_expr}), SUM(CASE WHEN {'fee IS NULL' if exact else '1'} THEN 1 ELSE 0 END) "
                        f"FROM trades WHERE ts >= ? AND chain != 'solana' GROUP BY chain", (since,)):  # Solana fees: claimed separately
                    rev[p]["curve"] += float(amt or 0) * px.get(ch, 0)
                    estimated = estimated or bool(n_est)
                    if p == "30d":
                        by_chain.setdefault(ch, {"curve": 0.0, "launch": 0.0})["curve"] += float(amt or 0)
        finally:
            c.close()
    # 2) launch fees from confirmed launches
    sol_fee = int(os.environ.get("LAUNCH_FEE_LAMPORTS") or "50000000") / 1e9
    with db._get_conn() as conn:
        rows = conn.execute("SELECT chain, mode, created_at FROM launch_requests WHERE status = 'confirmed'").fetchall()
    for ch, mode, created in rows:
        try:
            ts = int(_dt.fromisoformat(str(created).replace("Z", "+00:00")).timestamp())
        except ValueError:
            continue
        fee = sol_fee if ch == "solana" and mode == "meteora" else _EVM_LAUNCH_FEE.get(ch, 0.0)
        if not fee:
            continue
        for p, since in periods.items():
            if ts >= since:
                rev[p]["launch"] += fee * px.get(ch, 0)
        if ts >= periods["30d"]:
            launches_30d += 1
            by_chain.setdefault(ch, {"curve": 0.0, "launch": 0.0})["launch"] += fee
    # 3) Trade Bot swap fee (FEE_BPS of live volume; curve-token trades excluded - those pay the curve fee instead)
    desk_vol = {p: 0.0 for p in periods}
    td = "/opt/ferzan/app/Ferzan-Ecosystem/Trade Desk/.env"
    bps = int(_env_file_value(td, "FEE_BPS") or os.environ.get("FEE_BPS") or 50)
    tdb = _env_file_value(td, "DB_PATH") or "/opt/ferzan/app/ferzan.db"
    try:
        curve_tokens = set()
        c = _idx_db()
        if c is not None:
            curve_tokens = {str(r[0]).lower() for r in c.execute("SELECT token FROM curves WHERE graduated = 0")}
            c.close()
        k = _sq.connect(f"file:{tdb}?mode=ro", uri=True, timeout=10)
        for ts, usd, mint in k.execute("SELECT ts, usd, mint FROM live_trades WHERE ts >= ?", (0,)):
            if str(mint).lower() in curve_tokens:
                continue
            for p, since in periods.items():
                if ts >= since:
                    desk_vol[p] += float(usd or 0)
        k.close()
    except Exception as e:
        logger.info("revenue: trade desk db not readable: %s", e)
    for p in periods:
        rev[p]["desk"] = desk_vol[p] * bps / 10_000
    # 4) Solana trading fees waiting in Meteora pools + treasury balances
    sol_treasury = (os.environ.get("PLATFORM_TREASURY_SOL") or os.environ.get("TREASURY_SOL") or "").strip()
    unclaimed_sol = None
    if sol_treasury:
        try:
            out = _run_fees({"action": "list", "role": "partner", "wallet": sol_treasury}, timeout=60)
            unclaimed_sol = int(out.get("total_quote") or 0) / 1e9
        except Exception as e:
            logger.info("revenue: partner fee list failed: %s", e)
    balances = {}
    if PLATFORM_TREASURY_EVM:
        for ch in ("bsc", "base", "ethereum", "robinhood", "arc"):
            balances[ch] = _rpc_balance(ch, PLATFORM_TREASURY_EVM)
    if sol_treasury:
        try:
            r = requests.post(RPC_URLS["solana"], json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [sol_treasury]}, timeout=8).json()
            balances["solana"] = int(r["result"]["value"]) / 1e9
        except Exception:
            balances["solana"] = None
    totals = {p: round(sum(v.values()), 2) for p, v in rev.items()}
    return {
        "now": now, "totals_usd": totals, "by_source_usd": {p: {k2: round(v2, 2) for k2, v2 in v.items()} for p, v in rev.items()},
        "by_chain_30d_native": by_chain, "launches_30d": launches_30d, "desk_volume_usd": {p: round(v, 2) for p, v in desk_vol.items()},
        "desk_fee_bps": bps, "unclaimed_sol": unclaimed_sol, "unclaimed_sol_usd": (unclaimed_sol or 0) * px["solana"],
        "treasury_balances": balances, "native_usd": px, "estimated": estimated,
    }


@app.get("/internal/referral-stats/{user_id}")
def internal_referral_stats(user_id: int, request: Request):
    """What a referrer has earned: people referred, their launches, and trades that paid this wallet 10% of the fee."""
    if not _internal_ok(request):
        raise HTTPException(403, "internal only")
    uid = int(user_id)
    with db._get_conn() as conn:
        referred = conn.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id = ?", (uid,)).fetchone()[0]
        launches = conn.execute(
            "SELECT COUNT(*) FROM launch_requests WHERE status = 'confirmed' AND extra_params LIKE ?",
            (f'%"referrer_id": "{uid}"%',)).fetchone()[0]
    wallet = (db.get_payout_wallet(uid) or "").strip().lower()
    chains = {}
    c = _idx_db()
    if c is not None and wallet.startswith("0x"):
        try:
            cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
            if "referrer" in cols:
                for ch, n, vol, fee in c.execute(
                        "SELECT chain, COUNT(*), SUM(native), SUM(COALESCE(fee, 0)) FROM trades WHERE lower(referrer) = ? GROUP BY chain",
                        (wallet,)):
                    earned = float(fee or 0) * 0.10
                    chains[ch] = {"trades": n, "volume": float(vol or 0), "earned": earned,
                                  "earned_usd": earned * _native_usd(ch), "sym": _NATIVE_SYM.get(ch, "")}
        finally:
            c.close()
    return {"user_id": uid, "wallet": wallet, "referred": referred, "referred_launches": launches, "chains": chains,
            "earned_usd": round(sum(v["earned_usd"] for v in chains.values()), 2)}


@app.get("/api/curve-chart/{curve}")
def curve_chart(curve: str, tf: int = 300):
    curve = (curve or "").lower()
    if not _re.fullmatch(r"0x[0-9a-f]{40}", curve):
        raise HTTPException(400, "bad curve address")
    tf = tf if tf in (60, 300, 900, 3600, 14400) else 300
    c = _idx_db()
    if c is None:
        return {"candles": [], "trades": [], "indexed": False}
    try:
        cv = c.execute("SELECT * FROM curves WHERE curve = ?", (curve,)).fetchone()
        if not cv:
            return {"candles": [], "trades": [], "indexed": False}
        rows = c.execute("SELECT ts, price, native, is_buy, tokens, trader, tx FROM trades WHERE curve = ? ORDER BY ts, block, log_index",
                         (curve,)).fetchall()
        stats = _creator_stats(c, [cv["creator"]])
    finally:
        c.close()
    candles: list = []
    last = int(cv["v_eth"] or 0) / max(int(cv["v_token"] or 1), 1)
    if not cv["launched_block"]:  # picked up after launch: start from what we know, not the launch price
        last = rows[0]["price"] if rows else (cv["price"] or last)
    first_ts = (cv["launched_ts"] or (rows[0]["ts"] if rows else int(time.time())))
    for r in rows:
        b = (r["ts"] // tf) * tf
        if not candles or candles[-1][0] != b:
            candles.append([b, last, max(last, r["price"]), min(last, r["price"]), r["price"], 0.0])
        k = candles[-1]
        k[2], k[3], k[4] = max(k[2], r["price"]), min(k[3], r["price"]), r["price"]
        k[5] += r["native"] or 0.0
        last = r["price"]
    if not candles:
        candles = [[(first_ts // tf) * tf, last, last, last, last, 0.0]]
    supply = int(cv["total_supply"] or 0) / 1e18
    usd = _native_usd(cv["chain"])
    grad = int(cv["grad_target"] or 0)
    return {
        "indexed": True, "chain": cv["chain"], "native": _NATIVE_SYM.get(cv["chain"], ""), "native_usd": usd,
        "supply": supply, "tf": tf, "candles": candles[-400:],
        "price": last, "mcap_native": last * supply, "mcap_usd": last * supply * usd,
        "progress": 100.0 if cv["graduated"] else (min(100.0, int(cv["real_eth"] or 0) * 100.0 / grad) if grad else 0.0),
        "volume_native": cv["volume"] or 0, "trades_count": cv["trades"] or 0, "graduated": bool(cv["graduated"]),
        "creator": cv["creator"], "creator_stats": stats.get(cv["creator"], {}),
        "trades": [{"ts": r["ts"], "buy": bool(r["is_buy"]), "native": r["native"], "tokens": r["tokens"],
                    "trader": r["trader"], "tx": r["tx"]} for r in rows[-25:]][::-1],
    }


@app.get("/api/curve-by-token/{token}")
def curve_by_token(token: str, since: int = 0, kind: str = "buy"):
    """For the Buy Bot: a Ferzan curve token's recent curve trades (buys or sells) after `since`."""
    token = (token or "").strip()
    if not _re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", token):  # Tron addresses are case-sensitive
        token = token.lower()
        if not _re.fullmatch(r"0x[0-9a-f]{40}", token):
            return {"found": False}
    c = _idx_db()
    if c is None:
        return {"found": False}
    try:
        cv = c.execute("SELECT * FROM curves WHERE token = ?", (token,)).fetchone()
        if not cv:
            return {"found": False}
        rows = c.execute(
            "SELECT ts, native, tokens, trader, tx, price FROM trades WHERE curve = ? AND ts > ? AND is_buy = ? "
            "ORDER BY ts, block, log_index LIMIT 50",
            (cv["curve"], int(since or 0), 0 if kind == "sell" else 1)).fetchall()
    finally:
        c.close()
    usd = _native_usd(cv["chain"])
    grad = int(cv["grad_target"] or 0)
    return {
        "found": True, "chain": cv["chain"], "curve": cv["curve"], "token": cv["token"], "name": cv["name"],
        "symbol": cv["symbol"], "graduated": bool(cv["graduated"]), "pool": cv["pool"], "native": _NATIVE_SYM.get(cv["chain"], ""),
        "native_usd": usd, "price_usd": (cv["price"] or 0) * usd, "mcap_usd": (cv["mcap"] or 0) * usd,
        "progress": 100.0 if cv["graduated"] else (min(100.0, int(cv["real_eth"] or 0) * 100.0 / grad) if grad else 0.0),
        "url": _trade_url(cv["chain"], cv["curve"], cv["token"]),
        "trades": [{"ts": r["ts"], "native": r["native"], "tokens": r["tokens"], "usd": (r["native"] or 0) * usd,
                    "trader": r["trader"], "tx": r["tx"]} for r in rows],
    }


# ---- SOL_COIN_BATCH17: Solana coin pages on the website ----
_B58_RE = r"[1-9A-HJ-NP-Za-km-z]{32,44}"  # address pattern (not the _B58 alphabet)


def _sol_rpc_sync(method: str, params: list, timeout: int = 15) -> dict:
    try:
        return requests.post(RPC_URLS["solana"], json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                             timeout=timeout).json() or {}
    except Exception:
        return {}


@app.get("/api/sol-coin/{mint}")
def sol_coin(mint: str, tf: int = 300, wallet: str = ""):
    """Chart, stats and project info for a Ferzan Meteora curve, like /api/curve-chart for EVM."""
    if not _re.fullmatch(_B58_RE, mint or ""):
        raise HTTPException(404, "not found")
    tf = tf if tf in (60, 300, 900, 3600, 14400) else 300
    c = _idx_db()
    if c is None:
        return {"indexed": False}
    try:
        cv = c.execute("SELECT * FROM curves WHERE chain = 'solana' AND token = ?", (mint,)).fetchone()
        if not cv:
            return {"indexed": False}
        px = c.execute("SELECT ts, price FROM sol_px WHERE pool = ? ORDER BY ts", (cv["curve"],)).fetchall()
        stats = _creator_stats(c, [cv["creator"]])
    finally:
        c.close()
    candles: list = []
    last = px[0]["price"] if px else (cv["price"] or 0.0)
    for r in px:
        b = (r["ts"] // tf) * tf
        if not candles or candles[-1][0] != b:
            candles.append([b, last, max(last, r["price"]), min(last, r["price"]), r["price"], 0.0])
        k = candles[-1]
        k[2], k[3], k[4] = max(k[2], r["price"]), min(k[3], r["price"]), r["price"]
        last = r["price"]
    if not candles:
        t0 = cv["launched_ts"] or int(time.time())
        candles = [[(t0 // tf) * tf, last, last, last, last, 0.0]]
    price = cv["price"] or last
    usd = _native_usd("solana")
    grad, real = int(cv["grad_target"] or 0), int(cv["real_eth"] or 0)
    import json as _json
    info = {}
    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT name, symbol, image_url, description, extra_params FROM launch_requests "
            "WHERE chain = 'solana' AND result_token_address = ? ORDER BY created_at DESC LIMIT 1", (mint,)).fetchone()
    if row:
        try:
            extra = _json.loads(row[4] or "{}")
        except Exception:
            extra = {}
        info = {"image": row[2] or "", "description": row[3] or "",
                "website": extra.get("website", ""), "x": extra.get("x", ""), "telegram": extra.get("telegram", "")}
    mine = None
    if _re.fullmatch(_B58_RE, wallet or ""):
        bal = ((_sol_rpc_sync("getBalance", [wallet, {"commitment": "confirmed"}]).get("result") or {}).get("value")) or 0
        accts = ((_sol_rpc_sync("getTokenAccountsByOwner", [wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
                  .get("result") or {}).get("value")) or []
        tok = 0
        for a in accts:
            try:
                tok += int(a["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
            except Exception:
                pass
        mine = {"sol_lamports": str(int(bal)), "token_raw": str(tok)}
    return {
        "indexed": True, "chain": "solana", "native": "SOL", "native_usd": usd, "mint": mint, "pool": cv["curve"],
        "name": cv["name"], "symbol": cv["symbol"], **info, "decimals": 6, "supply": 1_000_000_000, "tf": tf,
        "candles": candles[-400:], "price": price, "mcap_native": price * 1_000_000_000, "mcap_usd": price * 1_000_000_000 * usd,
        "progress": 100.0 if cv["graduated"] else (min(100.0, real * 100.0 / grad) if grad else 0.0),
        "raised_sol": real / 1e18, "grad_sol": grad / 1e18,
        "volume_native": cv["volume"] or 0, "trades_count": cv["trades"] or 0, "graduated": bool(cv["graduated"]),
        "creator": cv["creator"], "creator_stats": stats.get(cv["creator"], {}), "mine": mine,
    }


import threading as _threading
_SWAP_SLOTS = _threading.BoundedSemaphore(int(os.environ.get("SOL_SWAP_PARALLEL") or "8"))


class SolSwapBody(BaseModel):
    mint: str
    wallet: str
    side: str
    amount: str
    slippage_bps: int = 500
    simulate: bool = False
    priority_micro_lamports: int = 0


@app.post("/api/sol-swap")
def sol_swap(body: SolSwapBody, request: Request):
    """For the website: an unsigned Meteora buy/sell for the visitor's wallet (their wallet must sign it)."""
    import json as _json
    import subprocess as _sp
    if not _re.fullmatch(_B58_RE, body.mint or "") or not _re.fullmatch(_B58_RE, body.wallet or ""):
        raise HTTPException(400, "Mint or wallet looks wrong")
    if body.side not in ("buy", "sell") or not _re.fullmatch(r"\d{1,20}", body.amount or "") or body.amount == "0":
        raise HTTPException(400, "Side or amount looks wrong")
    config = (os.environ.get("METEORA_CONFIG") or "").strip()
    if not config:
        raise HTTPException(501, "Solana curves are not configured")
    script = _Path(__file__).resolve().with_name("dbc") / "build_swap.mjs"
    if not _SWAP_SLOTS.acquire(timeout=30):
        raise HTTPException(503, "Busy right now. Try again in a few seconds.")
    try:
        p = _sp.run(["node", str(script)], input=_json.dumps({
            "rpc": RPC_URLS["solana"], "config": config, "mint": body.mint, "owner": body.wallet, "side": body.side,
            "amount": body.amount, "slippageBps": max(10, min(5000, int(body.slippage_bps))), "simulate": bool(body.simulate),
            "priorityMicroLamports": max(0, min(2_000_000, int(body.priority_micro_lamports or 0))),
        }), capture_output=True, text=True, timeout=45, cwd=str(script.parent))
    except _sp.TimeoutExpired:
        raise HTTPException(504, "Solana did not answer in time")
    finally:
        _SWAP_SLOTS.release()
    try:
        out = _json.loads(p.stdout or "{}")
    except Exception:
        out = {"error": (p.stderr or "no output")[-200:]}
    if out.get("error"):
        raise HTTPException(400, str(out["error"])[:300])
    return out


# ------------------------------------------ SITE_WALLET_BATCH18: portfolio --
_WALLET_CACHE: dict = {}
_TOKEN_PROGRAMS = ("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")


def _evm_balances(chain: str, wallet: str, tokens: list) -> dict:
    """balanceOf(wallet) for each token, one batched call (one by one if the node refuses batches)."""
    rpc = (RPC_URLS.get(chain) or "").strip()
    tokens = tokens[:40]
    if not rpc or not tokens:
        return {}
    data = "0x70a08231" + "0" * 24 + wallet[2:].lower()
    calls = [{"jsonrpc": "2.0", "id": i, "method": "eth_call", "params": [{"to": t, "data": data}, "latest"]}
             for i, t in enumerate(tokens)]
    try:
        res = requests.post(rpc, json=calls, timeout=12).json()
    except Exception:
        res = None
    if not isinstance(res, list):
        res = []
        for call in calls:
            try:
                res.append(requests.post(rpc, json=call, timeout=8).json())
            except Exception:
                pass
    out = {}
    for r in res:
        try:
            out[tokens[int(r["id"])]] = int(r["result"], 16)
        except Exception:
            pass
    return out


def _sol_token_balances(wallet: str) -> dict:
    out: dict = {}
    for prog in _TOKEN_PROGRAMS:
        accts = ((_sol_rpc_sync("getTokenAccountsByOwner", [wallet, {"programId": prog}, {"encoding": "jsonParsed"}])
                  .get("result") or {}).get("value")) or []
        for a in accts:
            try:
                info = a["account"]["data"]["parsed"]["info"]
                out[info["mint"]] = out.get(info["mint"], 0) + int(info["tokenAmount"]["amount"])
            except Exception:
                pass
    return out


@app.get("/api/wallet/{wallet}")
def wallet_portfolio(wallet: str):
    """Coins a wallet holds (live balances), coins it launched, and the trading fees those paid it."""
    w = (wallet or "").strip()
    evm = bool(_re.fullmatch(r"0x[0-9a-fA-F]{40}", w))
    if not evm and not _re.fullmatch(_B58_RE, w):
        raise HTTPException(400, "bad wallet address")
    key = w.lower() if evm else w
    hit = _WALLET_CACHE.get(key)
    if hit and time.time() - hit[0] < 20:
        return hit[1]
    out = {"wallet": w, "holdings": [], "launches": [], "value_usd": 0.0, "earned_usd": 0.0, "referral_usd": 0.0,
           "now": int(time.time())}
    c = _idx_db()
    if c is None:
        return out
    extra = _launch_rows_by_curve()
    fees: dict = {}
    ref: list = []
    try:
        if evm:
            created = c.execute("SELECT * FROM curves WHERE creator = ? AND chain != 'solana' "
                                "ORDER BY launched_ts DESC LIMIT 60", (key,)).fetchall()
            touched = c.execute("SELECT DISTINCT chain, curve FROM trades WHERE trader = ? LIMIT 200", (key,)).fetchall()
            if created:
                q = ",".join("?" for _ in created)
                for cv_, f_ in c.execute(f"SELECT curve, SUM(fee) FROM trades WHERE curve IN ({q}) GROUP BY curve",
                                         [r["curve"] for r in created]):
                    fees[cv_] = float(f_ or 0)
            ref = c.execute("SELECT chain, SUM(fee) FROM trades WHERE referrer = ? GROUP BY chain", (key,)).fetchall()
            pool = {(r["chain"], r["curve"]): r for r in created}
            for ch_, cv_ in touched:
                if (ch_, cv_) not in pool:
                    r_ = c.execute("SELECT * FROM curves WHERE chain = ? AND curve = ?", (ch_, cv_)).fetchone()
                    if r_:
                        pool[(ch_, cv_)] = r_
        else:
            created = c.execute("SELECT * FROM curves WHERE creator = ? AND chain = 'solana' "
                                "ORDER BY launched_ts DESC LIMIT 60", (w,)).fetchall()
            sol_rows = {r["token"]: r for r in c.execute("SELECT * FROM curves WHERE chain = 'solana'").fetchall()}
    finally:
        c.close()

    holdings = []
    if evm:
        by_chain: dict = {}
        for r in pool.values():
            by_chain.setdefault(r["chain"], []).append(r)
        for ch_, rows_ in by_chain.items():
            bals = _evm_balances(ch_, w, [r["token"] for r in rows_])
            for r in rows_:
                raw = bals.get(r["token"], 0)
                if raw > 0:
                    holdings.append((r, raw / 1e18))
    else:
        for mint, raw in _sol_token_balances(w).items():
            r = sol_rows.get(mint)
            if r and raw > 0:
                holdings.append((r, raw / 1e6))

    for r, amount in holdings:
        usd = _native_usd(r["chain"])
        item = _curve_item(r, usd, extra.get(r["curve"], {}), 0.0, {})
        item["balance"] = amount
        item["value_native"] = (r["price"] or 0) * amount
        item["value_usd"] = item["value_native"] * usd
        out["value_usd"] += item["value_usd"]
        out["holdings"].append(item)
    out["holdings"].sort(key=lambda x: x["value_usd"], reverse=True)

    for r in created:
        usd = _native_usd(r["chain"])
        item = _curve_item(r, usd, extra.get(r["curve"], {}), 0.0, {})
        if r["chain"] != "solana":  # EVM curves pay the creator 50% of the 1% fee on every trade, straight to the wallet
            item["earned_native"] = fees.get(r["curve"], 0.0) * 0.5
            item["earned_usd"] = item["earned_native"] * usd
            out["earned_usd"] += item["earned_usd"]
        out["launches"].append(item)
    for ch_, f_ in ref:
        out["referral_usd"] += float(f_ or 0) * 0.1 * _native_usd(ch_)
    _WALLET_CACHE[key] = (time.time(), out)
    if len(_WALLET_CACHE) > 2000:
        _WALLET_CACHE.clear()
    return out


@app.get("/api/creator/{wallet}")
def creator_record(wallet: str):
    w = (wallet or "").strip()
    evm = bool(_re.fullmatch(r"0x[0-9a-fA-F]{40}", w))
    if not evm and not _sol_addr_ok(w):
        raise HTTPException(400, "bad wallet address")
    out = {"wallet": w, "launches": 0, "graduated": 0, "best_mcap_usd": 0.0, "tokens": []}
    c = _idx_db()
    if evm and c is not None:
        try:
            rows = c.execute("SELECT * FROM curves WHERE creator = ? ORDER BY launched_ts DESC LIMIT 50", (w.lower(),)).fetchall()
        finally:
            c.close()
        for r in rows:
            usd = _native_usd(r["chain"])
            out["graduated"] += 1 if r["graduated"] else 0
            out["best_mcap_usd"] = max(out["best_mcap_usd"], (r["mcap"] or 0) * usd)
            out["tokens"].append({"chain": r["chain"], "symbol": r["symbol"], "name": r["name"], "graduated": bool(r["graduated"]),
                                  "mcap_usd": (r["mcap"] or 0) * usd, "launched_ts": r["launched_ts"],
                                  "url": _trade_url(r["chain"], r["curve"], r["token"])})
    with db._get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM launch_requests WHERE status = 'confirmed' AND LOWER(wallet_address) = ?",
                         (w.lower() if evm else w,)).fetchone()[0] if evm else conn.execute(
            "SELECT COUNT(*) FROM launch_requests WHERE status = 'confirmed' AND wallet_address = ?", (w,)).fetchone()[0]
    out["launches"] = max(int(n or 0), len(out["tokens"]))
    return out


# ---- PULSE_BATCH_A: live trade tape, fresh graduations and the FERZAN hero, for the website ----
_PULSE: dict = {"t": 0.0, "v": None}
_FLAGSHIP_STATE = "/opt/ferzan/dbc-keys/ferzan-flagship-state.json"
_FLYWHEEL_STATE = "/opt/ferzan/dbc-keys/flywheel-state.json"
_FERZAN_LAUNCH_AT = 1791586800  # Fri Oct 9 2026 23:00 UTC (7:00 PM Eastern)


def _feed_hidden() -> set:
    return {h.strip().lower() for h in (os.environ.get("FEED_HIDE") or _env_file_value("/opt/ferzan/.env", "FEED_HIDE")
                                        or "").split(",") if h.strip()}


def _ferzan_block(c) -> dict:
    """FERZAN before launch: just the time. After the launch is announced: address, price, market cap, progress, burned."""
    import json as _json

    out = {"launch_at": _FERZAN_LAUNCH_AT, "live": False}
    try:
        st = _json.loads(_Path(_FLAGSHIP_STATE).read_text())
    except Exception:
        st = {}
    mint = str(st.get("mint") or "") if st.get("announced") else ""  # stays private until it is announced
    if not mint:
        return out
    out.update(live=True, token=mint, url=f"https://ferzan-factory.com/coin/solana/{mint}")
    try:
        fw = _json.loads(_Path(_FLYWHEEL_STATE).read_text())
        t = fw.get("totals") or {}
        out["burned"] = int(t.get("burned_raw") or 0) / 1e6
        out["bought_sol"] = float(t.get("bought_sol") or 0)
    except Exception:
        out["burned"], out["bought_sol"] = 0.0, 0.0
    if c is not None:
        r = c.execute("SELECT * FROM curves WHERE chain = 'solana' AND token = ?", (mint,)).fetchone()
        if r:
            usd = _native_usd("solana")
            grad = int(r["grad_target"] or 0)
            out.update(price_usd=(r["price"] or 0) * usd, mcap_usd=(r["mcap"] or 0) * usd, graduated=bool(r["graduated"]),
                       progress=100.0 if r["graduated"] else (min(100.0, int(r["real_eth"] or 0) * 100.0 / grad) if grad else 0.0),
                       vol_native=r["volume"] or 0, trades=r["trades"] or 0)
    return out


@app.get("/api/pulse")
def pulse():
    """Everything the website needs to feel live, in one cached call: the last trades across all chains (Solana curves
    report per-poll moves), coins that graduated in the last 24 hours, and the FERZAN hero block."""
    now = time.time()
    if _PULSE["v"] is not None and now - _PULSE["t"] < 5:
        return _PULSE["v"]
    hide = _feed_hidden()
    tape, grads = [], []
    c = _idx_db()
    try:
        if c is not None:
            since = int(now) - 3600
            for r in c.execute(
                    "SELECT t.chain, t.ts, t.trader, t.is_buy, t.native, t.tokens, cv.token, cv.symbol, cv.curve FROM trades t "
                    "JOIN curves cv ON cv.chain = t.chain AND cv.curve = t.curve WHERE t.ts > ? ORDER BY t.ts DESC LIMIT 40", (since,)):
                if str(r["token"]).lower() in hide:
                    continue
                usd = _native_usd(r["chain"])
                tape.append({"chain": r["chain"], "ts": r["ts"], "side": "buy" if r["is_buy"] else "sell", "symbol": r["symbol"],
                             "token": r["token"], "native": round(float(r["native"] or 0), 6), "unit": _NATIVE_SYM.get(r["chain"], ""),
                             "usd": round(float(r["native"] or 0) * usd, 2), "who": (r["trader"] or "")[:4] + "…" + (r["trader"] or "")[-4:],
                             "url": _trade_url(r["chain"], r["curve"], r["token"])})
            try:  # Solana curves: one row per poll with the SOL that moved; direction from the price change
                if c.execute("SELECT 1 FROM trades WHERE chain = 'solana' AND ts > ? LIMIT 1", (since,)).fetchone():
                    raise LookupError("the Solana trade stream is recording real trades")
                for r in c.execute(
                        "SELECT v.ts, v.pool, v.vol, v.trades, cv.token, cv.symbol FROM sol_vol v JOIN curves cv ON cv.chain = 'solana' "
                        "AND cv.curve = v.pool WHERE v.ts > ? ORDER BY v.ts DESC LIMIT 20", (since,)):
                    if str(r["token"]).lower() in hide:
                        continue
                    px = c.execute("SELECT price FROM sol_px WHERE pool = ? AND ts <= ? ORDER BY ts DESC LIMIT 2", (r["pool"], r["ts"])).fetchall()
                    up = len(px) < 2 or float(px[0][0] or 0) >= float(px[1][0] or 0)
                    usd = _native_usd("solana")
                    tape.append({"chain": "solana", "ts": r["ts"], "side": "buy" if up else "sell", "symbol": r["symbol"], "token": r["token"],
                                 "native": round(float(r["vol"] or 0), 4), "unit": "SOL", "usd": round(float(r["vol"] or 0) * usd, 2),
                                 "who": f"{int(r['trades'] or 0)} trade{'s' if int(r['trades'] or 0) != 1 else ''}",
                                 "url": _trade_url("solana", r["pool"], r["token"])})
            except Exception:
                pass  # no Solana tables yet
            tape.sort(key=lambda x: x["ts"], reverse=True)
            tape = tape[:30]
            launch_rows = _launch_rows_by_curve()
            for r in c.execute("SELECT * FROM curves WHERE graduated = 1 AND grad_ts > ? ORDER BY grad_ts DESC LIMIT 5", (int(now) - 86400,)):
                if str(r["token"]).lower() in hide:
                    continue
                grads.append({"chain": r["chain"], "token": r["token"], "symbol": r["symbol"], "name": r["name"], "ts": r["grad_ts"],
                              "image": (launch_rows.get(r["curve"]) or launch_rows.get(str(r["curve"]).lower()) or {}).get("image", ""),
                              "raised": round(float(r["grad_native"] or 0), 4), "unit": _NATIVE_SYM.get(r["chain"], ""),
                              "url": _trade_url(r["chain"], r["curve"], r["token"])})
        ferzan = _ferzan_block(c)
        stats = {"graduated": 0, "curves": 0}
        if c is not None:
            row = c.execute("SELECT COUNT(*), COALESCE(SUM(graduated), 0) FROM curves").fetchone()
            stats = {"curves": int(row[0] or 0), "graduated": int(row[1] or 0)}
    finally:
        if c is not None:
            c.close()
    with db._get_conn() as conn:
        lr = conn.execute("SELECT COUNT(*), COUNT(DISTINCT chain) FROM launch_requests WHERE status = 'confirmed' "
                          "AND result_token_address IS NOT NULL").fetchone()
    stats.update(launches=int(lr[0] or 0), chains_used=int(lr[1] or 0), chains=8)
    out = {"now": int(now), "tape": tape, "graduations": grads, "ferzan": ferzan, "stats": stats}
    _PULSE.update(t=now, v=out)
    return out


# ---- SHARE_CARDS: a picture for every coin link shared on Telegram / X (og:image) ----
_OG_CACHE: dict = {}


@app.get("/api/og/{chain}/{token}.png")
def og_card(chain: str, token: str):
    """1200x630 PNG: logo, name, ticker, market cap, graduation progress, chain. Cached 5 minutes."""
    from fastapi.responses import Response
    from io import BytesIO

    if not _re.fullmatch(r"[a-z]{2,12}", chain or "") or not _re.fullmatch(r"[0-9A-Za-z_-]{20,70}", token or ""):
        raise HTTPException(404, "not found")
    key = f"{chain}:{token}"
    hit = _OG_CACHE.get(key)
    if hit and time.time() - hit[0] < 300:
        return Response(hit[1], media_type="image/png", headers={"Cache-Control": "public, max-age=300"})
    name, sym, mcap, prog, grad, image = "Ferzan coin", "", 0.0, None, False, ""
    r = None
    c = _idx_db()
    try:
        r = c.execute("SELECT * FROM curves WHERE chain = ? AND (token = ? OR token = ? OR curve = ? OR curve = ?)",
                      (chain, token, token.lower(), token, token.lower())).fetchone() if c else None  # coin pages use either
        if r:
            name, sym, grad = r["name"] or name, r["symbol"] or "", bool(r["graduated"])
            mcap = (r["mcap"] or 0) * _native_usd(chain)
            g = int(r["grad_target"] or 0)
            prog = 100.0 if grad else (min(100.0, int(r["real_eth"] or 0) * 100.0 / g) if g else None)
            image = (_launch_rows_by_curve().get(r["curve"]) or _launch_rows_by_curve().get(str(r["curve"]).lower()) or {}).get("image", "")
    finally:
        if c is not None:
            c.close()
    if not r:
        with db._get_conn() as conn:
            row = conn.execute("SELECT name, symbol, image_url FROM launch_requests WHERE status = 'confirmed' AND "
                               "(result_token_address = ? OR LOWER(result_token_address) = ?) LIMIT 1", (token, token.lower())).fetchone()
        if row:
            name, sym, image = row[0] or name, row[1] or "", row[2] or ""
    png = _draw_card(name, sym, chain, mcap, prog, grad, image)
    _OG_CACHE[key] = (time.time(), png)
    if len(_OG_CACHE) > 400:
        _OG_CACHE.clear()
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


_SHARE_EVM = {"base", "bsc", "ethereum", "robinhood", "arc"}


@app.get("/api/share/{chain}/{token}")
def share_page(chain: str, token: str, w: str = ""):
    """Share link for a coin. Telegram / X / Discord read the coin card from this page (the website's own
    host replaces per-page share tags); people who open it are sent straight on to the coin page."""
    import html as _html
    from fastapi.responses import HTMLResponse

    if not _re.fullmatch(r"[a-z]{2,12}", chain or "") or not _re.fullmatch(r"[0-9A-Za-z_-]{20,70}", token or ""):
        raise HTTPException(404, "not found")
    name, sym, path = "", "", ""
    c = _idx_db()
    try:
        r = c.execute("SELECT name, symbol, curve, token FROM curves WHERE chain = ? AND (token = ? OR token = ? OR curve = ? OR curve = ?)",
                      (chain, token, token.lower(), token, token.lower())).fetchone() if c else None
    finally:
        if c is not None:
            c.close()
    if r:
        name, sym = r["name"] or "", r["symbol"] or ""
        if chain == "solana":
            path = f"/coin/solana/{r['token']}"
        elif chain in _SHARE_EVM:
            path = f"/coin/{chain}/{str(r['curve']).lower()}"
        else:
            path = f"/token/{chain}/{r['token']}"
    else:
        with db._get_conn() as conn:
            row = conn.execute("SELECT name, symbol FROM launch_requests WHERE status = 'confirmed' AND chain = ? AND "
                               "(result_token_address = ? OR LOWER(result_token_address) = ?) LIMIT 1",
                               (chain, token, token.lower())).fetchone()
        if row:
            name, sym = row[0] or "", row[1] or ""
    if not path:
        path = (f"/coin/solana/{token}" if chain == "solana" else f"/token/{chain}/{token}" if chain in ("tron", "ton", "arc")
                else f"/c/{chain}/{token.lower()}")
    site = "https://ferzan-factory.com" + path
    img = f"https://launch.ferzaneco.com/api/og/{chain}/{token}.png"
    title = f"${sym} · {name} on Ferzan" if sym else "Trade it on Ferzan Factory"
    desc = "Live chart, bonding curve and creator score. Trade it on Ferzan Factory or in the Ferzan Trade Bot."
    if w and _re.fullmatch(r"0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,48}|[EUk]Q[A-Za-z0-9_-]{46}", w):
        try:  # a trader's profit card instead of the coin card
            p = _pnl(chain, token, w)
        except Exception:
            p = {}
        if p.get("traded"):
            img = f"https://launch.ferzaneco.com/api/pnl/{chain}/{token}/{w}.png"
            pct = p["pnl_pct"]
            title = f"{'+' if pct >= 0 else ''}{pct:,.0f}% on ${p['symbol']} · Ferzan"
    e = lambda v: _html.escape(v, quote=True)  # noqa: E731
    body = (f'<!doctype html><html><head><meta charset="utf-8"><title>{e(title)}</title>'
            f'<meta property="og:type" content="website"><meta property="og:site_name" content="Ferzan Factory">'
            f'<meta property="og:title" content="{e(title)}"><meta property="og:description" content="{e(desc)}">'
            f'<meta property="og:url" content="{e(site)}"><meta property="og:image" content="{e(img)}">'
            f'<meta property="og:image:width" content="1200"><meta property="og:image:height" content="630">'
            f'<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{e(title)}">'
            f'<meta name="twitter:image" content="{e(img)}"><meta http-equiv="refresh" content="0; url={e(site)}">'
            f'<link rel="canonical" href="{e(site)}"></head><body style="background:#07090b;color:#f4f7f7;font-family:sans-serif">'
            f'<p><a style="color:#3ee0e6" href="{e(site)}">Open {e(title)}</a></p>'
            f'<script>location.replace({_json_str(site)})</script></body></html>')
    return HTMLResponse(body, headers={"Cache-Control": "public, max-age=300"})


def _json_str(v: str) -> str:
    import json as _json

    return _json.dumps(v).replace("<", "\\u003c")


def _draw_card(name: str, sym: str, chain: str, mcap: float, prog, grad: bool, image: str) -> bytes:
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    def font(size, bold=False):
        for d in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu"):
            try:
                return ImageFont.truetype(f"{d}/DejaVuSans{'-Bold' if bold else ''}.ttf", size)
            except OSError:
                continue
        return ImageFont.load_default()

    W, H = 1200, 630
    bg, cyan, fg, muted, line = (7, 9, 11), (62, 224, 230), (244, 247, 247), (147, 164, 167), (28, 42, 44)
    im = Image.new("RGB", (W, H), bg)
    glow = Image.new("RGB", (W, H), bg)
    gd = ImageDraw.Draw(glow)
    for i in range(18):  # soft cyan glow at the top, like the site
        a = 1 - i / 18
        gd.ellipse((W * 0.15 - i * 30, -420 - i * 10, W * 0.85 + i * 30, 180 + i * 6), fill=tuple(int(bg[k] + (cyan[k] - bg[k]) * 0.05 * a) for k in range(3)))
    im = Image.blend(im, glow, 1.0)
    d = ImageDraw.Draw(im)
    logo = None
    if image.startswith("https://") or image.startswith("/api/media/"):
        try:
            src = image if image.startswith("https://") else f"http://127.0.0.1:8000{image}"
            if "/api/media/" in src:
                src = "http://127.0.0.1:8000/api/media/" + src.rsplit("/api/media/", 1)[1]
            logo = Image.open(BytesIO(requests.get(src, timeout=6).content)).convert("RGB").resize((220, 220))
        except Exception:
            logo = None
    x0 = 80
    if logo:
        mask = Image.new("L", (220, 220), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 220, 220), 36, fill=255)
        im.paste(logo, (80, 150), mask)
        x0 = 340
    else:
        d.rounded_rectangle((80, 150, 300, 370), 36, fill=(17, 24, 26), outline=line, width=2)
        t = (sym or "?")[:3].upper()
        f = font(80, True)
        tw = d.textlength(t, font=f)
        d.text((190 - tw / 2, 210), t, font=f, fill=cyan)
        x0 = 340
    title = (name or "Ferzan coin")[:32]
    size = 64
    while size > 34 and d.textlength(title, font=font(size, True)) > W - x0 - 80:
        size -= 4
    d.text((x0, 150 + (64 - size) // 2), title, font=font(size, True), fill=fg)
    d.text((x0, 232), f"${sym}"[:14] if sym else "", font=font(44, True), fill=cyan)
    chain_name = {"solana": "Solana", "base": "Base", "bsc": "BNB Chain", "ethereum": "Ethereum", "robinhood": "Robinhood Chain",
                  "arc": "Arc", "tron": "Tron", "ton": "TON"}.get(chain, chain)
    d.text((x0, 300), chain_name, font=font(32), fill=muted)
    if mcap > 0:
        m = f"${mcap / 1e6:.2f}M" if mcap >= 1e6 else f"${mcap / 1e3:.1f}K" if mcap >= 1e3 else f"${mcap:,.0f}"
        d.text((80, 420), "Market cap", font=font(28), fill=muted)
        d.text((80, 455), m, font=font(56, True), fill=fg)
    if prog is not None:
        bx, by, bw = 520, 470, 600
        d.text((bx, 420), "Graduated" if grad else f"{prog:.0f}% to graduation", font=font(28), fill=cyan if grad else muted)
        d.rounded_rectangle((bx, by, bx + bw, by + 24), 12, fill=line)
        d.rounded_rectangle((bx, by, bx + max(24, int(bw * min(100.0, prog) / 100)), by + 24), 12, fill=cyan)
    d.text((80, 560), "ferzan-factory.com", font=font(30, True), fill=fg)
    d.text((W - 80 - d.textlength("Trade it on Ferzan", font=font(30)), 560), "Trade it on Ferzan", font=font(30), fill=cyan)
    buf = BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


# ---- CREATOR_SCORE: a plain-language trust check on any coin launched through Ferzan ----
_SCORE_CACHE: dict = {}


def _norm_addr(v: str) -> str:
    v = (v or "").strip()
    return v.lower() if v.lower().startswith("0x") else v


@app.get("/api/creator-score/{token}")
def creator_score(token: str):
    """Who launched this coin, what else they launched, and (on curves) what the dev bought and sold.
    Only for coins launched through Ferzan; anything else answers {"found": false}."""
    tok = _norm_addr(token)
    if not (_re.fullmatch(r"0x[0-9a-f]{40}", tok) or _re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{32,48}|[EUk]Q[A-Za-z0-9_-]{46}", tok)):
        return {"found": False}
    hit = _SCORE_CACHE.get(tok)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    import json as _json
    from datetime import datetime as _dt

    def ts(v):
        try:
            return int(_dt.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0

    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT id, chain, mode, wallet_address, telegram_user_id, created_at, result_token_address, extra_params, total_supply "
            "FROM launch_requests WHERE status = 'confirmed' AND result_token_address IS NOT NULL").fetchall()
    me = next((r for r in rows if _norm_addr(r["result_token_address"]) == tok), None)
    if not me:
        out = {"found": False}
        _SCORE_CACHE[tok] = (time.time(), out)
        return out
    wallet, uid = _norm_addr(me["wallet_address"] or ""), int(me["telegram_user_id"] or 0)
    mine = [r for r in rows if (wallet and _norm_addr(r["wallet_address"] or "") == wallet) or (uid > 0 and int(r["telegram_user_id"] or 0) == uid)]
    now, born = time.time(), ts(me["created_at"])
    others = [r for r in mine if r["id"] != me["id"]]
    day = [r for r in mine if abs(ts(r["created_at"]) - born) < 86400]
    grads = best = 0
    dev_buy = dev_sell = 0.0
    supply_whole = 0.0
    curve = ""
    try:
        curve = (_json.loads(me["extra_params"] or "{}").get("curve_address") or "").strip()
    except ValueError:
        pass
    c = _idx_db()
    if c is not None:
        try:
            toks = [_norm_addr(r["result_token_address"]) for r in others]
            for t_ in toks[:200]:
                g = c.execute("SELECT graduated, mcap, chain FROM curves WHERE token = ? OR token = ?", (t_, t_.lower())).fetchone()
                if g:
                    grads += 1 if g["graduated"] else 0
                    best = max(best, (g["mcap"] or 0) * _native_usd(g["chain"]))
            if curve and me["mode"] == "bonding_curve":
                cv = c.execute("SELECT total_supply FROM curves WHERE curve = ? OR curve = ?", (curve, curve.lower())).fetchone()
                supply_whole = int(cv["total_supply"] or 0) / 1e18 if cv else 0.0
                for t in c.execute("SELECT is_buy, tokens FROM trades WHERE (curve = ? OR curve = ?) AND (trader = ? OR trader = ?)",
                                   (curve, curve.lower(), wallet, wallet.lower())):
                    if t["is_buy"]:
                        dev_buy += float(t["tokens"] or 0)
                    else:
                        dev_sell += float(t["tokens"] or 0)
        finally:
            c.close()
    score, lines = 100, []
    if len(day) >= 10:
        score -= 50
        lines.append(f"🚩 Launched {len(day)} coins within a day of this one")
    elif len(day) >= 3:
        score -= 25
        lines.append(f"⚠️ Launched {len(day)} coins within a day of this one")
    if others:
        lines.append(f"🧑‍💻 {len(others) + 1} launches by this creator · {grads} graduated")
        if grads:
            score += 10
        elif len(others) >= 3:
            score -= 15
    else:
        lines.append("🆕 First launch by this creator")
    hold_pct = None
    if me["mode"] == "bonding_curve" and supply_whole > 0:
        held = max(0.0, dev_buy - dev_sell)
        hold_pct = held * 100 / supply_whole
        if dev_buy > 0 and dev_sell >= dev_buy * 0.5:
            score -= 30
            lines.append(f"🚩 Dev sold {dev_sell * 100 / dev_buy:.0f}% of what they bought")
        elif dev_sell > 0:
            score -= 10
            lines.append(f"⚠️ Dev has sold some ({dev_sell * 100 / dev_buy:.0f}% of their buy)" if dev_buy else "⚠️ Dev has sold")
        if hold_pct >= 20:
            score -= 30
            lines.append(f"🚩 Dev holds {hold_pct:.1f}% of the supply")
        elif hold_pct >= 10:
            score -= 15
            lines.append(f"⚠️ Dev holds {hold_pct:.1f}% of the supply")
        elif dev_buy > 0:
            lines.append(f"✅ Dev holds {hold_pct:.1f}% of the supply")
        else:
            lines.append("✅ No dev buy")
    elif me["mode"] == "plain":
        lines.append("ℹ️ Standard coin: the creator received the whole supply at launch")
    badge = ""
    try:  # FERZAN holder badge for the creator's Solana wallet (information only, the score is unchanged)
        import ferzan_perks as _fp
        badge = _fp.perks(me["wallet_address"] or "").get("badge") or ""
    except Exception:
        badge = ""
    if badge:
        lines.append(f"{badge}: the creator holds FERZAN")
    score = max(0, min(100, score))
    if any(x.startswith("🚩") for x in lines):
        score = min(score, 59)  # a red flag always means at least "Caution", whatever the history
    label = "Good" if score >= 80 else "Caution" if score >= 50 else "Risky"
    out = {"found": True, "score": score, "label": label, "lines": lines, "creator": me["wallet_address"] or "",
           "launches": len(others) + 1, "graduated_before": grads, "best_prior_mcap_usd": best,
           "launches_same_day": len(day), "dev_bought": dev_buy, "dev_sold": dev_sell, "dev_hold_pct": hold_pct,
           "chain": me["chain"], "mode": me["mode"], "ferzan_badge": badge}
    _SCORE_CACHE[tok] = (time.time(), out)
    return out


@app.get("/api/ferzan-perks/{wallet}")
def ferzan_perks_for(wallet: str):
    """What this Solana wallet gets for holding FERZAN. Before FERZAN is announced: active = false."""
    import ferzan_perks as _fp

    p = _fp.perks(wallet)
    base = int(os.environ.get("LAUNCH_FEE_LAMPORTS") or "50000000")
    fee, _ = _fp.launch_fee_lamports(wallet, base) if p.get("active") else (base, "")
    return dict(p, launch_fee_sol=base / 1e9, your_launch_fee_sol=fee / 1e9,
                tiers=[{"tier": "holder", "min": p["holder_min"], "badge": "🔷 FERZAN holder", "launch_fee_off_pct": 50},
                       {"tier": "whale", "min": p["whale_min"], "badge": "🐋 FERZAN whale", "launch_fee_off_pct": 100}])


# ---- BATCH_D: live push, holders + safety, profit cards, search ----
_CHAIN_NAME = {"solana": "Solana", "base": "Base", "bsc": "BNB Chain", "ethereum": "Ethereum", "robinhood": "Robinhood Chain",
               "arc": "Arc", "tron": "Tron", "ton": "TON"}
_SITE_EVM = {"base", "bsc", "ethereum", "robinhood", "arc"}
_ADDR_ANY = r"0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,48}|[EUk]Q[A-Za-z0-9_-]{46}"


def _site_path(chain: str, token: str, curve: str = "") -> str:
    if chain == "solana":
        return f"/coin/solana/{token}"
    if chain in _SITE_EVM and curve:
        return f"/coin/{chain}/{curve.lower()}"
    if chain in ("tron", "ton", "arc"):
        return f"/token/{chain}/{token}"
    return f"/c/{chain}/{token.lower()}"


def _coin_row(c, chain: str, token: str):
    if c is None:
        return None
    return c.execute("SELECT * FROM curves WHERE chain = ? AND (token = ? OR token = ? OR curve = ? OR curve = ?)",
                     (chain, token, token.lower(), token, token.lower())).fetchone()


def _progress(r) -> float:
    g = int(r["grad_target"] or 0)
    return 100.0 if r["graduated"] else (min(100.0, int(r["real_eth"] or 0) * 100.0 / g) if g else 0.0)


def _supply_whole(r) -> float:
    return (int(r["total_supply"] or 0) / 1e18) or 1_000_000_000.0


# ---------------------------------------------------------------- live push (server-sent events)
import asyncio as _asyncio  # noqa: E402
import json as _jsonmod  # noqa: E402
from fastapi.responses import StreamingResponse as _Streaming  # noqa: E402

_LIVE = {"subs": set(), "task": None, "trade": None, "curve": None, "grad": None}
_LIVE_MAX = int(os.environ.get("LIVE_MAX_CLIENTS") or 3000)


def _live_poll() -> list:
    """New trades, new coins and graduations since the last look (runs in a worker thread, read-only)."""
    c = _idx_db()
    if c is None:
        return []
    out = []
    try:
        if _LIVE["trade"] is None:  # first look: start from now
            _LIVE["trade"] = int(c.execute("SELECT COALESCE(MAX(rowid), 0) FROM trades").fetchone()[0])
            _LIVE["curve"] = int(c.execute("SELECT COALESCE(MAX(rowid), 0) FROM curves").fetchone()[0])
            _LIVE["grad"] = int(c.execute("SELECT COALESCE(MAX(grad_ts), 0) FROM curves").fetchone()[0])
            return []
        hide = _feed_hidden()
        for r in c.execute("SELECT t.rowid AS rid, t.chain, t.ts, t.tx, t.trader, t.is_buy, t.native, t.tokens, cv.token, cv.curve, "
                           "cv.symbol, cv.name, cv.mcap, cv.trades AS n, cv.graduated, cv.grad_target, cv.real_eth FROM trades t "
                           "JOIN curves cv ON cv.chain = t.chain AND cv.curve = t.curve WHERE t.rowid > ? ORDER BY t.rowid LIMIT 300",
                           (_LIVE["trade"],)).fetchall():
            _LIVE["trade"] = max(_LIVE["trade"], int(r["rid"]))
            if str(r["token"]).lower() in hide:
                continue
            usd = _native_usd(r["chain"])
            who = r["trader"] or ""
            out.append({"type": "trade", "chain": r["chain"], "token": r["token"], "curve": r["curve"], "symbol": r["symbol"],
                        "ts": r["ts"], "tx": r["tx"], "side": "buy" if r["is_buy"] else "sell", "native": round(float(r["native"] or 0), 6),
                        "unit": _NATIVE_SYM.get(r["chain"], ""), "usd": round(float(r["native"] or 0) * usd, 2), "trader": who,
                        "who": (who[:4] + "…" + who[-4:]) if who else "", "tokens": float(r["tokens"] or 0),
                        "mcap_usd": round(float(r["mcap"] or 0) * usd, 2), "progress": round(_progress(r), 2), "trades": int(r["n"] or 0),
                        "graduated": bool(r["graduated"]), "path": _site_path(r["chain"], r["token"], r["curve"])})
        for r in c.execute("SELECT rowid AS rid, * FROM curves WHERE rowid > ? ORDER BY rowid LIMIT 50", (_LIVE["curve"],)).fetchall():
            _LIVE["curve"] = max(_LIVE["curve"], int(r["rid"]))
            if str(r["token"]).lower() in hide:
                continue
            out.append({"type": "launch", "chain": r["chain"], "token": r["token"], "curve": r["curve"], "symbol": r["symbol"],
                        "name": r["name"], "ts": r["launched_ts"] or int(time.time()), "path": _site_path(r["chain"], r["token"], r["curve"])})
        for r in c.execute("SELECT * FROM curves WHERE graduated = 1 AND grad_ts > ? ORDER BY grad_ts LIMIT 20", (_LIVE["grad"],)).fetchall():
            _LIVE["grad"] = max(_LIVE["grad"], int(r["grad_ts"] or 0))
            if str(r["token"]).lower() in hide:
                continue
            out.append({"type": "grad", "chain": r["chain"], "token": r["token"], "curve": r["curve"], "symbol": r["symbol"],
                        "name": r["name"], "ts": r["grad_ts"], "raised": round(float(r["grad_native"] or 0), 4),
                        "unit": _NATIVE_SYM.get(r["chain"], ""), "path": _site_path(r["chain"], r["token"], r["curve"])})
    finally:
        c.close()
    return out


async def _live_loop():
    while True:
        await _asyncio.sleep(1.0)
        if not _LIVE["subs"]:
            continue
        try:
            events = await _asyncio.to_thread(_live_poll)
        except Exception as e:
            print(f"LIVE_POLL_FAILED: {str(e)[:160]}")
            await _asyncio.sleep(3)
            continue
        for ev in events:
            line = f"event: {ev['type']}\ndata: {_jsonmod.dumps(ev, separators=(',', ':'))}\n\n"
            for q in list(_LIVE["subs"]):
                try:
                    q.put_nowait(line)
                except _asyncio.QueueFull:
                    pass  # a slow client misses a few; its page still refreshes on its own timer


@app.get("/api/stream")
async def live_stream(request: Request):
    """Server-sent events: every trade, new coin and graduation on Ferzan as it is indexed (all chains)."""
    if len(_LIVE["subs"]) >= _LIVE_MAX:
        raise HTTPException(503, "live feed is full, the page refreshes on its own")
    if _LIVE["task"] is None or _LIVE["task"].done():
        _LIVE["task"] = _asyncio.create_task(_live_loop())
    q: _asyncio.Queue = _asyncio.Queue(maxsize=400)
    _LIVE["subs"].add(q)

    async def gen():
        try:
            yield "retry: 4000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    yield await _asyncio.wait_for(q.get(), timeout=15)
                except _asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            _LIVE["subs"].discard(q)

    return _Streaming(gen(), media_type="text/event-stream",
                      headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


@app.get("/api/stream-status")
def live_status():
    return {"clients": len(_LIVE["subs"]), "max": _LIVE_MAX, "running": bool(_LIVE["task"] and not _LIVE["task"].done())}


# ---------------------------------------------------------------- holders + safety
_HOLD_CACHE: dict = {}


def _sol_largest(mint: str) -> list:
    """[(owner, whole tokens)] for the 20 biggest token accounts of a Solana coin."""
    r = _sol_rpc_sync("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}], timeout=12)
    accts = [(a["address"], float(a.get("uiAmount") or 0)) for a in ((r.get("result") or {}).get("value") or [])]
    if not accts:
        return []
    m = _sol_rpc_sync("getMultipleAccounts", [[a for a, _ in accts], {"encoding": "jsonParsed"}], timeout=12)
    vals = (m.get("result") or {}).get("value") or []
    out = []
    for (addr, amt), v in zip(accts, vals):
        owner = ((((v or {}).get("data") or {}).get("parsed") or {}).get("info") or {}).get("owner") or addr
        out.append((owner, amt))
    return out


@app.get("/api/holders/{chain}/{token}")
def holders(chain: str, token: str):
    """Top holders, dev share, snipers (bought in the first 15 s) and launch-block bundles for a Ferzan curve coin."""
    if not _re.fullmatch(r"[a-z]{2,12}", chain or "") or not _re.fullmatch(_ADDR_ANY, token or ""):
        raise HTTPException(404, "not found")
    key = f"{chain}:{token}"
    hit = _HOLD_CACHE.get(key)
    if hit and time.time() - hit[0] < 20:
        return hit[1]
    c = _idx_db()
    try:
        r = _coin_row(c, chain, token)
        if not r:
            return {"found": False}
        trades = c.execute("SELECT ts, block, trader, is_buy, tokens FROM trades WHERE chain = ? AND curve = ? ORDER BY ts, block, log_index",
                           (chain, r["curve"])).fetchall()
    finally:
        if c is not None:
            c.close()
    supply = _supply_whole(r)
    creator = _norm_addr(r["creator"] or "")
    launched = int(r["launched_ts"] or (trades[0]["ts"] if trades else 0))
    first_block = min((t["block"] for t in trades), default=None)
    net, bought, sniper, bundle = {}, {}, set(), set()
    for t in trades:
        w = _norm_addr(t["trader"] or "")
        amt = float(t["tokens"] or 0)
        net[w] = net.get(w, 0.0) + (amt if t["is_buy"] else -amt)
        if t["is_buy"]:
            bought[w] = bought.get(w, 0.0) + amt
            if w != creator and launched and t["ts"] - launched <= 15:
                sniper.add(w)
            if w != creator and first_block is not None and t["block"] == first_block:
                bundle.add(w)
    source = "trades"
    rows = []
    if chain == "solana":
        try:
            big = _sol_largest(r["token"])
        except Exception:
            big = []
        if big:
            source = "chain"
            pa = ""
            from solders.pubkey import Pubkey as _Pk
            try:
                pa = str(_Pk.find_program_address([b"pool_authority"], _Pk.from_string(_DBC_PROGRAM))[0])
            except Exception:
                pass
            for owner, amt in big:
                tag = "curve" if owner == pa else ""
                if not tag:
                    try:  # program-owned (pools, lockers, vesting escrows) addresses are off the ed25519 curve
                        tag = "" if _Pk.from_string(owner).is_on_curve() else "program"
                    except Exception:
                        pass
                rows.append((owner, amt, tag))
    if not rows:
        rows = [(w, a, "") for w, a in net.items() if a > supply * 1e-9]
        if not r["graduated"]:
            left = max(0.0, supply - sum(a for _, a, _ in rows))
            rows.append(("curve", left, "curve"))
    rows.sort(key=lambda x: x[1], reverse=True)
    items = []
    for w, a, tag in rows[:20]:
        wn = _norm_addr(w)
        tags = [tag] if tag else []
        if wn == creator:
            tags.append("dev")
        if wn in sniper:
            tags.append("sniper")
        if wn in bundle:
            tags.append("bundle")
        items.append({"wallet": w, "short": (w[:4] + "…" + w[-4:]) if len(w) > 12 else w, "amount": a,
                      "pct": round(a * 100 / supply, 3), "tags": tags})
    people = [i for i in items if "curve" not in i["tags"] and "program" not in i["tags"]]
    held = lambda ws: sum(max(0.0, net.get(w, 0.0)) for w in ws)  # noqa: E731
    out = {
        "found": True, "chain": chain, "token": r["token"], "symbol": r["symbol"], "source": source, "graduated": bool(r["graduated"]),
        "holders": items, "holder_count": sum(1 for v in net.values() if v > supply * 1e-9),
        "top10_pct": round(sum(i["pct"] for i in people[:10]), 2),
        "dev_pct": round(max(0.0, net.get(creator, 0.0)) * 100 / supply, 2) if creator else None,
        "snipers": {"wallets": len(sniper), "bought_pct": round(sum(bought.get(w, 0) for w in sniper) * 100 / supply, 2),
                    "holding_pct": round(held(sniper) * 100 / supply, 2)},
        "bundle": {"wallets": len(bundle), "bought_pct": round(sum(bought.get(w, 0) for w in bundle) * 100 / supply, 2),
                   "holding_pct": round(held(bundle) * 100 / supply, 2)},
        "trades_seen": len(trades),
        "note": ("Snipers and bundles are counted from Ferzan's own trade records." if trades else
                 "Trade details start with the new Solana trade stream; older trades are not in the snipers and bundles numbers."),
    }
    _HOLD_CACHE[key] = (time.time(), out)
    if len(_HOLD_CACHE) > 500:
        _HOLD_CACHE.clear()
    return out


# ---------------------------------------------------------------- profit / loss
def _pnl(chain: str, token: str, wallet: str) -> dict:
    c = _idx_db()
    try:
        r = _coin_row(c, chain, token)
        if not r:
            return {"found": False}
        w = _norm_addr(wallet)
        rows = c.execute("SELECT is_buy, native, tokens, ts FROM trades WHERE chain = ? AND curve = ? AND (trader = ? OR trader = ?)",
                         (chain, r["curve"], wallet, w)).fetchall()
    finally:
        if c is not None:
            c.close()
    if not rows:
        return {"found": True, "traded": False, "symbol": r["symbol"]}
    spent = sum(float(x["native"] or 0) for x in rows if x["is_buy"])
    got = sum(float(x["native"] or 0) for x in rows if not x["is_buy"])
    bought = sum(float(x["tokens"] or 0) for x in rows if x["is_buy"])
    sold = sum(float(x["tokens"] or 0) for x in rows if not x["is_buy"])
    held = max(0.0, bought - sold)
    price = float(r["price"] or 0)
    value = held * price
    pnl = got + value - spent
    usd = _native_usd(chain)
    supply = _supply_whole(r)
    entry_mcap = (spent / bought) * supply * usd if bought else 0.0
    return {"found": True, "traded": True, "chain": chain, "token": r["token"], "symbol": r["symbol"], "name": r["name"],
            "unit": _NATIVE_SYM.get(chain, ""), "spent": spent, "received": got, "holding_tokens": held, "holding_value": value,
            "pnl": pnl, "pnl_usd": pnl * usd, "pnl_pct": (pnl * 100 / spent) if spent else 0.0,
            "entry_mcap_usd": entry_mcap, "mcap_usd": float(r["mcap"] or 0) * usd, "first_ts": min(x["ts"] for x in rows),
            "wallet_short": wallet[:4] + "…" + wallet[-4:]}


def _draw_pnl(p: dict, image: str) -> bytes:
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont

    def font(size, bold=False):
        for d in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu"):
            try:
                return ImageFont.truetype(f"{d}/DejaVuSans{'-Bold' if bold else ''}.ttf", size)
            except OSError:
                continue
        return ImageFont.load_default()

    W, H = 1200, 630
    up = p["pnl"] >= 0
    bg, cyan, sell, fg, muted = (7, 9, 11), (62, 224, 230), (224, 122, 104), (244, 247, 247), (147, 164, 167)
    tone = cyan if up else sell
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    for i in range(22):  # glow behind the number
        a = 1 - i / 22
        d.ellipse((380 - i * 26, 120 - i * 14, 1260 + i * 26, 520 + i * 14), fill=tuple(int(bg[k] + (tone[k] - bg[k]) * 0.035 * a) for k in range(3)))
    logo = None
    if image.startswith("https://") or image.startswith("/api/media/"):
        try:
            src = image if image.startswith("https://") else "http://127.0.0.1:8000/api/media/" + image.rsplit("/api/media/", 1)[1]
            logo = Image.open(BytesIO(requests.get(src, timeout=6).content)).convert("RGB").resize((120, 120))
        except Exception:
            logo = None
    if logo:
        mask = Image.new("L", (120, 120), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, 120, 120), 24, fill=255)
        im.paste(logo, (80, 70), mask)
    tx = 230 if logo else 80
    d.text((tx, 78), f"${p['symbol']}"[:14], font=font(58, True), fill=fg)
    d.text((tx, 150), f"{_CHAIN_NAME.get(p['chain'], p['chain'])} · {p['wallet_short']}", font=font(28), fill=muted)
    pct = p["pnl_pct"]
    big = f"{'+' if pct >= 0 else ''}{pct:,.0f}%" if abs(pct) >= 10 else f"{'+' if pct >= 0 else ''}{pct:,.1f}%"
    size = 170
    while size > 90 and d.textlength(big, font=font(size, True)) > W - 160:
        size -= 10
    d.text((80, 230), big, font=font(size, True), fill=tone)
    u = p["unit"]
    fmt = lambda v: f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 100 else f"{v:,.1f}"  # noqa: E731
    cap = lambda v: f"${v / 1e6:.2f}M" if v >= 1e6 else f"${v / 1e3:.1f}K" if v >= 1e3 else f"${v:,.0f}"  # noqa: E731
    cols = [("Put in", f"{fmt(p['spent'])} {u}"), ("Now worth", f"{fmt(p['received'] + p['holding_value'])} {u}"),
            ("Bought at", cap(p["entry_mcap_usd"]) + " mcap"), ("Now", cap(p["mcap_usd"]) + " mcap")]
    x = 80
    for label, val in cols:
        d.text((x, 460), label, font=font(24), fill=muted)
        d.text((x, 492), val, font=font(32, True), fill=fg)
        x += 265
    d.text((80, 568), "ferzan-factory.com", font=font(28, True), fill=fg)
    tag = "Trade it on Ferzan"
    d.text((W - 80 - d.textlength(tag, font=font(28)), 568), tag, font=font(28), fill=cyan)
    buf = BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


_PNL_CACHE: dict = {}


@app.get("/api/pnl/{chain}/{token}/{wallet}.png")
def pnl_card(chain: str, token: str, wallet: str):
    from fastapi.responses import Response

    if not _re.fullmatch(r"[a-z]{2,12}", chain or "") or not _re.fullmatch(_ADDR_ANY, token or "") or not _re.fullmatch(_ADDR_ANY, wallet or ""):
        raise HTTPException(404, "not found")
    key = f"{chain}:{token}:{wallet}"
    hit = _PNL_CACHE.get(key)
    if hit and time.time() - hit[0] < 120:
        return Response(hit[1], media_type="image/png", headers={"Cache-Control": "public, max-age=120"})
    p = _pnl(chain, token, wallet)
    if not p.get("traded"):
        raise HTTPException(404, "no trades by this wallet")
    image = ""
    with db._get_conn() as conn:
        row = conn.execute("SELECT image_url FROM launch_requests WHERE status = 'confirmed' AND (result_token_address = ? OR "
                           "LOWER(result_token_address) = ?) LIMIT 1", (p["token"], p["token"].lower())).fetchone()
    if row:
        image = row[0] or ""
    png = _draw_pnl(p, image)
    _PNL_CACHE[key] = (time.time(), png)
    if len(_PNL_CACHE) > 300:
        _PNL_CACHE.clear()
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=120"})


@app.get("/api/pnl/{chain}/{token}/{wallet}")
def pnl_json(chain: str, token: str, wallet: str):
    if not _re.fullmatch(r"[a-z]{2,12}", chain or "") or not _re.fullmatch(_ADDR_ANY, token or "") or not _re.fullmatch(_ADDR_ANY, wallet or ""):
        raise HTTPException(404, "not found")
    return _pnl(chain, token, wallet)


# ---------------------------------------------------------------- search
@app.get("/api/search")
def search(q: str = "", limit: int = 12):
    """Coins launched through Ferzan on any chain, by ticker, name or address."""
    q = (q or "").strip()[:64]
    limit = max(1, min(25, int(limit or 12)))
    if len(q) < 2:
        return {"items": []}
    hide = _feed_hidden()
    ql, like = q.lower(), f"%{q.lower()}%"
    items, seen = [], set()
    c = _idx_db()
    try:
        if c is not None:
            extra = _launch_rows_by_curve()
            for r in c.execute(
                    "SELECT * FROM curves WHERE lower(symbol) = ? OR lower(token) = ? OR lower(curve) = ? OR lower(symbol) LIKE ? "
                    "OR lower(name) LIKE ? ORDER BY (lower(symbol) = ?) DESC, graduated DESC, mcap DESC LIMIT ?",
                    (ql, ql, ql, like, like, ql, limit * 2)).fetchall():
                if str(r["token"]).lower() in hide:
                    continue
                usd = _native_usd(r["chain"])
                seen.add((r["chain"], str(r["token"]).lower()))
                items.append({"chain": r["chain"], "token": r["token"], "symbol": r["symbol"], "name": r["name"],
                              "image": (extra.get(r["curve"]) or {}).get("image", ""), "mcap_usd": round(float(r["mcap"] or 0) * usd, 2),
                              "progress": round(_progress(r), 1), "graduated": bool(r["graduated"]),
                              "path": _site_path(r["chain"], r["token"], r["curve"])})
    finally:
        if c is not None:
            c.close()
    with db._get_conn() as conn:
        for r in conn.execute(
                "SELECT chain, name, symbol, image_url, result_token_address FROM launch_requests WHERE status = 'confirmed' "
                "AND result_token_address IS NOT NULL AND (lower(symbol) = ? OR lower(result_token_address) = ? OR lower(symbol) LIKE ? "
                "OR lower(name) LIKE ?) ORDER BY created_at DESC LIMIT ?", (ql, ql, like, like, limit * 2)).fetchall():
            tok = r[4]
            if (r[0], str(tok).lower()) in seen or str(tok).lower() in hide:
                continue
            seen.add((r[0], str(tok).lower()))
            items.append({"chain": r[0], "token": tok, "symbol": r[2], "name": r[1], "image": r[3] or "", "mcap_usd": 0,
                          "progress": None, "graduated": False, "path": _site_path(r[0], tok, "")})
    items.sort(key=lambda i: (str(i["symbol"] or "").lower() != ql, -(i["mcap_usd"] or 0)))
    return {"items": items[:limit]}


# ---- BATCH_E: transparency, creator pages, follow alerts ----
_TRANS_CACHE: dict = {"t": 0.0, "v": None}


@app.get("/api/transparency")
def transparency():
    """Public numbers: every FERZAN buyback and burn with its transactions, fees paid to creators,
    and launches, graduations and volume per chain. Cached one minute."""
    import json as _json
    import sqlite3 as _sq

    now = time.time()
    if _TRANS_CACHE["v"] is not None and now - _TRANS_CACHE["t"] < 60:
        return _TRANS_CACHE["v"]
    burns, totals = [], {"claimed_sol": 0.0, "bought_sol": 0.0, "burned": 0.0, "forward_sol": 0.0}
    try:
        fw = _json.loads(_Path(_FLYWHEEL_STATE).read_text())
        t = fw.get("totals") or {}
        totals = {"claimed_sol": float(t.get("claimed_sol") or 0), "bought_sol": float(t.get("bought_sol") or 0),
                  "burned": int(t.get("burned_raw") or 0) / 1e6, "forward_sol": float(t.get("forward_sol") or 0)}
        for day, d in sorted((fw.get("days") or {}).items(), reverse=True):
            if not d.get("live"):
                continue  # plan-only runs sent nothing
            ok = lambda s: s if isinstance(s, str) and _re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{60,100}", s) else ""  # noqa: E731
            burns.append({"day": day, "claimed_sol": float(d.get("claimed_sol") or 0), "bought_sol": float(d.get("bought_sol") or 0),
                          "burned": int(d.get("burned_raw") or 0) / 1e6, "forward_sol": float(d.get("forward_sol") or 0),
                          "buy_tx": ok(d.get("buy_sig")), "burn_tx": ok(d.get("burn_sig")),
                          "claim_txs": [ok(x) for x in (d.get("claim_sigs") or []) if ok(x)][:10]})
    except Exception:
        pass
    chains: dict = {}
    creators_native: dict = {}
    trades_24h = traders_24h = trades_all = 0
    c = _idx_db()
    try:
        if c is not None:
            cols = {r[1] for r in c.execute("PRAGMA table_info(trades)")}
            for r in c.execute("SELECT chain, COUNT(*) AS n, SUM(graduated) AS g, SUM(volume) AS v FROM curves GROUP BY chain"):
                chains.setdefault(r["chain"], {}).update(curves=int(r["n"] or 0), graduated=int(r["g"] or 0), volume_native=float(r["v"] or 0))
            since = int(now) - 86400
            for r in c.execute("SELECT chain, COUNT(*) AS n, SUM(native) AS v FROM trades WHERE ts > ? GROUP BY chain", (since,)):
                chains.setdefault(r["chain"], {}).update(trades_24h=int(r["n"] or 0), volume_24h_native=float(r["v"] or 0))
            row = c.execute("SELECT COUNT(*), COUNT(DISTINCT trader) FROM trades WHERE ts > ?", (since,)).fetchone()
            trades_24h, traders_24h = int(row[0] or 0), int(row[1] or 0)
            trades_all = int(c.execute("SELECT COUNT(*) FROM trades").fetchone()[0] or 0)
            if "fee" in cols:  # creators get half of every curve trading fee (EVM and Tron curves record the exact fee)
                for r in c.execute("SELECT chain, SUM(fee) FROM trades WHERE fee IS NOT NULL AND chain != 'solana' GROUP BY chain"):
                    creators_native[r[0]] = float(r[1] or 0) * 0.5
    finally:
        if c is not None:
            c.close()
    with db._get_conn() as conn:
        for ch, n in conn.execute("SELECT chain, COUNT(*) FROM launch_requests WHERE status = 'confirmed' "
                                  "AND result_token_address IS NOT NULL GROUP BY chain"):
            chains.setdefault(ch, {})["launches"] = int(n or 0)
    rows = []
    for ch, d in chains.items():
        usd = _native_usd(ch)
        rows.append({"chain": ch, "name": _CHAIN_NAME.get(ch, ch), "unit": _NATIVE_SYM.get(ch, ""),
                     "launches": d.get("launches", d.get("curves", 0)), "graduated": d.get("graduated", 0),
                     "volume_usd": round(d.get("volume_native", 0) * usd, 2), "volume_24h_usd": round(d.get("volume_24h_native", 0) * usd, 2),
                     "trades_24h": d.get("trades_24h", 0), "creator_fees_native": round(creators_native.get(ch, 0.0), 6),
                     "creator_fees_usd": round(creators_native.get(ch, 0.0) * usd, 2)})
    rows.sort(key=lambda x: (x["launches"], x["volume_usd"]), reverse=True)
    sol = _native_usd("solana")
    out = {"now": int(now), "ferzan_live": bool(_ferzan_block(None).get("live")), "burn": {**totals, "bought_usd": round(totals["bought_sol"] * sol, 2)},
           "burns": burns[:60], "chains": rows,
           "totals": {"launches": sum(r["launches"] for r in rows), "graduated": sum(r["graduated"] for r in rows),
                      "volume_usd": round(sum(r["volume_usd"] for r in rows), 2), "creator_fees_usd": round(sum(r["creator_fees_usd"] for r in rows), 2),
                      "trades_24h": trades_24h, "traders_24h": traders_24h, "trades_all": trades_all},
           "multisig": "2vWqwX72ijo24vgvPQW6yBQh2qXE4jrEd18YDdEbWKLG"}
    _TRANS_CACHE.update(t=now, v=out)
    return out


def _follow_db():
    import sqlite3 as _sq

    conn = _sq.connect(db.DB_PATH, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS creator_follows (user_id INTEGER NOT NULL, wallet TEXT NOT NULL, created_at INTEGER, "
                 "PRIMARY KEY (user_id, wallet))")
    return conn


def _followers(wallet: str) -> list:
    try:
        conn = _follow_db()
        rows = [int(r[0]) for r in conn.execute("SELECT user_id FROM creator_follows WHERE wallet = ?", (_norm_addr(wallet),))]
        conn.close()
        return rows
    except Exception:
        return []


def _notify_followers(req, token: str, curve: str) -> None:
    """DM everyone who follows this creator (from the Launch Bot), in the background."""
    users = _followers(req.wallet_address or "")
    if not users or not TELEGRAM_BOT_TOKEN:
        return
    import threading

    path = _site_path(req.chain, token, curve or "")
    who = (req.wallet_address or "")[:4] + "…" + (req.wallet_address or "")[-4:]
    text = (f"🆕 A creator you follow ({who}) just launched <b>{_html.escape(req.name or '')}</b> "
            f"(${_html.escape(req.symbol or '')}) on {_CHAIN_NAME.get(req.chain, req.chain)}.\n\n"
            f"https://ferzan-factory.com{path}\n\nStop these alerts: /following")

    def go():
        for uid in users[:5000]:
            try:
                requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                              json={"chat_id": uid, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}, timeout=10)
            except Exception:
                pass
            time.sleep(0.05)  # stays under Telegram's 30 messages a second

    threading.Thread(target=go, daemon=True).start()


_CREATOR_CACHE: dict = {}


@app.get("/api/creator/{wallet}")
def creator_page(wallet: str):
    """Everything one wallet launched through Ferzan, with its track record and FERZAN badge."""
    import json as _json
    from datetime import datetime as _dt

    if not _re.fullmatch(_ADDR_ANY, wallet or ""):
        raise HTTPException(404, "not found")
    key = _norm_addr(wallet)
    hit = _CREATOR_CACHE.get(key)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    hide = _feed_hidden()
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT id, chain, mode, name, symbol, image_url, result_token_address, extra_params, created_at, wallet_address FROM launch_requests "
            "WHERE status = 'confirmed' AND result_token_address IS NOT NULL AND (wallet_address = ? OR LOWER(wallet_address) = ?) "
            "ORDER BY created_at DESC LIMIT 200", (wallet, key.lower())).fetchall()
    if not rows:
        out = {"found": False}
        _CREATOR_CACHE[key] = (time.time(), out)
        return out

    def ts(v):
        try:
            return int(_dt.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0

    items, grads, best, vol = [], 0, 0.0, 0.0
    c = _idx_db()
    try:
        for r in rows:
            tok = r["result_token_address"]
            if str(tok).lower() in hide:
                continue
            try:
                curve = (_json.loads(r["extra_params"] or "{}").get("curve_address") or "").strip()
            except ValueError:
                curve = ""
            cv = _coin_row(c, r["chain"], tok) if c is not None else None
            usd = _native_usd(r["chain"])
            it = {"chain": r["chain"], "token": tok, "name": r["name"], "symbol": r["symbol"],
                  "image": r["image_url"] if str(r["image_url"] or "").startswith("https://") else "",
                  "launched_ts": ts(r["created_at"]), "mode": r["mode"], "mcap_usd": 0.0, "progress": None, "graduated": False,
                  "volume_usd": 0.0, "path": _site_path(r["chain"], tok, curve)}
            if cv:
                it.update(mcap_usd=round(float(cv["mcap"] or 0) * usd, 2), progress=round(_progress(cv), 1), graduated=bool(cv["graduated"]),
                          volume_usd=round(float(cv["volume"] or 0) * usd, 2), path=_site_path(cv["chain"], cv["token"], cv["curve"]))
                grads += 1 if cv["graduated"] else 0
                best = max(best, it["mcap_usd"])
                vol += it["volume_usd"]
            items.append(it)
    finally:
        if c is not None:
            c.close()
    score = {}
    if items:
        try:
            s = creator_score(items[0]["token"])
            if s.get("found"):
                score = {"score": s["score"], "label": s["label"], "lines": s["lines"][:6]}
        except Exception:
            score = {}
    badge = ""
    try:
        import ferzan_perks as _fp
        badge = _fp.perks(rows[0]["wallet_address"] or "").get("badge") or ""
    except Exception:
        pass
    firsts = [i["launched_ts"] for i in items if i["launched_ts"]]
    out = {"found": True, "wallet": rows[0]["wallet_address"], "launches": len(items), "graduated": grads, "best_mcap_usd": best,
           "volume_usd": round(vol, 2), "chains": sorted({i["chain"] for i in items}), "first_launch_ts": min(firsts) if firsts else 0,
           "followers": len(_followers(rows[0]["wallet_address"] or "")), "badge": badge, "score": score, "items": items[:100],
           "follow_url": f"https://t.me/{(os.environ.get('LAUNCH_BOT_USERNAME') or 'Ferzan_Launch_Bot').lstrip('@')}?start=follow_{rows[0]['wallet_address']}"}
    _CREATOR_CACHE[key] = (time.time(), out)
    if len(_CREATOR_CACHE) > 500:
        _CREATOR_CACHE.clear()
    return out


@app.on_event("startup")
def startup():
    db.init_db()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
