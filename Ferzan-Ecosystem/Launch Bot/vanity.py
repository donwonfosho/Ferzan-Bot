"""
vanity.py -- branded token addresses.

EVM (BNB / Base): the v3 factories deploy tokens with CREATE2. We ask the factory for the
token's init-code hash, search for a salt whose address ends in VANITY_EVM_SUFFIX (hex), and
confirm the address with the factory's predictToken() before building the transaction.
The salt is bound to the creator's wallet on-chain, so nobody can take the address.

Solana: mint addresses are just keypairs. vanity_grinder.py keeps a small pool of mint
keypairs ending in VANITY_SOL_SUFFIX in /opt/ferzan/dbc-keys/vanity-sol (outside git);
each launch takes one and it is deleted right away, so no key is ever used twice.

Every function here fails soft: if anything goes wrong the launch simply uses a normal address.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

EVM_SUFFIX = (os.environ.get("VANITY_EVM_SUFFIX") or "f222").strip().lower()
SOL_SUFFIX = (os.environ.get("VANITY_SOL_SUFFIX") or "fzn").strip()
SOL_POOL = Path(os.environ.get("VANITY_SOL_DIR") or "/opt/ferzan/dbc-keys/vanity-sol")
EVM_MAX_SECONDS = float(os.environ.get("VANITY_EVM_MAX_SECONDS") or "12")

if not re.fullmatch(r"[0-9a-f]{0,6}", EVM_SUFFIX):
    EVM_SUFFIX = ""
if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{0,5}", SOL_SUFFIX):
    SOL_SUFFIX = ""

_STR, _U256, _ADDR, _B32, _BOOL = ("string", "uint256", "address", "bytes32", "bool")


def _fn(name, ins, outs, mut="view"):
    return {"name": name, "type": "function", "stateMutability": mut,
            "inputs": [{"name": f"a{i}", "type": t} for i, t in enumerate(ins)],
            "outputs": [{"name": "", "type": t} for t in outs]}


_CURVE_PARAMS = {
    "name": "p", "type": "tuple",
    "components": [
        {"name": "name", "type": "string"}, {"name": "symbol", "type": "string"},
        {"name": "totalSupply", "type": "uint256"}, {"name": "gradTarget", "type": "uint256"},
        {"name": "startTime", "type": "uint256"}, {"name": "maxBuyPerWallet", "type": "uint256"},
        {"name": "allocWallets", "type": "address[]"}, {"name": "allocBps", "type": "uint256[]"},
    ],
}
CURVE_V3_ABI = [
    _fn("tokenInitCodeHash", [_STR, _STR, _U256], [_B32]),
    _fn("predictToken", [_ADDR, _B32, _STR, _STR, _U256], [_ADDR]),
    {"name": "launchWithSalt", "type": "function", "stateMutability": "payable",
     "inputs": [_CURVE_PARAMS, {"name": "salt", "type": "bytes32"}],
     "outputs": [{"name": "curveAddress", "type": "address"}, {"name": "tokenAddress", "type": "address"}]},
]
PLAIN_V3_ABI = [
    _fn("tokenInitCodeHash", [_STR, _STR, _U256, _ADDR, _STR, _BOOL], [_B32]),
    _fn("predictToken", [_ADDR, _B32, _STR, _STR, _U256, _STR, _BOOL], [_ADDR]),
    _fn("launchTokenWithSalt", [_STR, _STR, _U256, _STR, _B32], [_ADDR], "payable"),
    _fn("launchTokenWithAllocAndSalt", [_STR, _STR, _U256, _STR, "address[]", "uint256[]", _B32], [_ADDR], "payable"),
]


def _keccak():
    try:
        from eth_hash.auto import keccak
        return keccak
    except Exception:
        from eth_utils import keccak
        return keccak


def find_salt(factory: str, init_hash: bytes, creator: str, suffix: str = EVM_SUFFIX,
              max_seconds: float = EVM_MAX_SECONDS) -> bytes | None:
    """Salt such that CREATE2(factory, keccak(abi.encode(creator, salt)), init_hash) ends in `suffix`."""
    if not suffix:
        return None
    k = _keccak()
    head = b"\xff" + bytes.fromhex(factory[2:])
    who = b"\x00" * 12 + bytes.fromhex(creator[2:])
    base = os.urandom(24)
    n = len(suffix)
    deadline = time.time() + max_seconds
    i = 0
    while True:
        salt = base + i.to_bytes(8, "big")
        addr = k(head + k(who + salt) + init_hash)[12:]
        if addr.hex()[-n:] == suffix:
            return salt
        i += 1
        if (i & 0x3FFF) == 0 and time.time() > deadline:
            return None


def _ends(addr: str, suffix: str) -> bool:
    return bool(suffix) and str(addr).lower().endswith(suffix)


def evm_curve_salted(builder, creator: str, params: tuple):
    """launchWithSalt(...) call for a v3 curve factory, or None (v2 factory / no match)."""
    if not EVM_SUFFIX:
        return None
    c = builder.w3.eth.contract(address=builder.factory.address, abi=CURVE_V3_ABI)
    name, symbol, supply = params[0], params[1], int(params[2])
    try:
        init_hash = c.functions.tokenInitCodeHash(name, symbol, supply).call()
    except Exception:
        return None  # older factory without vanity support
    salt = find_salt(builder.factory.address, bytes(init_hash), creator)
    if not salt:
        return None
    predicted = c.functions.predictToken(creator, salt, name, symbol, supply).call()
    if not _ends(predicted, EVM_SUFFIX):
        return None
    return c.functions.launchWithSalt(params, salt)


def evm_plain_salted(builder, creator: str, name: str, symbol: str, supply: int, url: str,
                     wallets: list, bps: list, use_alloc: bool):
    """launchTokenWithSalt / launchTokenWithAllocAndSalt for a v3 plain factory, or None."""
    if not EVM_SUFFIX:
        return None
    c = builder.w3.eth.contract(address=builder.factory.address, abi=PLAIN_V3_ABI)
    try:
        init_hash = c.functions.tokenInitCodeHash(name, symbol, int(supply), creator, url, bool(use_alloc)).call()
    except Exception:
        return None
    salt = find_salt(builder.factory.address, bytes(init_hash), creator)
    if not salt:
        return None
    predicted = c.functions.predictToken(creator, salt, name, symbol, int(supply), url, bool(use_alloc)).call()
    if not _ends(predicted, EVM_SUFFIX):
        return None
    if use_alloc:
        return c.functions.launchTokenWithAllocAndSalt(name, symbol, int(supply), url, wallets, bps, salt)
    return c.functions.launchTokenWithSalt(name, symbol, int(supply), url, salt)


# ------------------------------------------------------------------ Solana --
def sol_pool_size() -> int:
    try:
        return sum(1 for p in SOL_POOL.glob("*.json"))
    except OSError:
        return 0


def sol_take_mint() -> list | None:
    """Claim one pre-ground mint keypair (64-byte secret as a list of ints), or None."""
    if not SOL_SUFFIX:
        return None
    try:
        files = sorted(SOL_POOL.glob("*.json"))
    except OSError:
        return None
    for f in files:
        claimed = f.with_suffix(".taken")
        try:
            os.rename(f, claimed)  # atomic: only one launch can win this key
        except OSError:
            continue
        try:
            secret = json.loads(claimed.read_text())
        finally:
            try:
                claimed.unlink()
            except OSError:
                pass
        if isinstance(secret, list) and len(secret) == 64 and f.stem.endswith(SOL_SUFFIX):
            return secret
    return None
