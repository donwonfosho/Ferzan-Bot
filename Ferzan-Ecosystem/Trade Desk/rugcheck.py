"""Solana token safety report, read straight from chain (no third party).

What it checks, and why each one matters to a buyer:
  * mint authority   - still set = the dev can print unlimited new supply
  * freeze authority - still set = the dev can freeze YOUR tokens (honeypot)
  * Token-2022 extensions that can trap or tax a holder:
      permanent delegate (can move anyone's tokens), non-transferable,
      default-frozen accounts, transfer hook (custom code on every transfer,
      the classic "can't sell" trick), transfer fee, pausable
  * top-10 holder share, EXCLUDING liquidity pools / bonding curves and the
    burn address, so a pump.fun curve holding 80% doesn't read as a whale.

Every public function is blocking (RPC calls) - run via asyncio.to_thread.
A failed read returns {"ok": False}; callers must treat that as "unknown",
never as "safe".
"""

from __future__ import annotations

import threading
import time

import requests

import signer

CACHE_TTL_S = 120
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# Programs that own pool / curve authority accounts. A token account whose
# owner is an account owned by one of these is liquidity, not a holder.
POOL_PROGRAMS = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",  # pump.fun bonding curve
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",  # PumpSwap AMM
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
    "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",  # Raydium CPMM
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",  # Raydium CLMM
    "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",  # Raydium LaunchLab
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",  # Meteora DLMM
    "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",  # Meteora pools
    "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",  # Meteora DAMM v2
    "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN",  # Meteora bonding curve
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",  # Orca Whirlpools
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",  # Orca v2
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",  # Moonshot
}
# Authorities that are PDAs with no account data (owner lookup returns null).
POOL_AUTHORITIES = {
    "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4 authority
    "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM authority
    "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh",  # Raydium LaunchLab authority
}
BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111",
}


def _rpc(method: str, params: list, timeout: float = 8) -> dict:
    r = requests.post(
        signer._rpc(),
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=timeout,
    )
    body = r.json() if r.content else {}
    if r.status_code >= 400 or body.get("error"):
        raise RuntimeError(str(body.get("error") or r.status_code)[:160])
    return body.get("result")


def _extension_flags(exts: list) -> tuple[list[str], list[str]]:
    """(blocking flags, warning flags) from Token-2022 extensions."""
    block, warn = [], []
    for ext in exts or []:
        if not isinstance(ext, dict):
            continue
        name = str(ext.get("extension") or "")
        state = ext.get("state") or {}
        if name == "permanentDelegate" and state.get("delegate"):
            block.append("permanent delegate (can take your tokens)")
        elif name == "nonTransferable":
            block.append("non-transferable")
        elif name == "defaultAccountState" and str(state.get("accountState") or "").lower() == "frozen":
            block.append("new holders start frozen")
        elif name == "transferHook" and state.get("programId"):
            block.append("transfer hook (custom code can block sells)")
        elif name == "transferFeeConfig":
            fee = (state.get("newerTransferFee") or {}).get("transferFeeBasisPoints") or 0
            try:
                fee_pct = int(fee) / 100
            except (TypeError, ValueError):
                fee_pct = 0
            if fee_pct >= 10:
                block.append(f"{fee_pct:g}% transfer tax")
            elif fee_pct > 0:
                warn.append(f"{fee_pct:g}% transfer tax")
        elif name in {"pausableConfig", "pausable"}:
            warn.append("pausable")
    return block, warn


