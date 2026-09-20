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

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

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
    }


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
                result = build_solana_plain_tx(
                    creator_pubkey=body.wallet_address,
                    decimals=req.decimals,
                    initial_supply_raw=total_supply,
                    rpc_url=RPC_URLS["solana"],
                )
                unsigned_tx_hex = bytes(result.unsigned_transaction).hex()  # transport as hex; adjust to match your Mini App's deserialization
                response = {"chain": "solana", "unsigned_transaction": unsigned_tx_hex, "mint_address": result.mint_address}
            elif req.mode in ("meteora", "pumpfun", "bonding_curve"):
                result = build_unsigned_meteora_tx(
                    creator_pubkey=body.wallet_address,
                    decimals=req.decimals,
                    initial_supply_raw=total_supply,
                    rpc_url=RPC_URLS["solana"],
                    graduation_sol_lamports=int(req.extra_params.get("graduation_eth_threshold") or 0),
                )
                unsigned_tx_hex = bytes(result.unsigned_transaction).hex()
                response = {
                    "chain": "solana",
                    "mode": "meteora",
                    "unsigned_transaction": unsigned_tx_hex,
                    "mint_address": result.mint_address,
                    "program_id": result.program_id,
                    "note": result.note,
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
                    raise HTTPException(500, f"No bonding-curve factory address configured for {req.chain}")
                builder = EvmBondingCurveTxBuilder(req.chain, factory_addr, rpc_url=rpc)
                extra = req.extra_params or {}
                dec = 6 if req.chain == "arc" else 18
                aw, ab = parse_allocs(extra.get("allocs") or "")
                mins = int(float(str(extra.get("start_minutes") or "0") or 0))
                start_time = int(time.time()) + mins * 60 if mins > 0 else 0
                tx = builder.build_unsigned_curve_launch_tx(
                    creator_address=body.wallet_address,
                    name=req.name,
                    symbol=req.symbol,
                    total_supply=total_supply,
                    graduation_eth_threshold=int(extra.get("graduation_eth_threshold", 0)),
                    virtual_eth_reserve=int(extra.get("virtual_eth_reserve", 0)),
                    virtual_token_reserve=int(extra.get("virtual_token_reserve", 0)),
                    dev_buy_wei=parse_native_amount(extra.get("dev_buy"), dec),
                    start_time=start_time,
                    max_buy_wei=parse_native_amount(extra.get("max_buy"), dec),
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

    ca = body.result_token_address or "see transaction"
    extra = req.extra_params or {}
    mint_ok = req.chain == "solana" or req.mode == "plain"
    safety = (
        f"🛡 *Safety card*\n"
        f"Mint revoked / fixed supply: {'✓' if mint_ok else 'curve holds remainder'}\n"
        f"LP burn on graduate: {'✓ curve' if req.mode == 'bonding_curve' else 'use /lplock after you add LP'}\n"
        f"Verify on explorer before you ape.\n"
    )
    trade = (os.environ.get("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")
    text = (
        f"🚀 *New Ferzan launch*\n"
        f"*{req.name}* (${req.symbol}) on {req.chain}\n"
        f"Mode: {req.mode}\n"
        f"CA: `{ca}`\n"
        f"Tx: `{body.tx_hash}`\n\n"
        f"{safety}\n"
        f"Trade: https://t.me/{trade}"
    )
    _notify_telegram(req.chat_id, text)
    channel = (os.environ.get("FERZAN_LAUNCHES_CHANNEL") or "").strip()
    if channel:
        _notify_telegram(channel, text)
    return {"status": "ok"}


@app.post("/api/launch-requests/{request_id}/fail")
def fail_request(request_id: str, body: CompleteRequest):
    req = db.get_launch_request(request_id)
    if not req:
        raise HTTPException(404, "Launch request not found")

    db.update_status(request_id, "failed", error_message=body.tx_hash)  # tx_hash field reused as message here
    _notify_telegram(
        chat_id=req.chat_id,
        text=f"⚠️ Launch of *{req.name}* failed or was cancelled in your wallet.",
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


def _notify_telegram(chat_id: int, text: str):
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set -- cannot notify user")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.error(f"Failed to notify Telegram chat {chat_id}: {e}")


@app.on_event("startup")
def startup():
    db.init_db()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
