"""The five venues this bot trades.

BNB Chain, Solana, Base, Ethereum, Robinhood Chain.
"""

from __future__ import annotations

CHAINS = {
    "eth": {
        "id": "eth",
        "label": "Ethereum",
        "kind": "evm",
        "chain_id": 1,
        "dexscreener": "ethereum",
        "gecko": "eth",
        "native": "ETH",
        "rpc": "https://ethereum.publicnode.com",
        "explorer_tx": "https://etherscan.io/tx/{txid}",
        "explorer_addr": "https://etherscan.io/address/{addr}",
        "router": "0x / Uniswap",
    },
    "bsc": {
        "id": "bsc",
        "label": "BNB Chain",
        "kind": "evm",
        "chain_id": 56,
        "dexscreener": "bsc",
        "gecko": "bsc",
        "native": "BNB",
        "rpc": "https://bsc-dataseed.binance.org",
        "explorer_tx": "https://bscscan.com/tx/{txid}",
        "explorer_addr": "https://bscscan.com/address/{addr}",
        "router": "0x / PancakeSwap",
    },
    "base": {
        "id": "base",
        "label": "Base",
        "kind": "evm",
        "chain_id": 8453,
        "dexscreener": "base",
        "gecko": "base",
        "native": "ETH",
        "rpc": "https://mainnet.base.org",
        "explorer_tx": "https://basescan.org/tx/{txid}",
        "explorer_addr": "https://basescan.org/address/{addr}",
        "router": "0x / Uniswap",
    },
    "sol": {
        "id": "sol",
        "label": "Solana",
        "kind": "sol",
        "chain_id": None,
        "dexscreener": "solana",
        "gecko": "solana",
        "native": "SOL",
        "rpc": "https://api.mainnet-beta.solana.com",
        "explorer_tx": "https://solscan.io/tx/{txid}",
        "explorer_addr": "https://solscan.io/account/{addr}",
        "router": "Jupiter",
    },
    "hood": {
        "id": "hood",
        "label": "Robinhood Chain",
        "kind": "evm",
        "chain_id": 4663,
        "dexscreener": "robinhood",
        "gecko": "robinhood",
        "native": "ETH",
        "rpc": "https://rpc.mainnet.chain.robinhood.com",
        "explorer_tx": "https://robinhoodchain.blockscout.com/tx/{txid}",
        "explorer_addr": "https://robinhoodchain.blockscout.com/address/{addr}",
        "router": "Uniswap on Hood / 0x if listed",
        "notes": "EVM L2. Gas is ETH. Not the Robinhood stock app.",
    },
}

ALIASES = {
    "ethereum": "eth",
    "ether": "eth",
    "bnb": "bsc",
    "binance": "bsc",
    "base": "base",
    "solana": "sol",
    "sol": "sol",
    "robinhood": "hood",
    "hood": "hood",
    "rh": "hood",
    "rhc": "hood",
    "robinhoodchain": "hood",
}

ACTIVE = ("eth", "bsc", "base", "sol", "hood")


def resolve_chain(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower().replace(" ", "").replace("-", "")
    if key in CHAINS:
        return key
    return ALIASES.get(key)


def meta(chain: str) -> dict:
    cid = resolve_chain(chain)
    if not cid:
        raise KeyError(chain)
    return CHAINS[cid]


def chain_list() -> str:
    return ", ".join(f"{CHAINS[c]['label']} ({c})" for c in ACTIVE)


def explorer_tx(chain: str, txid: str) -> str:
    m = meta(chain)
    return m["explorer_tx"].format(txid=txid)


def explorer_addr(chain: str, addr: str) -> str:
    m = meta(chain)
    return m["explorer_addr"].format(addr=addr)