def _top_holders(mint: str, supply_raw: int) -> tuple[float | None, float | None, int]:
    """(top-10 share %, largest single holder %, pools excluded)."""
    largest = _rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}]) or {}
    accts = [a for a in (largest.get("value") or []) if int(a.get("amount") or 0) > 0]
    if not accts or supply_raw <= 0:
        return None, None, 0
    addrs = [a["address"] for a in accts]
    infos = (_rpc("getMultipleAccounts", [addrs, {"encoding": "jsonParsed"}]) or {}).get("value") or []
    holder_of: dict[str, str] = {}
    for addr, info in zip(addrs, infos):
        parsed = (((info or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        holder_of[addr] = str(parsed.get("owner") or "")
    owners = sorted({o for o in holder_of.values() if o})
    owner_program: dict[str, str | None] = {}
    if owners:
        oinfo = (
            _rpc("getMultipleAccounts", [owners, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}])
            or {}
        ).get("value") or []
        for o, info in zip(owners, oinfo):
            owner_program[o] = (info or {}).get("owner") if info else None
    real, pools = [], 0
    for a in accts:
        owner = holder_of.get(a["address"], "")
        prog = owner_program.get(owner)
        if owner in POOL_AUTHORITIES or owner in BURN_ADDRESSES or (prog and prog in POOL_PROGRAMS):
            pools += 1
            continue
        real.append(int(a["amount"]))
    real.sort(reverse=True)
    top10 = sum(real[:10]) / supply_raw * 100 if real else 0.0
    top1 = real[0] / supply_raw * 100 if real else 0.0
    return top10, top1, pools


def sol_report(mint: str, fresh: bool = False) -> dict:
    mint = (mint or "").strip()
    now = time.time()
    if not fresh:
        with _lock:
            hit = _cache.get(mint)
        if hit and now - hit[0] < CACHE_TTL_S:
            return hit[1]
    rep: dict = {"ok": False, "mint": mint}
    try:
        info = _rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}]) or {}
        val = info.get("value") or {}
        parsed = ((val.get("data") or {}) if isinstance(val.get("data"), dict) else {}).get("parsed") or {}
        if parsed.get("type") != "mint":
            rep["error"] = "not a token mint"
            return rep
        mi = parsed.get("info") or {}
        supply_raw = int(mi.get("supply") or 0)
        block, warn = _extension_flags(mi.get("extensions") or [])
        rep.update(
            {
                "ok": True,
                "token2022": val.get("owner") == TOKEN_2022,
                "mint_auth": bool(mi.get("mintAuthority")),
                "freeze_auth": bool(mi.get("freezeAuthority")),
                "decimals": int(mi.get("decimals") or 0),
                "supply_raw": supply_raw,
                "block_flags": block,
                "warn_flags": warn,
                "top10_pct": None,
                "top1_pct": None,
                "pools_excluded": 0,
            }
        )
        try:
            top10, top1, pools = _top_holders(mint, supply_raw)
            rep.update({"top10_pct": top10, "top1_pct": top1, "pools_excluded": pools})
        except Exception as exc:  # authorities still count; holders unknown
            rep["holders_error"] = str(exc)[:120]
    except Exception as exc:
        rep["error"] = str(exc)[:160]
        return rep  # don't cache failures: next look retries
    with _lock:
        _cache[mint] = (now, rep)
        if len(_cache) > 2000:
            for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:500]:
                _cache.pop(k, None)
    return rep


def block_reason(rep: dict) -> str:
    """Non-empty = this token can trap a buyer. Only from a successful read."""
    if not rep.get("ok"):
        return ""
    if rep.get("freeze_auth"):
        return "freeze authority is ON (dev can freeze your tokens)"
    if rep.get("block_flags"):
        return rep["block_flags"][0]
    return ""


def security_line(rep: dict) -> str:
    """One or two card lines. Emoji + words, never color alone."""
    if not rep.get("ok"):
        return "🛡 Safety check unavailable right now — trade carefully"
    bits = [
        "Mint ✅ off" if not rep.get("mint_auth") else "Mint ⚠️ ON",
        "Freeze ✅ off" if not rep.get("freeze_auth") else "Freeze 🚨 ON",
    ]
    top10 = rep.get("top10_pct")
    if top10 is not None:
        mark = "🚨" if top10 >= 50 else "⚠️" if top10 >= 30 else "✅"
        bits.append(f"Top 10 {mark} {top10:.0f}%")
    head = "🚨 RUG RISK" if block_reason(rep) else "🛡"
    line = f"{head} " + " · ".join(bits)
    flags = list(rep.get("block_flags") or []) + list(rep.get("warn_flags") or [])
    if rep.get("mint_auth"):
        flags.append("dev can mint more")
    top1 = rep.get("top1_pct")
    if top1 is not None and top1 >= 15:
        flags.append(f"one wallet holds {top1:.0f}%")
    if flags:
        line += "\n⚠️ " + " · ".join(flags[:4])
    return line
