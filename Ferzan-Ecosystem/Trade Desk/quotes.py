"""Unsigned live quotes with the platform cut attached.

Jupiter (Solana) and 0x (EVM) return a price. The user signs in their
own wallet. This process never sees a private key.

Fee: platformFeeBps / swapFeeBps = FEE_BPS (default 50 = 0.50%).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests

import fees
from chains import CHAINS, resolve_chain

JUP_LITE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_KEYED = "https://api.jup.ag/swap/v1/quote"
ZEROX = "https://api.0x.org/swap/allowance-holder/price"
TIMEOUT = 12

SOL_MINT = "So11111111111111111111111111111111111111112"
NATIVE_EVM = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

WSOL_USD_FALLBACK = 100.0


@dataclass
class LiveQuote:
    chain: str
    label: str
    in_symbol: str
    out_mint: str
    usd: float
    out_amount_raw: str
    fee_usd: float
    fee_bps: int
    fee_wallet: str
    router: str
    ready: bool
    detail: str
    jup_url: str | None = None


class QuoteError(Exception):
    pass


def _jup_headers() -> dict[str, str]:
    key = os.getenv("JUPITER_API_KEY", "").strip()
    h = {"Accept": "application/json"}
    if key:
        h["x-api-key"] = key
    return h


def sol_quote(output_mint: str, usd: float) -> LiveQuote:
    bps = fees.current_bps()
    wallets = fees.fee_wallets()
    fee_acct = wallets["jupiter_fee_account"] or wallets["sol"]
    cut = fees.quote(usd)
    # Convert USD → approx lamports of SOL using a coarse mark.
    try:
        from price_fetcher import get_price_usd

        sol_px = get_price_usd("solana") or WSOL_USD_FALLBACK
    except Exception:
        sol_px = WSOL_USD_FALLBACK
    lamports = max(1, int((usd / max(sol_px, 1e-9)) * 1_000_000_000))
    params = {
        "inputMint": SOL_MINT,
        "outputMint": output_mint,
        "amount": str(lamports),
        "slippageBps": "100",
        "platformFeeBps": str(bps),
    }
    url = JUP_KEYED if os.getenv("JUPITER_API_KEY", "").strip() else JUP_LITE
    try:
        r = requests.get(url, params=params, headers=_jup_headers(), timeout=TIMEOUT)
        data = r.json() if r.content else {}
    except requests.RequestException as exc:
        raise QuoteError(f"Jupiter unreachable: {exc}") from exc
    if r.status_code >= 400 or data.get("error"):
        raise QuoteError(str(data.get("error") or data.get("message") or r.text[:180]))
    out_amt = str(data.get("outAmount") or "")
    ready = bool(fee_acct)
    jup_link = f"https://jup.ag/swap/SOL-{output_mint}"
    detail = (
        f"Jupiter quoted {out_amt or '?'} raw out for ~${usd:.2f} SOL in. "
        f"Cut {bps / 100:.2f}% (${cut.fee_usd:.4f}) "
        + ("lands on your fee account when the user signs." if ready else "needs FEE_WALLET_SOL / JUPITER_FEE_ACCOUNT before live skims settle.")
    )
    return LiveQuote(
        chain="sol",
        label="Solana · Jupiter",
        in_symbol="SOL",
        out_mint=output_mint,
        usd=usd,
        out_amount_raw=out_amt,
        fee_usd=cut.fee_usd,
        fee_bps=bps,
        fee_wallet=fee_acct,
        router="Jupiter",
        ready=ready,
        detail=detail,
        jup_url=jup_link,
    )


def evm_quote(chain: str, buy_token: str, usd: float) -> LiveQuote:
    cid = resolve_chain(chain)
    if not cid or CHAINS[cid]["kind"] != "evm":
        raise QuoteError("Use eth, base, bsc, or hood.")
    meta = CHAINS[cid]
    bps = fees.current_bps()
    cut = fees.quote(usd)
    fee_wallet = fees.fee_wallets()["evm"]
    key = os.getenv("ZEROX_API_KEY", "").strip()
    if not key:
        return LiveQuote(
            chain=cid,
            label=f"{meta['label']} · 0x",
            in_symbol=meta["native"],
            out_mint=buy_token,
            usd=usd,
            out_amount_raw="",
            fee_usd=cut.fee_usd,
            fee_bps=bps,
            fee_wallet=fee_wallet,
            router="0x",
            ready=False,
            detail=(
                f"Set ZEROX_API_KEY to pull a firm 0x quote. "
                f"Cut {bps / 100:.2f}% (${cut.fee_usd:.4f}) would go to FEE_WALLET_EVM."
            ),
        )
    # Rough native amount: skip precise FX; 0x wants sellAmount in wei.
    # Use $usd / $3000 * 1e18 as ETH-like default; BNB closer to $600.
    native_px = 600.0 if cid == "bsc" else 3000.0
    wei = max(1, int((usd / native_px) * 10**18))
    params = {
        "chainId": str(meta["chain_id"]),
        "sellToken": NATIVE_EVM,
        "buyToken": buy_token,
        "sellAmount": str(wei),
        "swapFeeBps": str(min(bps, 1000)),
        "swapFeeToken": NATIVE_EVM,
    }
    if fee_wallet:
        params["swapFeeRecipient"] = fee_wallet
    try:
        r = requests.get(
            ZEROX,
            params=params,
            headers={"0x-api-key": key, "0x-version": "v2", "Accept": "application/json"},
            timeout=TIMEOUT,
        )
        data = r.json() if r.content else {}
    except requests.RequestException as exc:
        raise QuoteError(f"0x unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise QuoteError(str(data.get("reason") or data.get("message") or r.text[:180]))
    buy_amt = str((data.get("buyAmount") or data.get("liquidityAvailable") or ""))
    return LiveQuote(
        chain=cid,
        label=f"{meta['label']} · 0x",
        in_symbol=meta["native"],
        out_mint=buy_token,
        usd=usd,
        out_amount_raw=buy_amt,
        fee_usd=cut.fee_usd,
        fee_bps=bps,
        fee_wallet=fee_wallet,
        router="0x",
        ready=bool(fee_wallet),
        detail=f"0x price quote ready. Cut {bps / 100:.2f}% (${cut.fee_usd:.4f}) to FEE_WALLET_EVM when signed.",
    )


def format_quote(q: LiveQuote) -> str:
    wallet = q.fee_wallet[:8] + "…" if q.fee_wallet and len(q.fee_wallet) > 10 else (q.fee_wallet or "not set")
    lines = [
        f"{q.label}",
        f"Spend ~${q.usd:.2f} {q.in_symbol} → {q.out_mint[:12]}…",
        f"Platform cut {q.fee_bps / 100:.2f}% = ${q.fee_usd:.4f}",
        f"Fee wallet: {wallet}",
        q.detail,
        "",
        "This bot does not sign. Open the link in your wallet, or sign on a machine you control.",
    ]
    if q.jup_url:
        lines.append(q.jup_url)
    return "\n".join(lines)
