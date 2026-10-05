"""Venues Ferzan can score and quote.

Green on Maestro's list. Data via DexScreener. Live swap still means
the user signs (Jupiter on Sol, 0x/Trust on EVM). TRX/TON/Arc/Stable
are scored when DexScreener indexes the CA; they are not first-class
0x routes.
"""

from __future__ import annotations


def _c(
    cid: str,
    label: str,
    kind: str,
    ds: str,
    gecko: str,
    native: str,
    *,
    chain_id: int | None = None,
    rpc: str = "",
    explorer: str = "",
    router: str = "",
    notes: str = "",
) -> dict:
    tx = f"{explorer}/tx/{{txid}}" if explorer else "{txid}"
    addr = f"{explorer}/address/{{addr}}" if explorer else "{addr}"
    if kind == "sol":
        addr = f"{explorer}/account/{{addr}}" if explorer else "{addr}"
    elif kind == "ton":
        tx = f"{explorer}/transaction/{{txid}}" if explorer else "{txid}"
        addr = f"{explorer}/{{addr}}" if explorer else "{addr}"
    return {
        "id": cid,
        "label": label,
        "kind": kind,
        "chain_id": chain_id,
        "dexscreener": ds,
        "gecko": gecko,
        "native": native,
        "rpc": rpc,
        "explorer_tx": tx,
        "explorer_addr": addr,
        "router": router,
        "notes": notes,
    }


CHAINS = {
    "sol": _c("sol", "Solana", "sol", "solana", "solana", "SOL",
              rpc="https://api.mainnet-beta.solana.com",
              explorer="https://solscan.io", router="Jupiter"),
    "bsc": _c("bsc", "BNB Chain", "evm", "bsc", "bsc", "BNB",
              chain_id=56, rpc="https://bsc-dataseed.binance.org",
              explorer="https://bscscan.com", router="0x / PancakeSwap"),
    "base": _c("base", "Base", "evm", "base", "base", "ETH",
               chain_id=8453, rpc="https://mainnet.base.org",
               explorer="https://basescan.org", router="0x / Uniswap"),
    "eth": _c("eth", "Ethereum", "evm", "ethereum", "eth", "ETH",
              chain_id=1, rpc="https://ethereum.publicnode.com",
              explorer="https://etherscan.io", router="0x / Uniswap"),
    "monad": _c("monad", "Monad", "evm", "monad", "monad", "MON",
                chain_id=143, rpc="https://rpc.monad.xyz",
                explorer="https://monadvision.com", router="0x"),
    "sonic": _c("sonic", "Sonic", "evm", "sonic", "sonic", "S",
                chain_id=146, rpc="https://rpc.soniclabs.com",
                explorer="https://sonicscan.org", router="0x"),
    "avax": _c("avax", "Avalanche", "evm", "avalanche", "avax", "AVAX",
               chain_id=43114, rpc="https://api.avax.network/ext/bc/C/rpc",
               explorer="https://snowtrace.io", router="0x / LFJ"),
    "arb": _c("arb", "Arbitrum", "evm", "arbitrum", "arbitrum", "ETH",
              chain_id=42161, rpc="https://arb1.arbitrum.io/rpc",
              explorer="https://arbiscan.io", router="0x / Uniswap"),
    "hype": _c("hype", "HyperEVM", "evm", "hyperevm", "hyperevm", "HYPE",
               chain_id=999, rpc="https://rpc.hyperliquid.xyz/evm",
               explorer="https://purrsec.com", router="HyperEVM DEX / 0x if listed"),
    "hood": _c("hood", "Robinhood Chain", "evm", "robinhood", "robinhood", "ETH",
               chain_id=4663, rpc="https://rpc.mainnet.chain.robinhood.com",
               explorer="https://explorer.robinhood.com",
               router="0x / Uniswap",
               notes="EVM L2. Not the stock app."),
    "arc": _c("arc", "Arc", "evm", "arc", "arc", "ETH",
              explorer="https://explorer.arc.network",
              router="data only until 0x lists it",
              notes="Score if DexScreener has /arc pairs"),
    "stable": _c("stable", "Stable", "evm", "stable", "stable", "ETH",
                 router="data only",
                 notes="Maestro label. Score when DexScreener indexes the CA"),
    "trx": _c("trx", "Tron", "tron", "tron", "tron", "TRX",
              rpc="https://api.trongrid.io",
              explorer="https://tronscan.org/#",
              router="SunSwap V2",
              notes="Live via TronGrid. Same key as EVM."),
    "ton": _c("ton", "TON", "ton", "ton", "ton", "TON",
              explorer="https://tonviewer.com",
              router="STON.fi",
              notes="Buy + sell live via STON.fi v1 (WalletV4R2)."),
    "pol": _c("pol", "Polygon", "evm", "polygon", "polygon_pos", "POL",
              chain_id=137, rpc="https://polygon-bor-rpc.publicnode.com",
              explorer="https://polygonscan.com", router="0x / QuickSwap"),
    "pulse": _c("pulse", "PulseChain", "evm", "pulsechain", "pulsechain", "PLS",
                chain_id=369, rpc="https://rpc.pulsechain.com",
                explorer="https://scan.pulsechain.com",
                router="PulseX",
                notes="Not on 0x. Signals live. Swap next."),
    "ink": _c("ink", "Ink", "evm", "ink", "ink", "ETH",
              chain_id=57073, rpc="https://rpc-gel.inkonchain.com",
              explorer="https://explorer.inkonchain.com", router="0x"),
    "op": _c("op", "Optimism", "evm", "optimism", "optimism", "ETH",
             chain_id=10, rpc="https://mainnet.optimism.io",
             explorer="https://optimistic.etherscan.io", router="0x / Uniswap"),
    "linea": _c("linea", "Linea", "evm", "linea", "linea", "ETH",
                chain_id=59144, rpc="https://rpc.linea.build",
                explorer="https://lineascan.build", router="0x"),
}

