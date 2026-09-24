"""Helius Sender: faster Solana landing (and sandwich-avoidance routing).

Sender fans each transaction out over several fast paths at once. Every tx
it gets must carry BOTH a compute-unit price (priority fee) AND a SOL tip to
one of Helius's Sender tip accounts. Jupiter's ready-made swap tx can carry
one or the other, never both, so we build the tx ourselves from Jupiter's
/swap-instructions (same route, same slippage, same accounts).

Two tiers (docs: helius.dev/docs/sending-transactions/sender):
  * swqos_only=true  - staked-connection paths only, tip >= 0.000005 SOL
  * full + mev-protect=true - all paths, tip >= 0.001 SOL, and routes around
    validators statistically linked to sandwich attacks

Blocking (HTTP) - callers run it in a worker thread.
"""

from __future__ import annotations

import base64
import logging
import os
import random

import requests

log = logging.getLogger("sender")

JUP_SWAP_IX = "https://lite-api.jup.ag/swap/v1/swap-instructions"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
TIP_ACCOUNTS = (
    "4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE",
    "D2L6yPZ2FmmmTKPgzaMKdhu6EWZcTpLy1Vhx8uvZe7NZ",
    "9bnz4RShgq1hAnLnZbP8kbgBg1kEmcJBYQq3gQbmnSta",
    "5VY91ws6B2hMmBFRsXkoAAdsPHBJwRfBht4DXox3xkwn",
    "2nyhqdwKcJZR2vcqCyrYsaPVdAnFoJjiksCXJ7hfEYgD",
    "2q5pghRs6arqVjRvT5gfgWfWcHWmw1ZuCzphgd5KfWGJ",
    "wyvPkWjVZz1M8fHQnMMCDTQDbkManefNNhweYk5WkcF",
    "3KCKozbAaF75qEU33jtzozcJ29yJuaLJTy2jFdzUY8bT",
    "4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey",
    "4TQLFNWK8AovT1gFvda5jfw2oJeRMKEmw7aH6MGBJ3or",
)
FULL_TIP_MIN = 1_000_000  # 0.001 SOL: Sender full tier minimum
SWQOS_TIP_MIN = 5_000  # 0.000005 SOL: swqos_only minimum
ALT_META = 56  # address lookup table header size; addresses follow


def enabled() -> bool:
    return (os.getenv("SENDER_ENABLED", "1").strip().lower()) not in {"0", "false", "off", "no"}


def url(mev_protect: bool) -> str:
    base = os.getenv("HELIUS_SENDER_URL", "http://ewr-sender.helius-rpc.com/fast").strip()
    q = "mev-protect=true" if mev_protect else "swqos_only=true"
    return base + ("&" if "?" in base else "?") + q


def fees(mev_protect: bool, user_fee: int) -> tuple[int, int]:
    """(priority_fee_lamports, tip_lamports). Anti-MEV: the user's ⛽ gas is
    the tip (>= Sender's 0.001 floor) plus a small priority fee. Normal: the
    gas is the priority fee and the tip is Sender's tiny swqos floor."""
    if mev_protect:
        prio = int(os.getenv("SENDER_MEV_PRIORITY_LAMPORTS", "10000"))
        return max(1_000, prio), max(FULL_TIP_MIN, int(user_fee))
    return max(1_000, int(user_fee)), SWQOS_TIP_MIN


def _ix(j: dict):
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    return Instruction(
        Pubkey.from_string(j["programId"]),
        base64.b64decode(j["data"]),
        [AccountMeta(Pubkey.from_string(a["pubkey"]), bool(a["isSigner"]), bool(a["isWritable"])) for a in j["accounts"]],
    )


def _has_cu_price(ixs_json: list[dict]) -> bool:
    for j in ixs_json:
        if j.get("programId") == COMPUTE_BUDGET:
            data = base64.b64decode(j.get("data") or "")
            if data[:1] == bytes([3]):  # SetComputeUnitPrice
                return True
    return False


