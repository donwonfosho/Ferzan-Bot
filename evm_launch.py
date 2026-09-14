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
}

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
        "stateMutability": "nonpayable",
        "type": "function",
    }
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
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


class UnsupportedChainError(Exception):
    pass


class EvmLaunchTxBuilder:
    def __init__(self, chain_key: str, factory_address: str, rpc_url: Optional[str] = None):
        if chain_key not in CHAIN_CONFIGS:
            raise UnsupportedChainError(
                f"'{chain_key}' not in supported chains: {list(CHAIN_CONFIGS)}"
            )
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

        try:
            gas_estimate = self.factory.functions.launchToken(
                name, symbol, total_supply, project_url
            ).estimate_gas({"from": creator})
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

        tx = self.factory.functions.launchToken(
            name, symbol, total_supply, project_url
        ).build_transaction({
            "from": creator,
            "nonce": nonce,
            "chainId": self.chain.chain_id,
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
        if chain_key not in CHAIN_CONFIGS:
            raise UnsupportedChainError(
                f"'{chain_key}' not in supported chains: {list(CHAIN_CONFIGS)}"
            )
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
    ) -> dict:
        creator = Web3.to_checksum_address(creator_address)

        try:
            nonce = self.w3.eth.get_transaction_count(creator, "pending")
        except Exception as e:
            raise ConnectionError(f"Could not reach {self.chain.name} RPC to fetch nonce: {e}") from e

        args = (name, symbol, total_supply, graduation_eth_threshold, virtual_eth_reserve, virtual_token_reserve)
        try:
            gas_estimate = self.factory.functions.launch(*args).estimate_gas({"from": creator})
        except Exception as e:
            raise ValueError(
                f"Gas estimation failed -- transaction would likely revert, or the "
                f"bonding curve factory address is wrong for {self.chain.name}: {e}"
            ) from e

        try:
            base_fee = self.w3.eth.gas_price
        except Exception as e:
            raise ConnectionError(f"Could not fetch gas price from {self.chain.name}: {e}") from e

        return self.factory.functions.launch(*args).build_transaction({
            "from": creator,
            "nonce": nonce,
            "chainId": self.chain.chain_id,
            "gas": int(gas_estimate * 1.2),
            "gasPrice": base_fee,
        })