# 0x Swap API as of 2026. Pulse / TON / TRON / Arc / Stable are not on that list.
def _rpc_overrides() -> None:
    """RPC_<KEY>_URL in the env replaces a chain's RPC (e.g. RPC_POL_URL), so a dead public RPC is a .env edit."""
    import os

    for k, m in CHAINS.items():
        u = (os.getenv(f"RPC_{k.upper()}_URL") or "").strip()
        if u.startswith("http"):
            m["rpc"] = u


_rpc_overrides()

ZEROX_LIVE = {
    "eth", "bsc", "base", "arb", "avax", "pol", "op", "linea",
    "sonic", "hype", "hood", "ink", "monad",
}

ALIASES = {
    "ethereum": "eth", "ether": "eth",
    "bnb": "bsc", "binance": "bsc",
    "solana": "sol",
    "avalanche": "avax",
    "arbitrum": "arb",
    "hyperliquid": "hype", "hyperevm": "hype", "hyper": "hype",
    "robinhood": "hood", "rh": "hood", "rhc": "hood", "robinhoodchain": "hood",
    "tron": "trx",
    "toncoin": "ton",
    "mon": "monad",
    "polygon": "pol", "matic": "pol", "poly": "pol",
    "pulsechain": "pulse", "pls": "pulse",
    "optimism": "op", "opmainnet": "op",
}

# Same order as Maestro's chain list in the screenshot.

def _arc_setup() -> None:
    """Arc needs an RPC and a chain id before any transaction can be signed.
    ARC_RPC_URL / ARC_CHAIN_ID override; otherwise the id is asked from the RPC
    once (4 s cap). If it can't be learned Arc stays unset (trades refuse
    cleanly) rather than signing for a wrong chain."""
    import os

    meta = CHAINS.get("arc")
    if not meta:
        return
    rpc = (os.getenv("ARC_RPC_URL") or "https://rpc.mainnet.arc.io").strip()
    meta["rpc"] = rpc
    raw = (os.getenv("ARC_CHAIN_ID") or "").strip()
    try:
        if raw:
            meta["chain_id"] = int(raw, 0)
        elif not meta.get("chain_id"):
            import requests

            r = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=4)
            meta["chain_id"] = int(str((r.json() or {}).get("result") or "0"), 16) or None
    except Exception:
        meta["chain_id"] = meta.get("chain_id")


_arc_setup()

ACTIVE = (
    "sol", "bsc", "base", "eth", "monad", "sonic", "avax",
    "arb", "hype", "hood", "pol", "pulse", "ink", "op", "linea",
    "arc", "stable", "trx", "ton",
)


def resolve_chain(raw: str | None) -> str | None:
    if not raw:
        return None
    key = raw.strip().lower().replace(" ", "").replace("-", "")
    if key in CHAINS:
        return key
    return ALIASES.get(key)


def label_for_mint(mint: str, detected: str | None = None) -> str:
    """The chain a saved DCA plan / buy limit belongs to. Non-EVM addresses say it themselves; a 0x address
    uses the chain the token lookup found (Base only if the lookup could not tell)."""
    mint = (mint or "").strip()
    if mint.startswith(("EQ", "UQ", "kQ")):
        return "ton"
    if mint.startswith("T") and 30 <= len(mint) <= 36:
        return "trx"
    if not mint.startswith("0x"):
        return "sol"
    return resolve_chain(detected) or "base"


def meta(chain: str) -> dict:
    cid = resolve_chain(chain)
    if not cid:
        raise KeyError(chain)
    return CHAINS[cid]


def chain_list() -> str:
    return ", ".join(CHAINS[c]["label"] for c in ACTIVE)


def explorer_tx(chain: str, txid: str) -> str:
    return meta(chain)["explorer_tx"].format(txid=txid)


def explorer_addr(chain: str, addr: str) -> str:
    return meta(chain)["explorer_addr"].format(addr=addr)
