"""
evm_launch.py

Builds UNSIGNED transactions for calling LaunchTokenFactory.launchToken()
on any of the four EVM chains. This is the core of the non-custodial
design: this code never holds a private key and never signs anything --
it hands back a plain dict describing the transaction, which your
Telegram Mini App passes to the user's own wallet (MetaMask/WalletConnect)
for them to review and sign. The bot backend only broadcasts the already-
signed raw transaction afterward.

Chain IDs and native gas currencies verified directly (not from training
memory, since getting these wrong causes silent failures or worse --
signing with the wrong chain ID can enable transaction replay across
networks):
  - Ethereum:        chain_id 1,     gas in ETH
  - BNB Chain:        chain_id 56,    gas in BNB
  - Base:             chain_id 8453,  gas in ETH
  - Robinhood Chain:  chain_id 4663,  gas in ETH (Arbitrum Orbit L2)

I have not been able to execute this against any of these networks from
where I'm running (no live network access) -- the web3.py API shape is
stable and well-documented, but test end-to-end against each chain's
testnet before pointing this at mainnet with real funds.
"""

import os
from dataclasses import dataclass
from typing import Optional

from web3 import Web3


@dataclass
class ChainConfig:
    name: str
    chain_id: int
    native_symbol: str
    default_rpc: str
    explorer: str
    native_decimals: int = 18


CHAIN_CONFIGS = {
    "ethereum": ChainConfig(
        name="Ethereum", chain_id=1, native_symbol="ETH",
        default_rpc="",  # use your own Infura/Alchemy endpoint -- no reliable free public RPC for mainnet
        explorer="https://etherscan.io",
    ),
    "bsc": ChainConfig(
        name="BNB Chain", chain_id=56, native_symbol="BNB",
        default_rpc="https://bsc-dataseed.binance.org",
        explorer="https://bscscan.com",
    ),
    "base": ChainConfig(
        name="Base", chain_id=8453, native_symbol="ETH",
        default_rpc="",  # use your own Infura/Alchemy/Base RPC endpoint
        explorer="https://basescan.org",
    ),
    "robinhood": ChainConfig(
        name="Robinhood Chain", chain_id=4663, native_symbol="ETH",
        default_rpc="https://rpc.mainnet.chain.robinhood.com",
        explorer="https://robinhoodchain.blockscout.com",
    ),
    "arc": ChainConfig(
        name="Arc", chain_id=5042, native_symbol="USDC",
        default_rpc="https://rpc.mainnet.arc.io",
        explorer="https://explorer.arc.io",
        native_decimals=6,  # Arc gas is USDC with 6 decimals — not 18
    ),
}


def launch_fee_units(chain_key: str) -> int:
    """Native-unit launch fee. Arc is 6-dec USDC; everyone else is 18-dec wei."""
    if chain_key == "arc":
        return int(os.environ.get("LAUNCH_FEE_ARC") or "0")
    return int(os.environ.get("LAUNCH_FEE_WEI") or "0")

# Minimal ABI covering just the function we call. After you compile
# LaunchTokenFactory.sol (Hardhat/Foundry), replace this with the real
# generated ABI -- this hand-written version matches the Solidity
# signature exactly, but the compiler's output is the source of truth.
FACTORY_ABI = [
    {
        "inputs": [
            {"name": "name_", "type": "string"},
            {"name": "symbol_", "type": "string"},
            {"name": "totalSupply_", "type": "uint256"},
            {"name": "projectUrl_", "type": "string"},
        ],
        "name": "launchToken",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "name_", "type": "string"},
            {"name": "symbol_", "type": "string"},
            {"name": "totalSupply_", "type": "uint256"},
            {"name": "projectUrl_", "type": "string"},
            {"name": "wallets", "type": "address[]"},
            {"name": "bps", "type": "uint256[]"},
        ],
        "name": "launchTokenWithAlloc",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "payable",
        "type": "function",
    },
]

