require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

/**
 * hardhat.config.js
 *
 * Testnet-only config on purpose -- no mainnet network is defined here.
 * Add one yourself only once contracts are audited and you're
 * deliberately ready for it; keeping it absent is a small guardrail
 * against an accidental `--network mainnet` typo during testing.
 */

const {
  DEPLOYER_PRIVATE_KEY,
  SEPOLIA_RPC_URL,
  BSC_TESTNET_RPC_URL,
  BASE_SEPOLIA_RPC_URL,
  ROBINHOOD_TESTNET_RPC_URL,
  ETHERSCAN_API_KEY,
} = process.env;

const accounts = DEPLOYER_PRIVATE_KEY ? [DEPLOYER_PRIVATE_KEY] : [];

module.exports = {
  solidity: {
    version: "0.8.24",
    settings: {
      optimizer: { enabled: true, runs: 200 },
    },
  },
  networks: {
    sepolia: {
      url: SEPOLIA_RPC_URL || "",
      chainId: 11155111,
      accounts,
    },
    bscTestnet: {
      url: BSC_TESTNET_RPC_URL || "https://data-seed-prebsc-1-s1.binance.org:8545",
      chainId: 97,
      accounts,
    },
    baseSepolia: {
      url: BASE_SEPOLIA_RPC_URL || "https://sepolia.base.org",
      chainId: 84532,
      accounts,
    },
    robinhoodTestnet: {
      // Chain ID verified during project research; RPC URL per Robinhood's
      // own docs (docs.robinhood.com/chain) -- confirm both are still
      // current before use, this network is very new.
      url: ROBINHOOD_TESTNET_RPC_URL || "",
      chainId: 46630,
      accounts,
    },
  },
  etherscan: {
    // Etherscan's V2 API key covers Ethereum/BSC/Base -- Robinhood Chain's
    // Blockscout explorer verification works differently; see its own
    // docs if you want contract verification there too.
    apiKey: ETHERSCAN_API_KEY || "",
  },
};
