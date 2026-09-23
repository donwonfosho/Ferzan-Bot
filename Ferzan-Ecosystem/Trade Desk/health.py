"""Read-only health checks for every outside service the desk depends on.

Nothing here signs or sends — each probe is a quote, a price, or a block
height. Probes run in parallel with short timeouts so /health answers in a
few seconds even when something is down.
"""

from __future__ import annotations

import html
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import requests

TIMEOUT = 8

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_SOL = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
EVM_NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
TON_ASSET = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"
USDT_TON = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"


@dataclass
class Check:
    name: str
    ok: bool | None  # None = not configured / skipped
    ms: int
    detail: str


def _timed(name: str, fn) -> Check:
    t0 = time.monotonic()
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001 — a probe failing IS the result
        ok, detail = False, f"{type(exc).__name__}: {str(exc)[:90]}"
    return Check(name, ok, int((time.monotonic() - t0) * 1000), detail)


def _sol_rpc():
    import signer

    r = requests.post(
        signer._rpc(),
        json={"jsonrpc": "2.0", "id": 1, "method": "getSlot"},
        timeout=TIMEOUT,
    )
    slot = (r.json() or {}).get("result")
    return (bool(slot), f"slot {slot}" if slot else r.text[:90])


def _jupiter():
    import signer

    r = requests.get(
        signer.JUP_QUOTE,
        params={"inputMint": SOL_MINT, "outputMint": USDC_SOL, "amount": "10000000", "slippageBps": "50"},
        timeout=TIMEOUT,
    )
    out = (r.json() or {}).get("outAmount") if r.content else None
    return (bool(out), f"0.01 SOL → {int(out) / 1e6:.2f} USDC" if out else f"HTTP {r.status_code}")


def _zerox():
    key = (os.getenv("ZEROX_API_KEY") or "").strip()
    if not key:
        return None, "ZEROX_API_KEY not set — EVM buys disabled"
    r = requests.get(
        "https://api.0x.org/swap/allowance-holder/price",
        headers={"0x-api-key": key, "0x-version": "v2"},
        params={"chainId": "8453", "sellToken": EVM_NATIVE, "buyToken": USDC_BASE, "sellAmount": str(10**15)},
        timeout=TIMEOUT,
    )
    data = r.json() if r.content else {}
    out = data.get("buyAmount")
    return (bool(out), f"0.001 ETH → {int(out) / 1e6:.2f} USDC (Base)" if out else f"HTTP {r.status_code} {str(data)[:70]}")


def _evm_rpc(cid: str):
    from chains import CHAINS

    rpc = (CHAINS.get(cid) or {}).get("rpc")
    if not rpc:
        return None, "no RPC configured"

    def probe():
        r = requests.post(
            rpc,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
            timeout=TIMEOUT,
        )
        blk = (r.json() or {}).get("result")
        return (bool(blk), f"block {int(blk, 16)}" if blk else r.text[:90])

    return probe


def _dexscreener():
    r = requests.get("https://api.dexscreener.com/latest/dex/search", params={"q": "SOL"}, timeout=TIMEOUT)
    pairs = (r.json() or {}).get("pairs") or []
    return (bool(pairs), f"{len(pairs)} pairs" if pairs else f"HTTP {r.status_code}")


def _geckoterminal():
    r = requests.get("https://api.geckoterminal.com/api/v2/networks/solana/new_pools", timeout=TIMEOUT)
    rows = (r.json() or {}).get("data") or []
    return (bool(rows), f"{len(rows)} new pools" if rows else f"HTTP {r.status_code}")


def _coingecko():
    r = requests.get(
        "https://api.coingecko.com/api/v3/simple/price",
        params={"ids": "solana", "vs_currencies": "usd"},
        timeout=TIMEOUT,
    )
    px = ((r.json() or {}).get("solana") or {}).get("usd")
    return (bool(px), f"SOL ${px}" if px else f"HTTP {r.status_code} (rate limit?)")


def _stonfi():
    import ton_signer

    sim = ton_signer.simulate(USDT_TON, str(10**9))
    if sim.get("error"):
        return False, str(sim["error"])[:90]
    return True, f"1 TON → {int(sim.get('ask_units') or 0) / 1e6:.2f} USDT"


def _trongrid():
    import tron_signer

    data = tron_signer._post("/wallet/getnowblock", {})
    num = ((data.get("block_header") or {}).get("raw_data") or {}).get("number")
    return (bool(num), f"block {num}" if num else str(data)[:90])


def _etherscan():
    key = (os.getenv("ETHERSCAN_API_KEY") or "").strip()
    if not key:
        return None, "ETHERSCAN_API_KEY not set — EVM wallet tracking off"
    r = requests.get(
        "https://api.etherscan.io/v2/api",
        params={"chainid": "1", "module": "proxy", "action": "eth_blockNumber", "apikey": key},
        timeout=TIMEOUT,
    )
    res = (r.json() or {}).get("result")
    ok = isinstance(res, str) and res.startswith("0x")
    return ok, (f"block {int(res, 16)}" if ok else str(res)[:90])


def _helius():
    key = (os.getenv("HELIUS_API_KEY") or "").strip()
    if not key:
        return None, "HELIUS_API_KEY not set — SOL copy-trade can't see swaps"
    r = requests.get(
        f"https://api-mainnet.helius-rpc.com/v0/addresses/{SOL_MINT}/transactions",
        params={"api-key": key, "limit": 1},
        timeout=TIMEOUT,
    )
    ok = r.status_code < 400 and isinstance(r.json(), list)
    return ok, ("parsed history OK" if ok else f"HTTP {r.status_code}")


def _pytoniq():
    try:
        import pytoniq  # noqa: F401
    except Exception:
        return False, "pytoniq not installed — TON buys disabled"
    return True, "installed"


def run_checks() -> list[Check]:
    probes: list[tuple[str, object]] = [
        ("Solana RPC", _sol_rpc),
        ("Jupiter (SOL swaps)", _jupiter),
        ("0x (EVM swaps)", _zerox),
        ("DexScreener", _dexscreener),
        ("GeckoTerminal", _geckoterminal),
        ("CoinGecko", _coingecko),
        ("STON.fi (TON swaps)", _stonfi),
        ("pytoniq (TON send)", _pytoniq),
        ("TronGrid", _trongrid),
        ("Etherscan", _etherscan),
        ("Helius", _helius),
    ]
    for cid in ("eth", "base", "bsc", "arb", "avax"):
        p = _evm_rpc(cid)
        if callable(p):
            probes.append((f"{cid.upper()} RPC", p))
        else:
            probes.append((f"{cid.upper()} RPC", lambda p=p: p))

    def run(item):
        name, fn = item
        return _timed(name, fn)

    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(run, probes))


def render(checks: list[Check], extra: list[str] | None = None) -> str:
    bad = [c for c in checks if c.ok is False]
    head = "🩺 <b>Desk health</b> — " + ("all green" if not bad else f"{len(bad)} down")
    lines = [head, ""]
    for c in checks:
        icon = "🟢" if c.ok else ("⚪️" if c.ok is None else "🔴")
        ms = f" · {c.ms}ms" if c.ok is not None else ""
        lines.append(f"{icon} <b>{html.escape(c.name)}</b>{ms}\n     {html.escape(c.detail)}")
    if extra:
        lines.append("")
        lines.extend(extra)
    return "\n".join(lines)
