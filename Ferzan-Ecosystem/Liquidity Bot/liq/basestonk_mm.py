"""
basestonk_mm.py

Volume-generation market-making loop for BaseStonk-launched tokens on
Base (8453) and Robinhood Chain (4663). Pastes a CA, alternates small
buy/sell round-trips against the user's OWN linked Ferzan wallet (the
same non-custodial wallet Ferzan Trade Bot already generated for them --
we read it, we never create or hold a separate one), signing and
broadcasting every transaction ourselves. Quoting and hook/tax math is
delegated to BaseStonk's own /agent/trade/prepare (see basestonk_api.py)
so we're never guessing at their Uniswap v4 hook internals.

NOT implemented in this version: actual liquidity provision (minting a
Uniswap v4 LP position). That's a materially different, separate build
(position manager, range selection, IL exposure) and ships as a later
phase. This module is volume mode only.

Money moves here. Test with a $2-3 budget on a token you don't mind
losing gas on before trusting this with anything bigger.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import random
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import requests

import basestonk_api as api

log = logging.getLogger("basestonk_mm")

# ---- chain config ---------------------------------------------------

CHAINS = {
    "base": {
        "chain_id": 8453,
        "rpc": os.getenv("BASE_RPC", "https://mainnet.base.org"),
        "explorer_tx": "https://basescan.org/tx/{txid}",
        "native_cg": "ethereum",
        # canonical WETH -- BaseStonk's own "funded" check wants actual
        # wrapped balance for WETH-paired pools, not raw native ETH.
        "weth": "0x4200000000000000000000000000000000000006",
        "api_chain": "base",  # value BaseStonk's REST API expects for ?chain=
    },
    "hood": {
        "chain_id": 4663,
        "rpc": os.getenv("HOOD_RPC", "https://rpc.mainnet.chain.robinhood.com"),
        "explorer_tx": "https://robinhoodchain.blockscout.com/tx/{txid}",
        "native_cg": "ethereum",
        "weth": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73",
        "api_chain": "robinhood",  # TODO: confirm against a real hood token_record call
    },
}
CHAIN_ALIASES = {"base": "base", "robinhood": "hood", "hood": "hood", "rh": "hood", "rhc": "hood"}

MM_DB_PATH = Path(os.getenv("MM_DB_PATH", str(Path(__file__).resolve().parent / "liq_mm.db")))

# session guardrails
MIN_TRADE_USD = 1.0
MAX_TRADE_USD = 25.0
MIN_BUDGET_USD = 5.0
MAX_BUDGET_USD = 1000.0
MAX_MINUTES = 6 * 60
DWELL_MIN_S = 20
DWELL_MAX_S = 90
MAX_CONSECUTIVE_FAILS = 3

_active: dict[int, dict] = {}  # user_id -> {"task": Task, "stop": bool, "session": dict}


# ---- shared wallet (same DB Ferzan Trade Bot writes to) --------------

FERZAN_DB_PATH = Path(os.getenv("DB_PATH", "/opt/ferzan/app/ferzan.db"))
MASTER_PATH = Path(os.getenv("FERZAN_MASTER_PATH", "/opt/ferzan/app/.master"))


def _fernet():
    from cryptography.fernet import Fernet

    secret = (os.getenv("FERZAN_MASTER_KEY") or "").strip()
    if not secret:
        if MASTER_PATH.exists():
            secret = MASTER_PATH.read_text().strip()
        else:
            raise RuntimeError("No FERZAN_MASTER_KEY and no .master file -- can't read the shared wallet store.")
    digest = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def get_linked_evm_key(user_id: int) -> tuple[str, str] | None:
    """Returns (evm_address, evm_private_key_hex) for the user's existing
    Ferzan wallet, or None if they haven't opened Trade Bot yet."""
    if not FERZAN_DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(FERZAN_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT evm_pub, evm_key FROM user_wallets WHERE user_id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["evm_pub"]:
        return None
    key = _fernet().decrypt(row["evm_key"].encode()).decode()
    return row["evm_pub"], key


# ---- mm session log (own db, doesn't touch ferzan.db) ----------------


@contextmanager
def _mmdb():
    conn = sqlite3.connect(str(MM_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_mmdb():
    with _mmdb() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS mm_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chain TEXT NOT NULL,
                token TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL,
                budget_usd REAL NOT NULL,
                spent_usd REAL NOT NULL DEFAULT 0,
                trades INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running'
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS mm_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                side TEXT NOT NULL,
                ts REAL NOT NULL,
                usd REAL,
                tx_hash TEXT,
                ok INTEGER NOT NULL,
                note TEXT
            )"""
        )


_init_mmdb()


# ---- low-level chain plumbing (sign + broadcast only; quoting via API) --


def _rpc(rpc: str, method: str, params: list) -> dict:
    r = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    return r.json() if r.content else {}


def _nonce(rpc: str, addr: str) -> int:
    body = _rpc(rpc, "eth_getTransactionCount", [addr, "pending"])
    return int(body.get("result") or "0x0", 16)


def _gas_price(rpc: str) -> int:
    body = _rpc(rpc, "eth_gasPrice", [])
    return int(body.get("result") or "0x77359400", 16)


def _native_balance_wei(rpc: str, addr: str) -> int:
    body = _rpc(rpc, "eth_getBalance", [addr, "latest"])
    return int(body.get("result") or "0x0", 16)


def _erc20_balance(rpc: str, token: str, owner: str) -> int:
    data = "0x70a08231" + owner[2:].lower().zfill(64)
    body = _rpc(rpc, "eth_call", [{"to": token, "data": data}, "latest"])
    val = body.get("result") or "0x0"
    try:
        return int(val, 16)
    except Exception:
        return 0


def _native_usd(cg_id: str) -> float:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": cg_id, "vs_currencies": "usd"},
            timeout=10,
        )
        return float((r.json() or {}).get(cg_id, {}).get("usd") or 0)
    except Exception:
        return 0.0


def _sign_and_send(rpc: str, chain_id: int, key_hex: str, tx: dict) -> tuple[bool, str]:
    from eth_account import Account
    from eth_utils import to_checksum_address

    raw = key_hex.replace("0x", "").replace("0X", "")
    acct = Account.from_key("0x" + raw)
    full_tx = {
        # eth-account's legacy validator requires a proper EIP-55 checksummed
        # 'to' -- plain lowercase (what BaseStonk's API returns token/router
        # addresses as) gets rejected with "Transaction had invalid fields".
        # Normalize here so it never matters what casing a caller passes in.
        "to": to_checksum_address(tx["to"]),
        "data": tx["data"] if str(tx["data"]).startswith("0x") else "0x" + str(tx["data"]),
        "value": int(tx.get("value") or 0),
        "chainId": int(tx.get("chainId") or chain_id),
        "gas": int(tx.get("gas") or 400000),
        "gasPrice": int(_gas_price(rpc) * 1.15),
        "nonce": _nonce(rpc, acct.address),
    }
    signed = acct.sign_transaction(full_tx)
    raw_hex = "0x" + signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex
    body = _rpc(rpc, "eth_sendRawTransaction", [raw_hex])
    if body.get("error"):
        err = body["error"]
        return False, str(err.get("message") if isinstance(err, dict) else err)
    txh = body.get("result") or ""
    if not txh:
        return False, "RPC accepted nothing."
    return True, txh


def _wait_receipt(rpc: str, txh: str, tries: int = 12, delay: float = 3.0) -> bool | None:
    """True = confirmed success, False = confirmed revert, None = still pending after tries."""
    for _ in range(tries):
        body = _rpc(rpc, "eth_getTransactionReceipt", [txh])
        res = body.get("result")
        if res:
            status = res.get("status")
            return status in ("0x1", 1)
        time.sleep(delay)
    return None


def _wrap_native(rpc: str, chain_id: int, key_hex: str, weth_addr: str, amount_wei: int) -> tuple[bool, str]:
    """Wrap native ETH into WETH via the canonical WETH contract's
    deposit() -- selector 0xd0e30db0, no args, amount carried in tx.value.
    1:1, no slippage, no approval needed for the wrap itself."""
    tx = {"to": weth_addr, "data": "0xd0e30db0", "value": amount_wei, "chainId": chain_id, "gas": 80000}
    return _sign_and_send(rpc, chain_id, key_hex, tx)


def _unwrap_native(rpc: str, chain_id: int, key_hex: str, weth_addr: str, amount_wei: int) -> tuple[bool, str]:
    """Unwrap WETH back to native ETH via withdraw(uint256) -- selector
    0x2e1a7d4d. Used after a sell that lands in WETH, so the wallet's
    native balance (what session budgeting/checks look at) stays accurate."""
    data = "0x2e1a7d4d" + hex(int(amount_wei))[2:].zfill(64)
    tx = {"to": weth_addr, "data": data, "value": 0, "chainId": chain_id, "gas": 80000}
    return _sign_and_send(rpc, chain_id, key_hex, tx)


_PAIR_CACHE: dict[tuple[str, str], str | None] = {}


def _pair_token(api_chain: str, token: str) -> str | None:
    """The ERC20 address this token's pool actually pairs against, lowercased
    (e.g. WETH, BSTONK, USDC -- whatever BaseStonk launched it against).
    Cached indefinitely per process -- a token's pair is fixed at launch."""
    key = (api_chain, token.lower())
    if key in _PAIR_CACHE:
        return _PAIR_CACHE[key]
    try:
        rec = api.token_record(token, api_chain)
        pair = ((rec.get("token") or {}).get("pairToken") or "").lower() or None
    except Exception:
        log.exception("token_record lookup failed for %s on %s", token, api_chain)
        pair = None
    _PAIR_CACHE[key] = pair
    return pair


# ---- multi-hop pair-chain resolution -----------------------------------
# Not every BaseStonk token pairs against WETH directly. CYBERCAB, for
# example, pairs against BSTONK (BaseStonk's own platform token), and
# BSTONK itself pairs against USDC -- not ETH. Walk that chain outward
# from the target token until we either reach WETH (a fully BaseStonk-
# native funding path, however many hops) or hit a currency BaseStonk
# doesn't itself track (its token_record lookup 404s -- that's the
# signal it's a "major"/terminal currency like USDC, not something
# anyone launched on the platform). A terminal currency needs exactly
# one plain Uniswap V3 swap against WETH; BaseStonk's own trade/prepare
# API only ever knows how to trade a token against its own pair.

_CHAIN_CACHE: dict[tuple[str, str], tuple[list[str], bool] | None] = {}


def _pair_chain(api_chain: str, token: str, weth: str, max_hops: int = 4) -> tuple[list[str] | None, bool | None]:
    """Returns (chain, needs_uni):
      chain: currencies from token's direct pair outward, e.g.
             CYBERCAB -> [BSTONK, USDC].
      needs_uni: False if chain[-1] == weth (fully BaseStonk-native);
                 True if chain[-1] is a terminal currency needing one
                 Uniswap V3 swap against WETH to fund.
    Returns (None, None) on a genuine lookup failure (network/5xx) or a
    chain that's too long/cyclic -- callers must treat that as "try again",
    never as "this token has no path", since a terminal currency and a
    transient API error look different (404 vs. anything else)."""
    key = (api_chain, token.lower())
    if key in _CHAIN_CACHE:
        cached = _CHAIN_CACHE[key]
        return cached if cached is not None else (None, None)

    chain: list[str] = []
    cur = token.lower()
    seen = {cur}
    result: tuple[list[str], bool] | None = None
    for _ in range(max_hops):
        try:
            rec = api.token_record(cur, api_chain)
        except api.BaseStonkError as exc:
            if exc.status == 404:
                result = (chain, True)
            break
        except Exception:
            log.exception("token_record lookup failed for %s on %s", cur, api_chain)
            break
        pair = ((rec.get("token") or {}).get("pairToken") or "").lower()
        if not pair:
            break
        chain.append(pair)
        if pair == weth:
            result = (chain, False)
            break
        if pair in seen:
            break  # cycle guard -- leave result as None (abort)
        seen.add(pair)
        cur = pair

    _CHAIN_CACHE[key] = result
    return result if result is not None else (None, None)


# ---- Uniswap V3 fallback -------------------------------------------------
# Only used for the one leg of a funding chain BaseStonk itself can't
# route (a terminal currency like USDC). Addresses below are Uniswap's
# own canonical Base mainnet deployments, cross-checked against Uniswap's
# official docs and BaseScan's own contract labels before being hardcoded
# here -- this is the one place in this file that signs an approval to a
# contract we didn't get the address for directly from BaseStonk's API.

UNISWAP_V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
UNISWAP_V3_QUOTER = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
UNISWAP_V3_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"  # SwapRouter02 (no-deadline variant)
UNISWAP_FEE_TIERS = (500, 3000, 100, 10000)  # canonical/highest-liquidity tiers first
MAX_UINT256 = (1 << 256) - 1
SWAP_SLIPPAGE_BPS = 300  # matches the BaseStonk-leg default


def _enc_addr(a: str) -> str:
    return a.lower().replace("0x", "").rjust(64, "0")


def _enc_uint(n: int) -> str:
    return format(int(n), "x").rjust(64, "0")


def _eth_call(rpc: str, to: str, data: str) -> str:
    body = _rpc(rpc, "eth_call", [{"to": to, "data": data}, "latest"])
    if body.get("error"):
        raise RuntimeError(str(body["error"]))
    return body.get("result") or "0x"


def _uni_get_pool(rpc: str, token_a: str, token_b: str, fee: int) -> str | None:
    data = "0x1698ee82" + _enc_addr(token_a) + _enc_addr(token_b) + _enc_uint(fee)
    result = _eth_call(rpc, UNISWAP_V3_FACTORY, data)
    if len(result) < 66:
        return None
    addr = "0x" + result[-40:]
    return None if int(addr, 16) == 0 else addr


def _uni_find_pool(rpc: str, token_a: str, token_b: str) -> tuple[int | None, str | None]:
    for fee in UNISWAP_FEE_TIERS:
        pool = _uni_get_pool(rpc, token_a, token_b, fee)
        if pool:
            return fee, pool
    return None, None


def _uni_quote(rpc: str, token_in: str, token_out: str, fee: int, amount_in: int) -> int:
    data = (
        "0xc6a5026a"
        + _enc_addr(token_in)
        + _enc_addr(token_out)
        + _enc_uint(amount_in)
        + _enc_uint(fee)
        + _enc_uint(0)
    )
    result = _eth_call(rpc, UNISWAP_V3_QUOTER, data)
    return 0 if len(result) < 66 else int(result[2:66], 16)


def _uni_swap_exact_in(
    rpc: str,
    chain_id: int,
    key_hex: str,
    token_in: str,
    token_out: str,
    fee: int,
    amount_in: int,
    amount_out_min: int,
    recipient: str,
) -> tuple[bool, str]:
    data = (
        "0x04e45aaf"
        + _enc_addr(token_in)
        + _enc_addr(token_out)
        + _enc_uint(fee)
        + _enc_addr(recipient)
        + _enc_uint(amount_in)
        + _enc_uint(amount_out_min)
        + _enc_uint(0)
    )
    tx = {"to": UNISWAP_V3_ROUTER, "data": data, "value": 0, "chainId": chain_id, "gas": 300000}
    return _sign_and_send(rpc, chain_id, key_hex, tx)


def _erc20_allowance(rpc: str, token: str, owner: str, spender: str) -> int:
    data = "0xdd62ed3e" + _enc_addr(owner) + _enc_addr(spender)
    result = _eth_call(rpc, token, data)
    return 0 if len(result) < 66 else int(result[2:66], 16)


def _erc20_approve(rpc: str, chain_id: int, key_hex: str, token: str, spender: str, amount: int) -> tuple[bool, str]:
    data = "0x095ea7b3" + _enc_addr(spender) + _enc_uint(amount)
    tx = {"to": token, "data": data, "value": 0, "chainId": chain_id, "gas": 90000}
    return _sign_and_send(rpc, chain_id, key_hex, tx)


def _uniswap_leg(
    rpc: str, chain_id: int, key_hex: str, address: str, token_in: str, token_out: str, amount_in: int
) -> tuple[bool, str, str | None]:
    """One Uniswap V3 exact-input swap: pool discovery, a fresh on-chain
    quote, an approval if needed, then the swap itself. Returns
    (ok, detail, tx_hash_or_None)."""
    fee, pool = _uni_find_pool(rpc, token_in, token_out)
    if not pool:
        return False, f"no Uniswap V3 pool found for {token_in}/{token_out} on any standard fee tier", None
    try:
        quote = _uni_quote(rpc, token_in, token_out, fee, amount_in)
    except Exception as exc:
        return False, f"Uniswap quote failed: {exc}", None
    if quote <= 0:
        return False, "Uniswap quote returned 0 -- pool may be illiquid", None
    min_out = quote * (10000 - SWAP_SLIPPAGE_BPS) // 10000

    allowance = _erc20_allowance(rpc, token_in, address, UNISWAP_V3_ROUTER)
    if allowance < amount_in:
        ok, res = _erc20_approve(rpc, chain_id, key_hex, token_in, UNISWAP_V3_ROUTER, MAX_UINT256)
        if not ok:
            return False, f"approve to Uniswap router failed: {res}", None
        if _wait_receipt(rpc, res) is not True:
            return False, "Uniswap approval reverted, or didn't confirm in time", res

    ok, res = _uni_swap_exact_in(rpc, chain_id, key_hex, token_in, token_out, fee, amount_in, min_out, address)
    if not ok:
        return False, f"Uniswap swap failed: {res}", None
    if _wait_receipt(rpc, res) is not True:
        return False, "Uniswap swap reverted, or didn't confirm in time", res
    return True, "ok", res


def _do_trade(cfg: dict, key_hex: str, address: str, token: str, side: str, amount_in: int, slippage_bps: int = 300):
    """Wrapper that guarantees a (ok, detail, tx_hash) tuple no matter what --
    a bug anywhere in the trade attempt gets caught, logged, and surfaced to
    the caller instead of silently killing the whole MM session task."""
    try:
        return _do_trade_inner(cfg, key_hex, address, token, side, amount_in, slippage_bps)
    except Exception as exc:
        log.exception("_do_trade crashed unexpectedly")
        return False, f"internal error: {type(exc).__name__}: {exc}", None


def _wait_balance(rpc: str, token: str, address: str, min_amount: int = 1, tries: int = 6, delay: float = 2.0) -> int:
    """Polls an ERC20 balance until it's >= min_amount (default: just
    nonzero) or the tries are exhausted, returning whatever it last read.
    Base's public RPC endpoint (mainnet.base.org) is a load-balanced
    gateway -- a confirmation poll can land on one backend node while the
    very next balance read lands on a different one that hasn't caught up
    yet. That's what produced "no BSTONK balance to sell" immediately
    after a CYBERCAB sell that had, in fact, already confirmed. If the
    balance is already there, this returns on the very first check with
    no added delay -- it only costs time when the read actually is stale."""
    bal = _erc20_balance(rpc, token, address)
    for _ in range(tries):
        if bal >= min_amount:
            return bal
        time.sleep(delay)
        bal = _erc20_balance(rpc, token, address)
    return bal


def _reprepare_after_approval(
    key_hex: str, address: str, chain_id: int, token: str, side: str, amount_in: int, slippage_bps: int
):
    """Re-quotes right after sending an approval/pre-approval. BaseStonk's
    own backend can take a moment to see a just-confirmed allowance (the
    same kind of propagation lag as an RPC gateway, just on their side),
    so a single immediate re-quote can still report the trade as
    unapproved even though the allowance is already on-chain -- that's
    exactly what happened buying CYBERCAB right after a confirmed
    approval. Retries a few times with a short backoff, but ONLY when the
    failure is specifically still "approved: false" -- any other failure
    reason is returned immediately, not masked behind a retry loop.
    Returns (prep_dict, None) on success or (None, exc) on failure."""
    last_exc = None
    for attempt in range(3):
        try:
            return api.prepare_trade(key_hex, address, chain_id, token, side, amount_in, slippage_bps), None
        except api.BaseStonkError as exc:
            last_exc = exc
            checks = ((exc.body or {}).get("verdict") or {}).get("checks") or []
            still_unapproved = any(
                isinstance(c, dict) and c.get("id") == "approved" and c.get("ok") is False for c in checks
            )
            if not still_unapproved or attempt == 2:
                return None, exc
            time.sleep(3.0)
    return None, last_exc


def _basestonk_leg(
    cfg: dict, key_hex: str, address: str, token: str, side: str, amount_in: int, slippage_bps: int = 300
) -> tuple[bool, str, str | None]:
    """One BaseStonk prepare -> (pre-approve/approve if needed) -> swap
    round for a single BaseStonk-native token against ITS OWN pair
    currency (WETH, BSTONK, USDC -- whatever). Caller must already have
    >= amount_in of the currency being spent (the pair currency on a buy,
    `token` itself on a sell) sitting in the wallet. Returns
    (ok, detail_str, tx_hash_or_None). This is the same prepare/approve/
    swap dance the original single-hop code used -- just no longer
    assuming the pair is WETH, so it works for any hop in a chain."""
    rpc, chain_id = cfg["rpc"], cfg["chain_id"]

    try:
        prep = api.prepare_trade(key_hex, address, chain_id, token, side, amount_in, slippage_bps)
    except api.BaseStonkError as exc:
        # A 422 (failed verdict) still carries the full body -- including
        # approvalTx -- when the ONLY thing blocking the trade is a missing
        # allowance. BaseStonk's documented loop expects the caller to grab
        # approvalTx from THIS response and send it, then re-prepare.
        body = exc.body or {}
        pre_approval = body.get("approvalTx")
        if pre_approval and pre_approval.get("to") and pre_approval.get("data"):
            ok, res = _sign_and_send(rpc, chain_id, key_hex, pre_approval)
            if not ok:
                return False, f"pre-approval failed: {res}", None
            if _wait_receipt(rpc, res) is not True:
                return False, "pre-approval reverted, or didn't confirm in time", res
            prep, exc2 = _reprepare_after_approval(key_hex, address, chain_id, token, side, amount_in, slippage_bps)
            if prep is None:
                return False, f"re-quote after pre-approval failed: {exc2}", None
        else:
            return False, f"quote refused: {exc}", None

    verdict = prep.get("verdict") or {}
    if verdict and verdict.get("ok") is False:
        return False, f"verdict failed: {verdict.get('checks')}", None

    approval = prep.get("approvalTx")
    if approval and approval.get("to") and approval.get("data"):
        ok, res = _sign_and_send(rpc, chain_id, key_hex, approval)
        if not ok:
            return False, f"approval failed: {res}", None
        if _wait_receipt(rpc, res) is not True:
            return False, "approval reverted, or didn't confirm in time", res
        prep, exc = _reprepare_after_approval(key_hex, address, chain_id, token, side, amount_in, slippage_bps)
        if prep is None:
            return False, f"re-quote after approval failed: {exc}", None

    tx = prep.get("tx") or {}
    if not tx.get("to") or not tx.get("data"):
        return False, "prepare returned no transaction", None

    ok, res = _sign_and_send(rpc, chain_id, key_hex, tx)
    if not ok:
        return False, f"swap send failed: {res}", None
    if _wait_receipt(rpc, res) is not True:
        return False, "swap reverted, or didn't confirm in time", res
    return True, "ok", res


def _fund_and_buy_chain(
    cfg: dict, key_hex: str, address: str, token: str, amount_in_wei: int, slippage_bps: int = 300
) -> tuple[bool, str, str | None]:
    """Buys `token`, funding through however many hops its BaseStonk pair
    chain needs. A direct WETH pair (the common case) is just a wrap + one
    BaseStonk leg -- identical to the original single-hop behavior. A token
    paired against something BaseStonk itself launched chains through that
    too. A token whose chain bottoms out at a currency BaseStonk doesn't
    track (e.g. USDC) gets one Uniswap V3 swap from WETH to fund that leg,
    then the remaining BaseStonk-native hops as usual.

    Resumable: if an earlier attempt got partway through this chain and
    then failed on a later leg (a rate limit, a revert, whatever), the
    funds it already moved are sitting in an intermediate currency, not
    lost -- retrying from scratch would wrap and swap AGAIN on top of
    that, stranding more funds in the same place. So before doing
    anything, this checks the wallet's actual balance at every point in
    the chain and resumes from the furthest currency it already holds,
    instead of redoing legs that already succeeded."""
    rpc, chain_id = cfg["rpc"], cfg["chain_id"]
    weth = (cfg.get("weth") or "").lower()
    token = token.lower()

    chain, needs_uni = _pair_chain(cfg.get("api_chain", ""), token, weth)
    if chain is None:
        return False, "could not resolve this token's funding chain (BaseStonk lookup failed) -- try again shortly", None
    if not chain:
        return False, "token not recognized by BaseStonk", None

    # Full ordered path of currencies from WETH to the target token, e.g.
    # CYBERCAB -> [WETH, USDC, BSTONK, CYBERCAB]; BARNABY -> [WETH, BARNABY].
    if needs_uni:
        sequence = [weth, chain[-1]] + list(reversed(chain[:-1])) + [token]
    else:
        sequence = [weth] + list(reversed(chain[:-1])) + [token]

    # Resume from the furthest currency (besides the target token itself)
    # we already hold a nonzero balance of.
    start_idx = 0
    for i in range(len(sequence) - 1, 0, -1):
        cur = sequence[i]
        if cur == token:
            continue
        if _erc20_balance(rpc, cur, address) > 0:
            start_idx = i
            break

    last_txh = None
    if start_idx == 0:
        have_weth = _erc20_balance(rpc, weth, address)
        if have_weth < amount_in_wei:
            need = amount_in_wei - have_weth
            ok, res = _wrap_native(rpc, chain_id, key_hex, weth, need)
            if not ok:
                return False, f"wrap ETH->WETH failed: {res}", None
            if _wait_receipt(rpc, res) is not True:
                return False, "wrap ETH->WETH reverted, or didn't confirm in time", res
        # Only this first leg is capped to the requested per-round budget --
        # every hop after that spends whatever the previous hop actually
        # landed us in (there's no independent USD budget for an
        # intermediate currency; we just move the whole amount forward).
        # Polls rather than a single read -- this follows a wrap that just
        # confirmed, and a single immediate read can still be stale (see
        # _wait_balance).
        spend = min(amount_in_wei, _wait_balance(rpc, weth, address, min_amount=amount_in_wei))
    else:
        spend = None  # resumed partway through -- always spend the full balance held

    cur_currency = sequence[start_idx]
    for i in range(start_idx + 1, len(sequence)):
        next_currency = sequence[i]
        # Following a leg that just confirmed, a single immediate balance
        # read can still be stale -- poll briefly rather than conclude
        # "no balance" from one read.
        this_spend = spend if (i == start_idx + 1 and spend is not None) else _wait_balance(rpc, cur_currency, address)
        if this_spend <= 0:
            return False, f"no {cur_currency} balance to fund acquiring {next_currency}", last_txh

        is_uniswap_leg = needs_uni and cur_currency == weth and next_currency == chain[-1]
        if is_uniswap_leg:
            ok, detail, txh = _uniswap_leg(rpc, chain_id, key_hex, address, cur_currency, next_currency, this_spend)
            if not ok:
                return False, f"funding leg ({cur_currency}->{next_currency}): {detail}", txh
        else:
            ok, detail, txh = _basestonk_leg(cfg, key_hex, address, next_currency, "buy", this_spend, slippage_bps)
            if not ok:
                return False, f"funding leg (buy {next_currency}): {detail}", txh
        last_txh = txh
        cur_currency = next_currency
        spend = None

    return True, "ok", last_txh


def _sell_chain(cfg: dict, key_hex: str, address: str, token: str, slippage_bps: int = 300) -> tuple[bool, str, str | None]:
    """Sells the wallet's full balance of `token`, walking back down the
    same pair chain (and back through Uniswap for the one leg BaseStonk
    doesn't track, if any), then unwraps whatever WETH lands back to
    native ETH so the session's balance bookkeeping stays accurate."""
    rpc, chain_id = cfg["rpc"], cfg["chain_id"]
    weth = (cfg.get("weth") or "").lower()
    token = token.lower()

    chain, needs_uni = _pair_chain(cfg.get("api_chain", ""), token, weth)
    if chain is None:
        return False, "could not resolve this token's funding chain (BaseStonk lookup failed) -- try again shortly", None
    if not chain:
        return False, "token not recognized by BaseStonk", None

    last_txh = None
    # Poll rather than a single read at every step -- each hop after the
    # first is checked right after the previous leg just confirmed, and a
    # single immediate read can still be stale (see _wait_balance).
    basestonk_hops = [token] + chain[:-1]  # e.g. [CYBERCAB, BSTONK]; single-hop case -> [token] only
    for hop_token in basestonk_hops:
        bal = _wait_balance(rpc, hop_token, address)
        if bal <= 0:
            return False, f"no {hop_token} balance to sell", last_txh
        ok, detail, txh = _basestonk_leg(cfg, key_hex, address, hop_token, "sell", bal, slippage_bps)
        if not ok:
            return False, f"sell leg ({hop_token}): {detail}", txh
        last_txh = txh

    if needs_uni:
        terminal = chain[-1]
        bal = _wait_balance(rpc, terminal, address)
        if bal > 0:
            ok, detail, txh = _uniswap_leg(rpc, chain_id, key_hex, address, terminal, weth, bal)
            if not ok:
                return False, f"sell leg ({terminal}->WETH): {detail}", txh
            last_txh = txh

    # proceeds landed as WETH -- unwrap back to native ETH. Best-effort: a
    # failed unwrap doesn't undo a sell that already confirmed, it just
    # leaves the proceeds sitting as WETH.
    got = _wait_balance(rpc, weth, address)
    if got > 0:
        uok, ures = _unwrap_native(rpc, chain_id, key_hex, weth, got)
        if not uok:
            log.warning("unwrap WETH->ETH failed after sell (funds are safe, just sitting as WETH): %s", ures)

    return True, "ok", last_txh


def _do_trade_inner(cfg: dict, key_hex: str, address: str, token: str, side: str, amount_in: int, slippage_bps: int = 300):
    """Dispatches to the chain-aware buy/sell helpers above. `amount_in` is
    only meaningful for a buy (the WETH-equivalent budget for this round);
    a sell always sells the wallet's full current balance of `token`,
    computed fresh here rather than trusting a possibly-stale caller value."""
    if side == "buy":
        return _fund_and_buy_chain(cfg, key_hex, address, token, amount_in, slippage_bps)
    if side == "sell":
        return _sell_chain(cfg, key_hex, address, token, slippage_bps)
    return False, f"unknown side: {side}", None


# ---- the loop ----------------------------------------------------------


async def _run(user_id: int, chain: str, token: str, trade_usd: float, budget_usd: float, minutes: int, notify):
    """Wrapper so a bug anywhere in the session (wallet lookup, balance
    check, DB write, the trade loop itself) gets logged and DMed instead
    of the whole background task silently vanishing."""
    try:
        await _run_inner(user_id, chain, token, trade_usd, budget_usd, minutes, notify)
    except Exception as exc:
        log.exception("MM session for user %s crashed", user_id)
        try:
            await notify(f"MM session hit an unexpected error and stopped: {type(exc).__name__}: {exc}")
        except Exception:
            pass
    finally:
        _active.pop(user_id, None)


async def _run_inner(user_id: int, chain: str, token: str, trade_usd: float, budget_usd: float, minutes: int, notify):
    cfg = CHAINS[chain]
    rpc, chain_id = cfg["rpc"], cfg["chain_id"]
    wallet = get_linked_evm_key(user_id)
    if not wallet:
        await notify("No linked wallet found. Open Ferzan Trade Bot, send /start once (it generates your wallet), then try /mm again.")
        return
    address, key_hex = wallet

    px = _native_usd(cfg["native_cg"]) or 3000.0
    bal_wei = await asyncio.to_thread(_native_balance_wei, rpc, address)
    bal_usd = (bal_wei / 1e18) * px
    if bal_usd < trade_usd * 2 + 2:
        await notify(
            f"Wallet {address} only has ~${bal_usd:.2f} on {chain}. "
            f"Fund it with at least ${trade_usd * 2 + 2:.2f} of native gas token before running MM."
        )
        return

    with _mmdb() as c:
        cur = c.execute(
            "INSERT INTO mm_sessions (user_id, chain, token, started_at, budget_usd) VALUES (?,?,?,?,?)",
            (user_id, chain, token, time.time(), budget_usd),
        )
        session_id = cur.lastrowid

    deadline = time.time() + minutes * 60
    spent = 0.0
    trades = 0
    fails = 0
    entry = _active[user_id]

    await notify(f"MM started on {chain}: {token[:10]}… · ${trade_usd:.2f}/round · budget ${budget_usd:.2f} · {minutes}m")

    while not entry.get("stop") and time.time() < deadline and spent < budget_usd:
        wei_in = int((trade_usd / max(px, 1e-9)) * 1e18)

        ok, detail, txh = await asyncio.to_thread(_do_trade, cfg, key_hex, address, token, "buy", wei_in)
        with _mmdb() as c:
            c.execute(
                "INSERT INTO mm_trades (session_id, side, ts, usd, tx_hash, ok, note) VALUES (?,?,?,?,?,?,?)",
                (session_id, "buy", time.time(), trade_usd, txh, int(ok), detail),
            )
        if not ok:
            fails += 1
            log.warning("mm buy failed user=%s: %s", user_id, detail)
            if fails >= MAX_CONSECUTIVE_FAILS:
                await notify(f"Stopping: {fails} failed trades in a row. Last error: {detail}")
                break
            # BaseStonk's own API rate-limits us if we hit it too fast --
            # a multi-hop chain issues several /trade/prepare calls per
            # round, so this is more likely to come up than it was for a
            # direct WETH pair. Back off longer specifically for that case
            # instead of hammering it again in 10s.
            backoff = 45 if "rate limited" in str(detail) else 10
            await asyncio.sleep(backoff)
            continue
        fails = 0
        spent += trade_usd
        trades += 1

        await asyncio.sleep(random.uniform(DWELL_MIN_S, DWELL_MAX_S))
        if entry.get("stop"):
            break

        tok_bal = await asyncio.to_thread(_erc20_balance, rpc, token, address)
        if tok_bal <= 0:
            await asyncio.sleep(5)
            continue
        ok, detail, txh = await asyncio.to_thread(_do_trade, cfg, key_hex, address, token, "sell", tok_bal)
        with _mmdb() as c:
            c.execute(
                "INSERT INTO mm_trades (session_id, side, ts, usd, tx_hash, ok, note) VALUES (?,?,?,?,?,?,?)",
                (session_id, "sell", time.time(), None, txh, int(ok), detail),
            )
        if not ok:
            fails += 1
            log.warning("mm sell failed user=%s: %s", user_id, detail)
            if fails >= MAX_CONSECUTIVE_FAILS:
                await notify(f"Stopping: sells failing ({detail}). Your tokens are still in your wallet -- sell manually if needed.")
                break
        else:
            fails = 0
            trades += 1

        with _mmdb() as c:
            c.execute(
                "UPDATE mm_sessions SET spent_usd=?, trades=? WHERE id=?", (spent, trades, session_id)
            )

        await asyncio.sleep(random.uniform(DWELL_MIN_S, DWELL_MAX_S))

    with _mmdb() as c:
        c.execute(
            "UPDATE mm_sessions SET ended_at=?, spent_usd=?, trades=?, status=? WHERE id=?",
            (time.time(), spent, trades, "stopped" if entry.get("stop") else "finished", session_id),
        )

    await notify(f"MM ended on {chain}: {trades} legs, ~${spent:.2f} of volume routed. /mm again to run another round.")
    _active.pop(user_id, None)


def start(user_id: int, chain: str, token: str, trade_usd: float, budget_usd: float, minutes: int, notify) -> str | None:
    """Returns an error string if it couldn't start, else None and a task is running."""
    if user_id in _active:
        return "You already have an MM session running. /mmstop first."
    chain = CHAIN_ALIASES.get(chain.lower())
    if not chain:
        return "Chain must be base or robinhood."
    if not (token.startswith("0x") and len(token) == 42):
        return "Need a 0x contract address (BaseStonk is EVM-only right now)."
    trade_usd = max(MIN_TRADE_USD, min(MAX_TRADE_USD, trade_usd))
    budget_usd = max(MIN_BUDGET_USD, min(MAX_BUDGET_USD, budget_usd))
    minutes = max(5, min(MAX_MINUTES, minutes))

    entry = {"stop": False}
    _active[user_id] = entry
    task = asyncio.create_task(_run(user_id, chain, token, trade_usd, budget_usd, minutes, notify))
    entry["task"] = task
    return None


def stop(user_id: int) -> bool:
    entry = _active.get(user_id)
    if not entry:
        return False
    entry["stop"] = True
    return True


def status(user_id: int) -> dict | None:
    with _mmdb() as c:
        row = c.execute(
            "SELECT * FROM mm_sessions WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
    if not row:
        return None
    return dict(row)
