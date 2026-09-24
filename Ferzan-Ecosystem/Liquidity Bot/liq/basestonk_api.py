"""
basestonk_api.py

Thin client for BaseStonk's machine API (/api/v1). We never hand our key
to BaseStonk -- it only ever returns an UNSIGNED transaction (`tx.to`,
`tx.data`, `tx.value`, `tx.chainId`); we sign and broadcast it ourselves
with the user's own linked wallet key. BaseStonk "never signs, relays or
broadcasts" (their own docs' words) -- and neither do we, on their behalf.

This intentionally leans on their server to do the hard part (reading the
pool's hook, the live anti-snipe/tax rate, and the AMM quote) instead of
us re-implementing Uniswap v4 hook math by hand -- that's the difference
between "probably right" and "definitely right" when real funds move.
"""

from __future__ import annotations

import logging
import os
import time

import requests

log = logging.getLogger("basestonk_api")

BASE_URL = "https://api.basestonk.io"
DOMAIN_NAME = "BaseStonk"
DOMAIN_VERSION = "1"

# bearer tokens are wallet+chain scoped and last 1h; minting is capped at
# 3/hour per IP on their side, so we reuse aggressively instead of minting
# a fresh one per trade.
_SESSION_CACHE: dict[tuple[str, int], dict] = {}


class BaseStonkError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body or {}


def _sig_for_session(key_hex: str, address: str, chain_id: int, action: str, expires_at_ms: int) -> str:
    from eth_account import Account

    full_message = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
            ],
            "Authorization": [
                {"name": "action", "type": "string"},
                {"name": "address", "type": "address"},
                {"name": "expiresAt", "type": "uint256"},
                {"name": "detail", "type": "string"},
            ],
        },
        "primaryType": "Authorization",
        "domain": {"name": DOMAIN_NAME, "version": DOMAIN_VERSION, "chainId": chain_id},
        "message": {"action": action, "address": address, "expiresAt": expires_at_ms, "detail": ""},
    }
    raw = "0x" + key_hex.replace("0x", "").replace("0X", "")
    # eth-account renamed/restructured its EIP-712 helper across versions --
    # newer releases (>=0.11ish) expose encode_typed_data(full_message=...),
    # older ones (this venv has 0.8.0) only have encode_structured_data(dict).
    # Support both so this doesn't silently break again on a version bump.
    try:
        from eth_account.messages import encode_typed_data

        signable = encode_typed_data(full_message=full_message)
    except ImportError:
        from eth_account.messages import encode_structured_data

        signable = encode_structured_data(full_message)
    signed = Account.sign_message(signable, private_key=raw)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def get_session(key_hex: str, address: str, chain_id: int, scopes: tuple[str, ...] = ("read", "prepare")) -> str:
    """Bearer token for this wallet+chain, minting a fresh one only when
    the cached one is missing or about to expire (< 90s left)."""
    key = (address.lower(), chain_id)
    cached = _SESSION_CACHE.get(key)
    now = time.time()
    if cached and cached["expires"] - now > 90:
        return cached["token"]

    expires_at_ms = int((now + 240) * 1000)  # signature itself must be <=5min out
    action = "agent:session:prepare" if "prepare" in scopes else "agent:session"
    sig = _sig_for_session(key_hex, address, chain_id, action, expires_at_ms)

    r = requests.post(
        f"{BASE_URL}/api/v1/agent/session",
        headers={
            "x-wallet-address": address,
            "x-wallet-signature": sig,
            "x-wallet-expires": str(expires_at_ms),
            "Content-Type": "application/json",
        },
        json={"scopes": list(scopes)},
        timeout=15,
    )
    body = r.json() if r.content else {}
    if r.status_code >= 400:
        err = body.get("error") or {}
        raise BaseStonkError(
            str(err.get("message") or body or r.text[:200]),
            status=r.status_code,
            code=err.get("code") if isinstance(err, dict) else None,
            body=body,
        )
    token = body.get("token")
    if not token:
        raise BaseStonkError("Session mint returned no token", body=body)
    _SESSION_CACHE[key] = {"token": token, "expires": now + 3600}
    return token


def prepare_trade(
    key_hex: str,
    address: str,
    chain_id: int,
    token: str,
    side: str,
    amount_in: int,
    slippage_bps: int = 300,
) -> dict:
    """side: 'buy' or 'sell'. amount_in is raw units of what you SPEND --
    the pair token (usually native) on a buy, the launched token on a sell."""
    bearer = get_session(key_hex, address, chain_id)
    r = requests.post(
        f"{BASE_URL}/api/v1/agent/trade/prepare",
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
        json={
            "chainId": chain_id,
            "token": token,
            "side": side,
            "amountIn": str(int(amount_in)),
            "slippageBps": int(slippage_bps),
        },
        timeout=20,
    )
    body = r.json() if r.content else {}
    if r.status_code == 422:
        # a prepare that failed its own checks -- never a silent 200.
        # Surface WHY: BaseStonk returns either an error.message, or a
        # verdict{ok:false, checks:[...]} block naming the failed check(s).
        # Without this detail every 422 looks identical in the logs.
        err = body.get("error") or {}
        verdict = body.get("verdict") or {}
        reason = (
            err.get("message")
            if isinstance(err, dict) and err.get("message")
            else (verdict.get("checks") if verdict else None)
        )
        raise BaseStonkError(
            f"Trade prepare failed its checks: {reason if reason is not None else body}",
            status=422,
            code="verdict_failed",
            body=body,
        )
    if r.status_code >= 400:
        err = body.get("error") or {}
        raise BaseStonkError(
            str(err.get("message") or body or r.text[:200]),
            status=r.status_code,
            code=err.get("code") if isinstance(err, dict) else None,
            body=body,
        )
    return body


def token_record(address: str, chain: str) -> dict:
    r = requests.get(f"{BASE_URL}/api/launchpad/tokens/{address}", params={"chain": chain}, timeout=15)
    if r.status_code >= 400:
        body = r.json() if r.content else {}
        raise BaseStonkError(str(body.get("error") or r.text[:200]), status=r.status_code)
    return r.json() if r.content else {}
