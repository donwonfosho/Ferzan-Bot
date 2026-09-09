"""Transparent trading cut.

Paper fills take FEE_BPS from notional and write a ledger row.
Live fills do not custody funds. When you turn on Jupiter / 0x later,
the same bps is passed as a platform/affiliate fee to FEE_WALLET_*.

Banana Gun takes 0.5–1% inside a custodial swap. We take the same
idea without holding keys: the router pays your fee account on-chain.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import db

DEFAULT_FEE_BPS = int(os.getenv("FEE_BPS", "50"))  # 50 = 0.50%
MAX_FEE_BPS = 100  # 1.00% hard cap. Do not get greedy.


@dataclass
class FeeQuote:
    bps: int
    notional_usd: float
    fee_usd: float
    working_usd: float

    def disclose(self) -> str:
        pct = self.bps / 100.0
        return (
            f"Platform cut {pct:.2f}% = ${self.fee_usd:,.4f} "
            f"on ${self.notional_usd:,.2f} notional "
            f"(${self.working_usd:,.2f} works the trade)."
        )


def current_bps() -> int:
    raw = int(os.getenv("FEE_BPS", str(DEFAULT_FEE_BPS)))
    return max(0, min(MAX_FEE_BPS, raw))


def quote(notional_usd: float, bps: int | None = None) -> FeeQuote:
    bps = current_bps() if bps is None else max(0, min(MAX_FEE_BPS, bps))
    fee = round(notional_usd * bps / 10_000.0, 6)
    working = max(0.0, notional_usd - fee)
    return FeeQuote(bps=bps, notional_usd=notional_usd, fee_usd=fee, working_usd=working)


def record(user_id: int, kind: str, fq: FeeQuote, note: str = "") -> None:
    db.add_fee(
        user_id=user_id,
        kind=kind,
        notional_usd=fq.notional_usd,
        fee_bps=fq.bps,
        fee_usd=fq.fee_usd,
        note=note,
    )


def fee_wallets() -> dict[str, str]:
    return {
        "sol": os.getenv("FEE_WALLET_SOL", "").strip(),
        "evm": os.getenv("FEE_WALLET_EVM", "").strip(),
        "jupiter_fee_account": os.getenv("JUPITER_FEE_ACCOUNT", "").strip(),
    }


def live_ready() -> tuple[bool, str]:
    wallets = fee_wallets()
    if wallets["sol"] or wallets["evm"] or wallets["jupiter_fee_account"]:
        return True, "Live fee accounts are set. Router will pay them when swaps are enabled."
    return False, "No fee wallets set yet. Paper ledger still records the cut."


def jupiter_quote_params(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Params to attach to Jupiter /quote so the cut lands in your token account."""
    params = {"platformFeeBps": str(current_bps())}
    acct = fee_wallets()["jupiter_fee_account"]
    if acct:
        params["feeAccountHint"] = acct
    if extra:
        params.update(extra)
    return params


def zerox_quote_params() -> dict[str, str]:
    evm = fee_wallets()["evm"]
    params = {"swapFeeBps": str(current_bps())}
    if evm:
        params["swapFeeRecipient"] = evm
    return params
