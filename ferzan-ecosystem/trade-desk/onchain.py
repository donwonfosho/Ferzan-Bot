"""Wallet activity + light portfolio marks.

EVM: Etherscan API V2 unified endpoint (one free key, 60+ chains).
Solana: Helius enhanced history if HELIUS_API_KEY is set, otherwise
public RPC getSignaturesForAddress (no key, coarse).

Signup (free, no card):
  Etherscan  https://etherscan.io/apis
  Helius     https://dashboard.helius.dev
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import requests

from chains import CHAINS, explorer_tx, resolve_chain

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"
SOLANA_RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
HELIUS_TX = "https://api-mainnet.helius-rpc.com/v0/addresses/{address}/transactions"
HOOD_EXPLORER = "https://robinhoodchain.blockscout.com/api"

CHAIN_IDS = {cid: meta["chain_id"] for cid, meta in CHAINS.items() if meta.get("chain_id")}
NATIVE = {meta["chain_id"]: meta["native"] for meta in CHAINS.values() if meta.get("chain_id")}


class OnchainError(Exception):
    pass


def etherscan_key() -> str:
    return os.getenv("ETHERSCAN_API_KEY", "").strip()


def helius_key() -> str:
    return os.getenv("HELIUS_API_KEY", "").strip()


def normalize_chain(raw: str) -> str:
    cid = resolve_chain(raw)
    if not cid:
        raise OnchainError("Unknown chain. Use eth, bsc, base, sol, hood (Robinhood Chain).")
    return cid


def looks_evm(addr: str) -> bool:
    return addr.startswith("0x") and len(addr) == 42


def looks_sol(addr: str) -> bool:
    return 32 <= len(addr) <= 44 and not addr.startswith("0x")


@dataclass
class WalletEvent:
    chain: str
    address: str
    txid: str
    when: int
    summary: str
    direction: str
    value_hint: str


def _es(params: dict[str, Any]) -> Any:
    key = etherscan_key()
    if not key:
        raise OnchainError(
            "No ETHERSCAN_API_KEY. Free key: https://etherscan.io/apis"
        )
    q = {"apikey": key, **params}
    r = requests.get(ETHERSCAN_V2, params=q, timeout=15)
    r.raise_for_status()
    data = r.json()
    if str(data.get("status")) == "0" and data.get("message") not in {"No transactions found", "OK"}:
        raise OnchainError(str(data.get("result") or data.get("message")))
    return data.get("result") or []


def evm_native_balance(chain: str, address: str) -> tuple[float, str]:
    chain_id = CHAIN_IDS[chain]
    result = _es(
        {
            "chainid": chain_id,
            "module": "account",
            "action": "balance",
            "address": address,
            "tag": "latest",
        }
    )
    wei = int(result or 0)
    return wei / 1e18, NATIVE.get(chain_id, "ETH")


def evm_recent(chain: str, address: str, limit: int = 8) -> list[WalletEvent]:
    chain_id = CHAIN_IDS[chain]
    rows = _es(
        {
            "chainid": chain_id,
            "module": "account",
            "action": "txlist",
            "address": address,
            "page": 1,
            "offset": limit,
            "sort": "desc",
        }
    )
    events: list[WalletEvent] = []
    if not isinstance(rows, list):
        return events
    addr = address.lower()
    for row in rows:
        to = (row.get("to") or "").lower()
        frm = (row.get("from") or "").lower()
        direction = "IN" if to == addr else "OUT"
        val = int(row.get("value") or 0) / 1e18
        sym = NATIVE.get(chain_id, "ETH")
        ts = int(row.get("timeStamp") or 0)
        txid = row.get("hash") or ""
        events.append(
            WalletEvent(
                chain=chain,
                address=address,
                txid=txid,
                when=ts,
                summary=f"{direction} {val:.6g} {sym}",
                direction=direction,
                value_hint=f"{val:.6g} {sym}",
            )
        )
    tokens = _es(
        {
            "chainid": chain_id,
            "module": "account",
            "action": "tokentx",
            "address": address,
            "page": 1,
            "offset": limit,
            "sort": "desc",
        }
    )
    if isinstance(tokens, list):
        for row in tokens[:limit]:
            to = (row.get("to") or "").lower()
            direction = "IN" if to == addr else "OUT"
            decimals = int(row.get("tokenDecimal") or 18)
            raw = int(row.get("value") or 0)
            amt = raw / (10 ** decimals) if decimals <= 36 else 0
            sym = row.get("tokenSymbol") or "TOKEN"
            events.append(
                WalletEvent(
                    chain=chain,
                    address=address,
                    txid=row.get("hash") or "",
                    when=int(row.get("timeStamp") or 0),
                    summary=f"{direction} {amt:.6g} {sym}",
                    direction=direction,
                    value_hint=f"{amt:.6g} {sym}",
                )
            )
    events.sort(key=lambda e: e.when, reverse=True)
    return events[:limit]


def sol_recent_public(address: str, limit: int = 8) -> list[WalletEvent]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": limit}],
    }
    r = requests.post(SOLANA_RPC, json=payload, timeout=15)
    r.raise_for_status()
    rows = (r.json() or {}).get("result") or []
    events: list[WalletEvent] = []
    for row in rows:
        sig = row.get("signature") or ""
        ts = int(row.get("blockTime") or 0)
        err = row.get("err")
        status = "fail" if err else "ok"
        events.append(
            WalletEvent(
                chain="sol",
                address=address,
                txid=sig,
                when=ts,
                summary=f"SOL tx {status} {sig[:8]}…",
                direction="?",
                value_hint=status,
            )
        )
    return events


def sol_recent_helius(address: str, limit: int = 8) -> list[WalletEvent]:
    key = helius_key()
    if not key:
        return sol_recent_public(address, limit)
    url = HELIUS_TX.format(address=address)
    r = requests.get(url, params={"api-key": key, "limit": limit}, timeout=15)
    if r.status_code >= 400:
        return sol_recent_public(address, limit)
    rows = r.json()
    if not isinstance(rows, list):
        return sol_recent_public(address, limit)
    events: list[WalletEvent] = []
    for row in rows[:limit]:
        txid = row.get("signature") or ""
        ts = int(row.get("timestamp") or 0)
        ttype = row.get("type") or row.get("transactionType") or "TX"
        source = row.get("source") or ""
        events.append(
            WalletEvent(
                chain="sol",
                address=address,
                txid=txid,
                when=ts,
                summary=f"{ttype} {source}".strip(),
                direction=str(ttype),
                value_hint=source,
            )
        )
    return events


def hood_recent(address: str, limit: int = 8) -> list[WalletEvent]:
    try:
        r = requests.get(
            HOOD_EXPLORER,
            params={
                "module": "account",
                "action": "txlist",
                "address": address,
                "page": 1,
                "offset": limit,
                "sort": "desc",
            },
            timeout=15,
        )
        r.raise_for_status()
        rows = (r.json() or {}).get("result") or []
    except requests.RequestException as exc:
        raise OnchainError(f"Robinhood explorer error: {exc}") from exc
    if isinstance(rows, str):
        raise OnchainError(rows)
    events: list[WalletEvent] = []
    addr = address.lower()
    for row in rows[:limit]:
        to = (row.get("to") or "").lower()
        direction = "IN" if to == addr else "OUT"
        val = int(row.get("value") or 0) / 1e18
        events.append(
            WalletEvent(
                chain="hood",
                address=address,
                txid=row.get("hash") or "",
                when=int(row.get("timeStamp") or 0),
                summary=f"{direction} {val:.6g} ETH",
                direction=direction,
                value_hint=f"{val:.6g} ETH",
            )
        )
    return events


def recent_activity(chain: str, address: str, limit: int = 8) -> list[WalletEvent]:
    chain = normalize_chain(chain)
    if chain == "sol":
        if not looks_sol(address):
            raise OnchainError("That does not look like a Solana address.")
        return sol_recent_helius(address, limit)
    if not looks_evm(address):
        raise OnchainError("That does not look like an EVM address (0x + 40 hex).")
    if chain == "hood":
        return hood_recent(address, limit)
    return evm_recent(chain, address, limit)


def native_mark_usd(chain: str, address: str) -> float | None:
    """Best-effort native-asset mark. Token book needs a paid portfolio API."""
    from price_fetcher import quote_price

    chain = normalize_chain(chain)
    if chain == "sol":
        return None
    try:
        qty, sym = evm_native_balance(chain, address)
        px = quote_price(sym if sym != "MATIC" else "POL")
        return qty * px
    except Exception:
        return None


def status_line() -> str:
    bits = []
    bits.append("Etherscan " + ("ready" if etherscan_key() else "missing key"))
    bits.append("Helius " + ("ready" if helius_key() else "public RPC fallback"))
    return " · ".join(bits)
