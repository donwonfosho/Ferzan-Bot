require("@nomicfoundation/hardhat-toolbox");
require("dotenv").config();

const {
  DEPLOYER_PRIVATE_KEY,
  SEPOLIA_RPC_URL,
  BSC_TESTNET_RPC_URL,
  BASE_SEPOLIA_RPC_URL,
  ROBINHOOD_TESTNET_RPC_URL,
  ETHEREUM_RPC_URL,
  BSC_RPC_URL,
  BASE_RPC_URL,
  ROBINHOOD_RPC_URL,
  ETHERSCAN_API_KEY,
} = process.env;

const accounts = DEPLOYER_PRIVATE_KEY ? [DEPLOYER_PRIVATE_KEY] : [];

module.exports = {
  solidity: {
    version: "0.8.24",
    settings: { optimizer: { enabled: true, runs: 200 } },
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
      url: ROBINHOOD_TESTNET_RPC_URL || "",
      chainId: 46630,
      accounts,
    },
    ethereum: {
      url: ETHEREUM_RPC_URL || "https://ethereum.publicnode.com",
      chainId: 1,
      accounts,
    },
    bsc: {
      url: BSC_RPC_URL || "https://bsc-dataseed.binance.org",
      chainId: 56,
      accounts,
    },
    base: {
      url: BASE_RPC_URL || "https://mainnet.base.org",
      chainId: 8453,
      accounts,
    },
    robinhood: {
      url: ROBINHOOD_RPC_URL || "https://rpc.mainnet.chain.robinhood.com",
      chainId: 4663,
      accounts,
    },
    arc: {
      url: process.env.ARC_RPC_URL || "https://rpc.mainnet.arc.io",
      chainId: 5042,
      accounts,
    },
  },
  etherscan: {
    apiKey: ETHERSCAN_API_KEY || "",
  },
};