# ABI fragment for BondingCurveFactory.launch() -- the bonding-curve
# revenue path, as opposed to a plain LaunchTokenFactory deployment.
BONDING_CURVE_FACTORY_ABI = [
    {
        "inputs": [
            {"name": "name_", "type": "string"},
            {"name": "symbol_", "type": "string"},
            {"name": "totalSupply_", "type": "uint256"},
            {"name": "graduationEthThreshold_", "type": "uint256"},
            {"name": "virtualEthReserve_", "type": "uint256"},
            {"name": "virtualTokenReserve_", "type": "uint256"},
        ],
        "name": "launch",
        "outputs": [
            {"name": "curveAddress", "type": "address"},
            {"name": "tokenAddress", "type": "address"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "name_", "type": "string"},
            {"name": "symbol_", "type": "string"},
            {"name": "totalSupply_", "type": "uint256"},
            {"name": "graduationEthThreshold_", "type": "uint256"},
            {"name": "virtualEthReserve_", "type": "uint256"},
            {"name": "virtualTokenReserve_", "type": "uint256"},
            {"name": "startTime_", "type": "uint256"},
            {"name": "maxBuyPerWallet_", "type": "uint256"},
            {"name": "allocWallets", "type": "address[]"},
            {"name": "allocBps", "type": "uint256[]"},
        ],
        "name": "launchFull",
        "outputs": [
            {"name": "curveAddress", "type": "address"},
            {"name": "tokenAddress", "type": "address"},
        ],
        "stateMutability": "payable",
        "type": "function",
    },
]


def parse_allocs(raw: str) -> tuple[list[str], list[int]]:
    wallets, bps = [], []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        addr, pct = part.split(":", 1)
        addr = addr.strip()
        if not addr.startswith("0x") or len(addr) != 42:
            continue
        try:
            n = int(pct.strip())
        except ValueError:
            continue
        if 0 < n <= 2000:
            wallets.append(Web3.to_checksum_address(addr))
            bps.append(n)
    return wallets, bps


def parse_native_amount(raw: str, decimals: int = 18) -> int:
    text = str(raw or "0").strip().replace(",", "")
    if not text or text.lower() in {"0", "skip", "none"}:
        return 0
    try:
        return int(float(text) * (10 ** decimals))
    except ValueError:
        return 0


class UnsupportedChainError(Exception):
    pass


class EvmLaunchTxBuilder:
    def __init__(self, chain_key: str, factory_address: str, rpc_url: Optional[str] = None):
        if chain_key not in CHAIN_CONFIGS:
            raise UnsupportedChainError(
                f"'{chain_key}' not in supported chains: {list(CHAIN_CONFIGS)}"
            )
        self.chain_key = chain_key
        self.chain = CHAIN_CONFIGS[chain_key]
        rpc = rpc_url or self.chain.default_rpc
        if not rpc:
            raise ValueError(
                f"No RPC URL for {self.chain.name} -- pass one explicitly "
                "(mainnet Ethereum has no reliable free public endpoint)."
            )
        self.w3 = Web3(Web3.HTTPProvider(rpc))
        self.factory = self.w3.eth.contract(
            address=Web3.to_checksum_address(factory_address), abi=FACTORY_ABI
        )

    def build_unsigned_launch_tx(
        self,
        creator_address: str,
        name: str,
        symbol: str,
        total_supply: int,
        decimals: int = 18,
        project_url: str = "",
        alloc_wallets: list | None = None,
        alloc_bps: list | None = None,
    ) -> dict:
        """
        Returns a plain dict ready to hand to a wallet for signing (e.g.
        via WalletConnect's eth_signTransaction / eth_sendTransaction, or
        ethers.js on the Mini App side). total_supply should already be
        in the token's smallest unit (i.e. pre-multiplied by 10**decimals)
        -- decimals is accepted here for clarity/validation, not applied
        automatically, since silently scaling a supply number is exactly
        the kind of thing that should be explicit and visible to whoever
        is reviewing this before they sign it.
        """
        creator = Web3.to_checksum_address(creator_address)

        try:
            nonce = self.w3.eth.get_transaction_count(creator, "pending")
        except Exception as e:
            raise ConnectionError(
                f"Could not reach {self.chain.name} RPC to fetch nonce: {e}"
            ) from e

        wallets = list(alloc_wallets or [])
        bps = list(alloc_bps or [])
        use_alloc = bool(wallets) and len(wallets) == len(bps)
        try:
            fee_wei = launch_fee_units(self.chain_key)
            if use_alloc:
                gas_estimate = self.factory.functions.launchTokenWithAlloc(
                    name, symbol, total_supply, project_url, wallets, bps
                ).estimate_gas({"from": creator, "value": fee_wei})
            else:
                gas_estimate = self.factory.functions.launchToken(
                    name, symbol, total_supply, project_url
                ).estimate_gas({"from": creator, "value": fee_wei})
        except Exception as e:
            # Common causes: factory address wrong for this chain, or the
            # call would revert (e.g. bad params) -- surface this clearly
            # rather than silently falling back to a guessed gas limit,
            # since launches are one-shot and expensive to get wrong.
            raise ValueError(
                f"Gas estimation failed -- the transaction would likely revert "
                f"or the factory address is wrong for {self.chain.name}: {e}"
            ) from e

        try:
            base_fee = self.w3.eth.gas_price
        except Exception as e:
            raise ConnectionError(f"Could not fetch gas price from {self.chain.name}: {e}") from e

        fn = (
            self.factory.functions.launchTokenWithAlloc(
                name, symbol, total_supply, project_url, wallets, bps
            )
            if use_alloc
            else self.factory.functions.launchToken(name, symbol, total_supply, project_url)
        )
        tx = fn.build_transaction({
            "from": creator,
            "nonce": nonce,
            "chainId": self.chain.chain_id,
            "value": fee_wei,
            "gas": int(gas_estimate * 1.2),  # 20% buffer -- estimates can be tight
            "gasPrice": base_fee,
        })

        return tx

    def explorer_tx_url(self, tx_hash: str) -> str:
        return f"{self.chain.explorer}/tx/{tx_hash}"


