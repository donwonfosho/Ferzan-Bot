"""Supported venues. Same command surface on every chain."""

from __future__ import annotations

CHAINS = {
    "eth": {
        "id": "eth",
        "dexscreener": "ethereum",
        "gecko": "eth",
        "kind": "evm",
        "label": "Ethereum",
    },
    "base": {
        "id": "base",
        "dexscreener": "base",
        "gecko": "base",
        "kind": "evm",
        "label": "Base",
    },
    "bsc": {
        "id": "bsc",
        "dexscreener": "bsc",
        "gecko": "bsc",
        "kind": "evm",
        "label": "BNB Chain",
    },
    "arb": {
        "id": "arb",
        "dexscreener": "arbitrum",
        "gecko": "arbitrum",
        "kind": "evm",
        "label": "Arbitrum",
    },
    "op": {
        "id": "op",
        "dexscreener": "optimism",
        "gecko": "optimism",
        "kind": "evm",
        "label": "Optimism",
    },
    "polygon": {
        "id": "polygon",
        "dexscreener": "polygon",
        "gecko": "polygon_pos",
        "kind": "evm",
        "label": "Polygon",
    },
    "avax": {
        "id": "avax",
        "dexscreener": "avalanche",
        "gecko": "avax",
        "kind": "evm",
        "label": "Avalanche",
    },
    "sol": {
        "id": "sol",
        "dexscreener": "solana",
        "gecko": "solana",
        "kind": "sol",
        "label": "Solana",
    },
}

ALIASES = {
    "ethereum": "eth",
    "ether": "eth",
    "bnb": "bsc",
    "binance": "bsc",
    "arbitrum": "arb",
    "optimism": "op",
    "matic": "polygon",
    "poly": "polygon",
    "avalanche": "avax",
    "solana": "sol",
}


def resolve_chain(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower()
    if key in CHAINS:
        return key
    return ALIASES.get(key)


def chain_list() -> str:
    return ", ".join(sorted(CHAINS))
