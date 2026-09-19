"""Sliced routing + liquidity probe.

Paper is the default. Live is ONE SIDE only (buy or sell), not both
in the same job — that is how wash volume is printed.

Live Solana uses Jupiter. Live EVM uses 0x. Caps are small on purpose.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field

import requests

from dex_data import Pool, estimate_price_impact_pct, fetch_token_pools, liquidity_score

JUPITER_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP = "https://quote-api.jup.ag/v6/swap"
ZEROX = "https://api.0x.org/swap/allowance-holder/quote"
NATIVE_EVM = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

CHAIN_IDS = {
    "ethereum": 1,
    "eth": 1,
    "base": 8453,
    "bsc": 56,
    "arbitrum": 42161,
    "arb": 42161,
    "avalanche": 43114,
    "avax": 43114,
    "polygon": 137,
    "optimism": 10,
    "op": 10,
}


def live_ok() -> bool:
    return (os.getenv("LIQ_LIVE") or "0").strip().lower() in {"1", "true", "yes"}


def max_usd() -> float:
    try:
        return max(1.0, min(50.0, float(os.getenv("LIQ_MAX_USD", "5"))))
    except ValueError:
        return 5.0


@dataclass
class Slice:
    n: int
    usd: float
    wait_s: float
    est_impact_pct: float
    note: str = "paper"


@dataclass
class Plan:
    address: str
    side: str
    pool: Pool
    score: int
    total_usd: float
    slices: list[Slice] = field(default_factory=list)
    live: bool = False


def make_plan(
    address: str,
    usd: float,
    side: str = "buy",
    slices: int = 4,
    interval_s: float = 20.0,
    jitter: float = 0.35,
) -> Plan:
    side = side.lower()
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell — not both.")
    usd = min(max(1.0, float(usd)), max_usd() if live_ok() else float(usd))
    slices = max(1, min(12, int(slices)))
    pools = fetch_token_pools(address)
    pool = pools[0]
    chunk = usd / slices
    out: list[Slice] = []
    for i in range(slices):
        wait = max(2.0, interval_s * (1.0 + random.uniform(-jitter, jitter)))
        impact = estimate_price_impact_pct(pool.liquidity_usd, chunk)
        out.append(Slice(n=i + 1, usd=chunk, wait_s=wait, est_impact_pct=impact))
    return Plan(
        address=address,
        side=side,
        pool=pool,
        score=liquidity_score(pool),
        total_usd=usd,
        slices=out,
        live=live_ok(),
    )


def plan_text(plan: Plan) -> str:
    mode = "LIVE" if plan.live else "PAPER"
    p = plan.pool
    lines = [
        f"{mode} {plan.side.upper()} plan · score {plan.score}/100",
        f"{p.base_symbol}/{p.quote_symbol} · {p.chain} · {p.dex}",
        f"TVL ${p.liquidity_usd:,.0f} · 24h vol ${p.volume_24h_usd:,.0f}",
        f"Total ${plan.total_usd:.2f} in {len(plan.slices)} slices",
        "",
    ]
    for s in plan.slices:
        lines.append(
            f"  {s.n}. ${s.usd:.2f}  wait ~{s.wait_s:.0f}s  est impact {s.est_impact_pct:.2f}%"
        )
    if plan.live:
        lines.append("\nLIVE is one-sided only. Cap $%s. No buy+sell loop." % f"{max_usd():.0f}")
    else:
        lines.append("\nPAPER. Set LIQ_LIVE=1 to send through Jupiter / 0x.")
    if p.liquidity_usd < 10_000:
        lines.append("Pool is thin. Even paper impact will look ugly.")
    return "\n".join(lines)


def jupiter_quote(mint: str, usd: float) -> dict:
    # Rough SOL size; caller should pass lamports if they have a SOL px.
    try:
        px = float(
            requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "solana", "vs_currencies": "usd"},
                timeout=10,
            ).json()["solana"]["usd"]
        )
    except Exception:
        px = 150.0
    lamports = max(10_000, int((usd / max(px, 1e-9)) * 1_000_000_000))
    r = requests.get(
        JUPITER_QUOTE,
        params={
            "inputMint": "So11111111111111111111111111111111111111112",
            "outputMint": mint,
            "amount": str(lamports),
            "slippageBps": "150",
        },
        timeout=15,
    )
    return r.json() if r.content else {}


def zerox_quote(chain: str, token: str, usd: float) -> dict:
    cid = CHAIN_IDS.get((chain or "").lower())
    if not cid:
        return {"message": f"No 0x chain id for {chain}"}
    key = (os.getenv("ZEROX_API_KEY") or "").strip()
    if not key:
        return {"message": "ZEROX_API_KEY missing"}
    try:
        px = float(
            requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "ethereum", "vs_currencies": "usd"},
                timeout=10,
            ).json()["ethereum"]["usd"]
        )
    except Exception:
        px = 3000.0
    wei = max(10**12, int((usd / max(px, 1e-9)) * 10**18))
    r = requests.get(
        ZEROX,
        headers={"0x-api-key": key, "0x-version": "v2"},
        params={
            "chainId": str(cid),
            "sellToken": NATIVE_EVM,
            "buyToken": token,
            "sellAmount": str(wei),
        },
        timeout=15,
    )
    return r.json() if r.content else {}


def quote_slice(plan: Plan, sl: Slice) -> str:
    """Live quote only. Does not broadcast. Ferzan signer does that."""
    addr = plan.address
    if addr.startswith("0x"):
        q = zerox_quote(plan.pool.chain, addr, sl.usd)
        buy = q.get("buyAmount") or q.get("grossBuyAmount")
        if buy:
            return f"0x quote ok · buyAmount {buy}"
        return str(q.get("message") or q.get("reason") or "0x no quote")[:180]
    q = jupiter_quote(addr, sl.usd)
    if q.get("outAmount"):
        return f"Jupiter quote ok · out {q.get('outAmount')}"
    return str(q.get("error") or q.get("message") or "Jupiter no quote")[:180]


def run_paper(plan: Plan) -> str:
    lines = [plan_text(plan), "", "Paper fills:"]
    t = 0.0
    for sl in plan.slices:
        t += sl.wait_s
        lines.append(
            f"t+{t:.0f}s  {plan.side} ${sl.usd:.2f}  "
            f"model impact {sl.est_impact_pct:.2f}%  filled (sim)"
        )
    return "\n".join(lines)


def run_live_quotes(plan: Plan) -> str:
    if not plan.live:
        return run_paper(plan)
    lines = [plan_text(plan), "", "Live quotes (not broadcast):"]
    for sl in plan.slices:
        try:
            note = quote_slice(plan, sl)
        except Exception as exc:
            note = str(exc)
        lines.append(f"slice {sl.n} ${sl.usd:.2f} · {note}")
        time.sleep(min(sl.wait_s, 8.0))
    lines.append(
        "\nBroadcast is Ferzan Trade Bot Buy/Sell. "
        "This desk only proves the router still quotes."
    )
    return "\n".join(lines)
