"""Robinhood Chain Uniswap V2 live swap. Chain id 4663."""

from __future__ import annotations

import time

from chains import CHAINS
from evm_signer import _addr, _as_int, _broadcast, _estimate_gas, _key_hex, live_enabled, max_usd

ROUTER = "0x89e5db8b5aa49aa85ac63f691524311aeb649eba"
WETH = "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73"
# swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)
SWAP_ETH = "0xb6f9de95"


def _enc_addr(addr: str) -> str:
    return _addr(addr)[2:].lower().zfill(64)


GET_AMOUNTS_OUT = "0xd06ca61f"


def _min_out(rpc: str, amount_in: int, path: list) -> int:
    """What the router says the swap returns, less HOOD_SLIP_BPS (default 20%), so a sandwich or a fast price move
    makes the swap revert instead of filling at any price. Raises when there is no quote: then nothing is sent."""
    from evm_signer import _rpc
    data = (GET_AMOUNTS_OUT + hex(int(amount_in))[2:].zfill(64) + "40".zfill(64) + hex(len(path))[2:].zfill(64)
            + "".join(_enc_addr(a) for a in path))
    body = _rpc(rpc, "eth_call", [{"to": ROUTER, "data": data}, "latest"])
    res = (body.get("result") or "")
    if body.get("error") or len(res) < 2 + 64 * (2 + len(path)):
        raise RuntimeError("no router quote for this token (no pool yet?)")
    out = int(res[-64:], 16)
    bps = max(100, min(9000, int(__import__("os").environ.get("HOOD_SLIP_BPS") or 2000)))
    mn = out * (10_000 - bps) // 10_000
    if mn <= 0:
        raise RuntimeError("router quote is zero (no liquidity)")
    return mn


def buy_hood(token: str, usd: float, key_hex: str | None = None) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live buys OFF."
    token = _addr(token)
    usd = min(max(1.0, float(usd)), max_usd())
    try:
        from eth_account import Account
        from price_fetcher import get_price_usd
    except Exception as exc:
        return False, str(exc)
    raw = (key_hex or _key_hex()).replace("0x", "").replace("0X", "")
    if not raw:
        return False, "No EVM key for Hood buy."
    acct = Account.from_key("0x" + raw)
    try:
        px = float(get_price_usd("ethereum") or 0)
    except Exception:
        px = 0.0
    if px <= 0:
        return False, "No live ETH price right now, so no buy was sent. Try again in a minute."
    wei = max(10**12, int((usd / max(px, 1e-9)) * 10**18))
    deadline = int(time.time()) + 600
    try:
        min_out = _min_out(CHAINS["hood"]["rpc"], wei, [WETH, token])
    except Exception as exc:
        return False, f"Hood buy not sent: {str(exc)[:120]}. Nothing was sent."
    # offset path dynamic array after 4 * 32
    data = (
        SWAP_ETH
        + hex(min_out)[2:].zfill(64)  # amountOutMin
        + "80".zfill(64)  # path offset
        + _enc_addr(acct.address)
        + hex(deadline)[2:].zfill(64)
        + "2".zfill(64)
        + _enc_addr(WETH)
        + _enc_addr(token)
    )
    meta = dict(CHAINS["hood"])
    ok, msg = _broadcast(acct, meta, ROUTER, "0x" + data if not data.startswith("0x") else data, wei)
    if not ok:
        return False, "Hood Uniswap buy failed: " + msg
    return True, f"Live HOOD buy ~${usd:.2f}\n{msg}"


SWAP_TOKEN = "0x791ac947"  # swapExactTokensForETHSupportingFeeOnTransferTokens


def sell_hood(token: str, key_hex: str | None = None) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live sells OFF."
    token = _addr(token)
    try:
        from eth_account import Account

        from evm_signer import _erc20_balance
    except Exception as exc:
        return False, str(exc)
    raw = (key_hex or _key_hex()).replace("0x", "").replace("0X", "")
    if not raw:
        return False, "No EVM key for Hood sell."
    acct = Account.from_key("0x" + raw)
    meta = dict(CHAINS["hood"])
    try:
        bal = _erc20_balance(meta["rpc"], token, acct.address)
    except Exception:
        return False, "Couldn't read your token balance on Hood (the node is busy). Nothing was sent. Tap sell again in a few seconds."
    if bal <= 0:
        return False, f"No token on Hood for {token}"
    try:
        min_out = _min_out(meta["rpc"], bal, [token, WETH])
    except Exception as exc:
        return False, f"Hood sell not sent: {str(exc)[:120]}. Nothing was sent."
    approve = "0x095ea7b3" + _enc_addr(ROUTER) + ("f" * 64)
    ok, msg = _broadcast(acct, meta, token, approve, 0)
    if not ok:
        return False, "Hood approve failed: " + msg
    time.sleep(8)
    deadline = int(time.time()) + 600
    data = (
        SWAP_TOKEN
        + hex(bal)[2:].zfill(64)
        + hex(min_out)[2:].zfill(64)
        + "a0".zfill(64)
        + _enc_addr(acct.address)
        + hex(deadline)[2:].zfill(64)
        + "2".zfill(64)
        + _enc_addr(token)
        + _enc_addr(WETH)
    )
    ok, msg2 = _broadcast(acct, meta, ROUTER, data, 0)
    note = f"Approved\n{msg}\n"
    if not ok:
        return False, note + "Hood sell failed: " + msg2
    return True, note + f"Live HOOD sell\n{msg2}"
