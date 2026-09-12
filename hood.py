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
        px = float(get_price_usd("ethereum") or 3000)
    except Exception:
        px = 3000.0
    wei = max(10**12, int((usd / max(px, 1e-9)) * 10**18))
    deadline = int(time.time()) + 600
    # offset path dynamic array after 4 * 32
    data = (
        SWAP_ETH
        + "0".zfill(64)  # amountOutMin
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
    bal = _erc20_balance(meta["rpc"], token, acct.address)
    if bal <= 0:
        return False, f"No token on Hood for {token}"
    approve = "0x095ea7b3" + _enc_addr(ROUTER) + ("f" * 64)
    ok, msg = _broadcast(acct, meta, token, approve, 0)
    if not ok:
        return False, "Hood approve failed: " + msg
    time.sleep(8)
    deadline = int(time.time()) + 600
    data = (
        SWAP_TOKEN
        + hex(bal)[2:].zfill(64)
        + "0".zfill(64)
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
