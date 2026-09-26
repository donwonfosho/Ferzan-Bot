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
from tron_launch import build_unsigned_launch_tx as build_tron_plain_tx
from ton_launch import build_unsigned_launch_tx as build_ton_fee_tx

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
            raise HTTPException(
                501,
                "Tron launch is not signable yet — Mini App has no TronWeb encoder. "
                "Label is coming-soon until that ships.",
            )
            result = build_tron_plain_tx(
                creator_address=body.wallet_address,
                name=req.name,
                symbol=req.symbol,
                total_supply=total_supply,
                project_url=req.extra_params.get("project_url", ""),
            )
            response = {
                "chain": "tron",
                "trigger": result.trigger,
                "fee_transfer": result.fee_transfer,
                "factory": result.factory,
                "note": result.note,
            }

        elif req.chain == "ton":
            result = build_ton_fee_tx(request_id)
            response = {
                "chain": "ton",
                "to": result.to,
                "amount_nano": result.amount_nano,
                "comment": result.comment,
                "note": result.note,
            }

        elif req.chain in CHAIN_CONFIGS:
            if req.chain == "arc" and req.mode == "bonding_curve":
                raise HTTPException(
                    501,
                    "Arc bonding curve is held — Uniswap v4 on Arc, no V2 addLiquidityETH.",
                )
            if req.chain == "arc" and (os.environ.get("ARC_LAUNCH_LIVE") or "").strip() != "1":
                raise HTTPException(
                    501,
                    "Arc plain is held until you confirm rpc.mainnet.arc.io and "
                    "LAUNCH_FEE_ARC (6-dec USDC, not LAUNCH_FEE_WEI). Then set ARC_LAUNCH_LIVE=1.",
                )
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


@app.post("/api/launch-requests/{request_id}/complete")
def complete_request(request_id: str, body: CompleteRequest):
    req = db.get_launch_request(request_id)
    if not req:
        raise HTTPException(404, "Launch request not found")

    token_addr = (body.result_token_address or "").strip()
    curve_addr = (body.curve_address or "").strip()
    if body.tx_hash and (not token_addr or not curve_addr):
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
    if req.wallet_address and req.telegram_user_id:
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
    return {"status": "ok"}


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
}
_CHAIN_NAME = {"solana": "Solana", "bsc": "BNB Chain", "base": "Base", "ethereum": "Ethereum", "robinhood": "Robinhood Chain"}


def _launch_card(req, token_addr: str, curve_addr: str, tx_hash: str) -> str:
    esc = _html.escape
    mode_txt = {"plain": "Standard token", "meteora": "Meteora bonding curve",
                "bonding_curve": "Bonding curve"}.get(req.mode, req.mode)
    if req.mode == "bonding_curve":
        safety = ("Fixed supply, no owner. Trades on the curve, then moves to a DEX pool at the same price "
                  "and the pool liquidity is burned forever. Team tokens stay locked until graduation.")
    elif req.mode == "meteora":
        safety = "Meteora curve with anti-sniper fee; moves to a Meteora DAMM v2 pool when it fills."
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
    if curve_addr and req.mode == "bonding_curve":
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
_NATIVE_USD: dict = {"t": 0.0, "bsc": 0.0, "base": 0.0, "solana": 0.0}
_NATIVE_SYM = {"bsc": "BNB", "base": "ETH", "ethereum": "ETH", "robinhood": "ETH", "solana": "SOL"}


def _idx_db():
    import sqlite3 as _sq

    path = os.environ.get("CURVE_INDEX_DB") or os.path.join(os.path.dirname(db.DB_PATH) or ".", "curve_index.db")
    if not os.path.exists(path):
        return None
    c = _sq.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    c.row_factory = _sq.Row
    return c


def _native_usd(chain: str) -> float:
    chain = "base" if chain in ("ethereum", "robinhood") else chain  # all ETH-gas chains share the ETH price
    if time.time() - _NATIVE_USD["t"] > 300:
        try:
            r = requests.get("https://api.coingecko.com/api/v3/simple/price",
                             params={"ids": "binancecoin,ethereum,solana", "vs_currencies": "usd"}, timeout=8).json()
            _NATIVE_USD.update(t=time.time(), bsc=float(r["binancecoin"]["usd"]), base=float(r["ethereum"]["usd"]),
                               solana=float(r["solana"]["usd"]))
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
            out[cv] = out[cv.lower()] = {"image": r[1] or "", "description": r[2] or ""}
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
        "url": _trade_url(r["chain"], r["curve"], r["token"]),
    }


@app.get("/api/launches")
def launches_feed(sort: str = "new", limit: int = 30, chain: str = ""):
    """New launches (all chains), King of the Hill (closest to graduating) and top 24h volume."""
    limit = max(1, min(int(limit or 30), 60))
    c = _idx_db()
    items: list = []
    if c is not None:
        where, args = "1=1", []
        if chain in _NATIVE_SYM:
            where, args = "chain = ?", [chain]
        if sort == "koth":
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
                if r_:
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
    if sort == "new" and chain in ("", "solana"):
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
    return {"sort": sort, "items": items, "now": int(time.time())}


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


_EVM_LAUNCH_FEE = {"bsc": 0.015, "base": 0.003, "ethereum": 0.003, "robinhood": 0.003}  # native, fixed in the v3 factories
_REV_RPC = {"bsc": "https://bsc-rpc.publicnode.com", "base": "https://base-rpc.publicnode.com",
            "ethereum": "https://ethereum-rpc.publicnode.com", "robinhood": "https://rpc.mainnet.chain.robinhood.com"}


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
    px = {ch: _native_usd(ch) for ch in ("bsc", "base", "ethereum", "robinhood", "solana")}
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
                        f"FROM trades WHERE ts >= ? GROUP BY chain", (since,)):
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
        for ch in ("bsc", "base", "ethereum", "robinhood"):
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
    token = (token or "").lower()
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


@app.on_event("startup")
def startup():
    db.init_db()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
