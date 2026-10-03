"""Native-coin balances (the gas coins) across every main chain, read in parallel and remembered briefly.

Reads are independent network calls, so they run together in a small pool. A read that fails is
reported as unknown (None), never as 0. Results are remembered per (chain, address) for a short time
so the bag header and the Balances screen do not each pay for the same lookups.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor

# key, label, symbol, kind, CoinGecko id (for the USD value)
CHAINS = (
    ("sol", "Solana", "SOL", "sol", "solana"),
    ("eth", "Ethereum", "ETH", "evm", "ethereum"),
    ("base", "Base", "ETH", "evm", "ethereum"),
    ("bsc", "BNB Chain", "BNB", "evm", "binancecoin"),
    ("arb", "Arbitrum", "ETH", "evm", "ethereum"),
    ("avax", "Avalanche", "AVAX", "evm", "avalanche-2"),
    ("hood", "Robinhood Chain", "ETH", "evm", "ethereum"),
    ("ton", "TON", "TON", "ton", "the-open-network"),
    ("trx", "Tron", "TRX", "tron", "tron"),
)
BY_KEY = {c[0]: c for c in CHAINS}

_CACHE: dict = {}
_PENDING: dict = {}
_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=10, thread_name_prefix="natives")


def _run(ck, fn):
    try:
        v = float(fn())
    except Exception:  # noqa: BLE001 - unknown, not zero
        v = None
    if v is not None:
        with _LOCK:
            _CACHE[ck] = (time.time(), v)
    return v


def prime(readers: dict, ttl: float = 30.0) -> None:
    """Start any read that is not already fresh or in flight. readers: {key: (address, fn -> float)}. Never blocks."""
    now = time.time()
    for key, (addr, fn) in readers.items():
        ck = (key, addr)
        with _LOCK:
            hit = _CACHE.get(ck)
            if hit and now - hit[0] < ttl:
                continue
            fut = _PENDING.get(ck)
            if fut is not None and not fut.done():
                continue
            _PENDING[ck] = _POOL.submit(_run, ck, fn)


def collect(readers: dict, wait: float = 2.0, stale: float = 120.0) -> dict:
    """{key: amount or None}. Waits up to `wait` seconds in total for reads still running."""
    deadline = time.time() + max(0.0, wait)
    out: dict = {}
    for key, (addr, _fn) in readers.items():
        ck = (key, addr)
        fut: Future | None = _PENDING.get(ck)
        if fut is not None and not fut.done():
            try:
                fut.result(timeout=max(0.0, deadline - time.time()))
            except Exception:  # noqa: BLE001
                pass
        hit = _CACHE.get(ck)
        out[key] = hit[1] if hit and time.time() - hit[0] < stale else None
    return out


def _fmt(x: float) -> str:
    if x >= 100:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:.3f}"
    return f"{x:.4f}".rstrip("0").rstrip(".") if x > 0 else "0"


def header_line(values: dict) -> str:
    """'💰 0.4972 SOL · 0.0428 ETH Base · 0.12 BNB' - only chains holding something. Unknown reads are flagged."""
    bits, unknown = [], 0
    for key, label, sym, _k, _g in CHAINS:
        v = values.get(key, 0.0)
        if v is None:
            unknown += 1
        elif v > 0:
            tag = "" if key in ("sol", "ton", "trx") or sym in ("BNB", "AVAX") else f" {label.split()[0]}"
            bits.append(f"{_fmt(v)} {sym}{tag}")
    line = "💰 " + (" · ".join(bits) if bits else "no gas coins yet")
    if unknown:
        line += f" · ⚠️ {unknown} unread"
    return line


def balances_text(values: dict, prices: dict, wallet_label: str) -> str:
    """The full Balances screen (HTML). prices: {coingecko id: usd or None}."""
    rows, empty, total, unknown = [], [], 0.0, []
    for key, label, sym, _k, gid in CHAINS:
        v = values.get(key, 0.0)
        if v is None:
            unknown.append(label)
        elif v <= 0:
            empty.append(label)
        else:
            px = prices.get(gid)
            usd = v * px if px else None
            if usd:
                total += usd
            rows.append(f"<b>{sym}</b> · {label}\n    {_fmt(v)}" + (f"  ≈ ${usd:,.2f}" if usd else ""))
    head = f"💰 <b>Balances</b> · {wallet_label}\n"
    body = "\n".join(rows) if rows else "No gas coins in this wallet yet. Open Wallets to deposit."
    foot = ""
    if total > 0:
        foot += f"\n\n<b>Total ≈ ${total:,.2f}</b>"
    if empty:
        foot += "\n<i>Empty: " + ", ".join(empty) + "</i>"
    if unknown:
        foot += "\n⚠️ Couldn't read: " + ", ".join(unknown) + ". Tap Refresh."
    return head + "\n" + body + foot