def _cu_price_ix(priority_lamports: int, cu_limit: int):
    """SetComputeUnitPrice (instruction 3, u64 micro-lamports per CU)."""
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    micro = max(1, int(priority_lamports * 1_000_000 // max(1, cu_limit)))
    return Instruction(Pubkey.from_string(COMPUTE_BUDGET), bytes([3]) + micro.to_bytes(8, "little"), [])


def _rpc_call(rpc: str, method: str, params: list, timeout: float = 10):
    r = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout)
    body = r.json() if r.content else {}
    if r.status_code >= 400 or body.get("error"):
        raise RuntimeError(f"{method}: {str(body.get('error') or r.status_code)[:160]}")
    return body.get("result")


def _lookup_tables(rpc: str, addresses: list[str]) -> list:
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.pubkey import Pubkey

    if not addresses:
        return []
    vals = (_rpc_call(rpc, "getMultipleAccounts", [addresses, {"encoding": "base64"}]) or {}).get("value") or []
    out = []
    for addr, v in zip(addresses, vals):
        if not v:
            raise RuntimeError(f"lookup table {addr} not found")
        data = base64.b64decode(v["data"][0])
        keys = [Pubkey.from_bytes(data[i:i + 32]) for i in range(ALT_META, len(data) - 31, 32)]
        out.append(AddressLookupTableAccount(key=Pubkey.from_string(addr), addresses=keys))
    return out


def build(quote: dict, kp, rpc: str, priority_lamports: int, tip_lamports: int) -> tuple[str, str, int]:
    """(base64 wire tx, signature, lastValidBlockHeight). Raises on any
    problem BEFORE anything is sent, so the caller can fall back safely."""
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import VersionedTransaction

    r = requests.post(
        JUP_SWAP_IX,
        json={
            "quoteResponse": quote,
            "userPublicKey": str(kp.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": int(priority_lamports),
        },
        timeout=20,
    )
    j = r.json() if r.content else {}
    if r.status_code >= 400 or j.get("error") or not j.get("swapInstruction"):
        raise RuntimeError(f"Jupiter swap-instructions: {str(j.get('error') or j.get('message') or r.status_code)[:160]}")
    compute = list(j.get("computeBudgetInstructions") or [])
    ixs = [_ix(x) for x in compute]
    if not _has_cu_price(compute):
        ixs.append(_cu_price_ix(priority_lamports, int(j.get("computeUnitLimit") or 1_400_000)))
    ixs += [_ix(x) for x in (j.get("setupInstructions") or [])]
    ixs.append(_ix(j["swapInstruction"]))
    if j.get("cleanupInstruction"):
        ixs.append(_ix(j["cleanupInstruction"]))
    ixs += [_ix(x) for x in (j.get("otherInstructions") or [])]
    tip_to = Pubkey.from_string(random.choice(TIP_ACCOUNTS))
    ixs.append(transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=tip_to, lamports=int(tip_lamports))))
    alts = _lookup_tables(rpc, list(j.get("addressLookupTableAddresses") or []))
    bh = (_rpc_call(rpc, "getLatestBlockhash", [{"commitment": "confirmed"}]) or {}).get("value") or {}
    msg = MessageV0.try_compile(kp.pubkey(), ixs, alts, Hash.from_string(bh["blockhash"]))
    tx = VersionedTransaction(msg, [kp])
    return base64.b64encode(bytes(tx)).decode(), str(tx.signatures[0]), int(bh["lastValidBlockHeight"])


def simulate(rpc: str, wire: str) -> str | None:
    """Error text if the swap would fail on-chain right now; None if it
    passes OR the simulation itself couldn't run (then we let it go and the
    confirm step reports the real outcome)."""
    try:
        res = _rpc_call(
            rpc,
            "simulateTransaction",
            # replaceRecentBlockhash: a load-balanced RPC may answer from a node
            # that hasn't seen our fresh blockhash yet -- don't fail on that.
            [wire, {"encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "processed"}],
            timeout=12,
        ) or {}
    except Exception as exc:
        log.warning("sender simulate unavailable: %s", exc)
        return None
    val = res.get("value") or {}
    if not val.get("err"):
        return None
    logs = [ln for ln in (val.get("logs") or []) if "rror" in ln or "failed" in ln.lower()]
    return f"{val['err']}" + (f" — {logs[-1][:160]}" if logs else "")


# Error text that proves Sender rejected the tx BEFORE forwarding it. Only
# these let the caller fall back to another route; anything else might have
# been forwarded, so the caller checks the chain instead of re-building.
# Exact phrases / JSON-RPC codes only: loose words ("tip" is inside
# "multiple", "invalid" shows up in gateway errors after a forward) could turn
# a maybe-forwarded tx into a second buy.
_REFUSED_PHRASES = ("must include a tip", "tip account", "compute unit price", "computeunitprice",
                    "rate limit", "too many requests", "failed to deserialize", "could not decode",
                    "transaction too large")
_REFUSED_CODES = (-32600, -32602)  # invalid request / invalid params: rejected before forwarding


def send(wire: str, mev_protect: bool) -> tuple[str, str]:
    """("sent" | "refused" | "uncertain", detail). "refused" = provably not
    forwarded (safe to try another route). "uncertain" = may have gone out:
    the caller must check the chain, never blindly rebuild."""
    try:
        r = requests.post(
            url(mev_protect),
            json={"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                  "params": [wire, {"encoding": "base64", "skipPreflight": True, "maxRetries": 0}]},
            timeout=10,
        )
        if r.status_code == 429:
            return "refused", "rate limited"
        body = r.json() if r.content else {}
        if not isinstance(body, dict):
            return "uncertain", f"odd reply ({r.status_code})"
        if body.get("error"):
            err = body["error"]
            msg = str(err.get("message") if isinstance(err, dict) else err)[:200]
            code = err.get("code") if isinstance(err, dict) else None
            low = msg.lower()
            refused = code in _REFUSED_CODES or any(p in low for p in _REFUSED_PHRASES)
            return ("refused" if refused else "uncertain"), msg
        if r.status_code >= 400:
            return "uncertain", f"HTTP {r.status_code}"
        return "sent", ""
    except Exception as exc:
        return "uncertain", f"send uncertain: {str(exc)[:160]}"