class EvmBondingCurveTxBuilder(EvmLaunchTxBuilder):
    """
    Same chain-config/RPC handling as EvmLaunchTxBuilder, but targets a
    deployed BondingCurveFactory instead of a plain LaunchTokenFactory --
    this is the revenue-generating launch path (see BondingCurve.sol).
    """

    def __init__(self, chain_key: str, factory_address: str, rpc_url: Optional[str] = None):
        if chain_key == "arc":
            raise UnsupportedChainError(
                "Arc bonding curve is held — Uniswap v4 on Arc, no V2 addLiquidityETH router."
            )
        if chain_key not in CHAIN_CONFIGS:
            raise UnsupportedChainError(
                f"'{chain_key}' not in supported chains: {list(CHAIN_CONFIGS)}"
            )
        self.chain_key = chain_key
        self.chain = CHAIN_CONFIGS[chain_key]
        rpc = rpc_url or self.chain.default_rpc
        if not rpc:
            raise ValueError(f"No RPC URL for {self.chain.name} -- pass one explicitly.")
        self.w3 = Web3(Web3.HTTPProvider(rpc))
        self.factory = self.w3.eth.contract(
            address=Web3.to_checksum_address(factory_address), abi=BONDING_CURVE_FACTORY_ABI
        )

    def build_unsigned_curve_launch_tx(
        self,
        creator_address: str,
        name: str,
        symbol: str,
        total_supply: int,
        graduation_eth_threshold: int,
        virtual_eth_reserve: int,
        virtual_token_reserve: int,
        dev_buy_wei: int = 0,
        start_time: int = 0,
        max_buy_wei: int = 0,
        alloc_wallets: list | None = None,
        alloc_bps: list | None = None,
    ) -> dict:
        creator = Web3.to_checksum_address(creator_address)

        try:
            nonce = self.w3.eth.get_transaction_count(creator, "pending")
        except Exception as e:
            raise ConnectionError(f"Could not reach {self.chain.name} RPC to fetch nonce: {e}") from e

        args = (
            name,
            symbol,
            total_supply,
            graduation_eth_threshold,
            virtual_eth_reserve,
            virtual_token_reserve,
            int(start_time or 0),
            int(max_buy_wei or 0),
            list(alloc_wallets or []),
            list(alloc_bps or []),
        )
        try:
            gas_estimate = self.factory.functions.launchFull(*args).estimate_gas(
                {"from": creator, "value": int(dev_buy_wei or 0)}
            )
        except Exception as e:
            raise ValueError(
                f"Gas estimation failed -- transaction would likely revert, or the "
                f"bonding curve factory address is wrong for {self.chain.name}: {e}"
            ) from e

        try:
            base_fee = self.w3.eth.gas_price
        except Exception as e:
            raise ConnectionError(f"Could not fetch gas price from {self.chain.name}: {e}") from e

        return self.factory.functions.launchFull(*args).build_transaction({
            "from": creator,
            "nonce": nonce,
            "chainId": self.chain.chain_id,
            "value": int(dev_buy_wei or 0),
            "gas": int(gas_estimate * 1.2),
            "gasPrice": base_fee,
        })
