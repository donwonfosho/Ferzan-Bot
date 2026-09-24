"""Portfolio snapshot from PUBLIC data only (addresses from the DB, balances
from RPC, marks from DexScreener). Never decrypts a key, so the internet-
facing Mini App process can use it. Shared by webapp.py and the bot's daily
recap. Blocking - run via asyncio.to_thread.
"""

from __future__ import annotations

import time

import requests

import db
import evm_signer
import signer
from chains import CHAINS

# DexScreener chainId -> Ferzan chain id (EVM chains we can read balances on)
DS_TO_CID = {"base": "base", "ethereum": "eth", "bsc": "bsc", "arbitrum": "arb", "avalanche": "avax"}

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
                "mc": float(p.get("marketCap") or p.get("fdv") or 0),
                "name": ((p.get("baseToken") or {}).get("name") or "")[:40],
                "dex": p.get("dexId") or "",
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
    # Public data only: this internet-facing process never decrypts a key
    # and never creates wallets (that happens in the bot).
    wallet = db.get_user_wallet(uid)
    if not wallet:
        raise LookupError("no wallet")
    slots = db.list_wallet_slots(uid)
    sol_pub, evm_pub = wallet.get("sol_pub", ""), wallet.get("evm_pub", "")

    sol_bal = signer.sol_balance_lamports(sol_pub) / 1e9 if sol_pub else 0.0
    try:
        eth_bal, _ = evm_signer.native_balance("base", evm_pub)
    except Exception:
        eth_bal = 0.0
    sol_px, eth_px = _price("solana"), _price("ethereum")

    try:
        sol_holds = signer.holdings_pub(sol_pub)
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
        "ts": int(time.time()),
    }
