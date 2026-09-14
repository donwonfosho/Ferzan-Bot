# Getting to Live Testing — Step by Step

This is the concrete path from "code that's never touched a network" to
"actually testing a launch on real testnets." Follow in order.

---

## ⚠️ Read this before deploying `BondingCurveFactory` anywhere

`BondingCurve.sol` was written against the **Uniswap V2** router
interface (`addLiquidityETH`). Several chains — Base in particular —
have shifted toward **Uniswap V3**, which uses a fundamentally different
liquidity model (concentrated liquidity, NFT-based positions, no
`addLiquidityETH` at all). **A V3 router address will not work with this
contract.**

During research for this project, searches for the exact current
official V2-style router address on each testnet returned inconsistent
results across sources — not something to trust enough to hardcode. You
have two real options:

1. **Look it up yourself, carefully**, confirming it's specifically a V2
   interface:
   - Uniswap: https://docs.uniswap.org/contracts/v2/reference/smart-contracts/v2-deployments
   - PancakeSwap (BSC): https://developer.pancakeswap.finance/contracts/exchange/addresses
2. **Deploy your own minimal Uniswap-V2-compatible router+factory to the
   testnet first.** This guarantees the interface matches what
   `BondingCurve.sol` expects, and sidesteps the address-verification
   problem entirely. Uniswap's V2 core + periphery contracts are open
   source and standard to redeploy for exactly this purpose. For a first
   testnet pass, this is often the more reliable path.

Either way, verify `addLiquidityETH(address,uint256,uint256,uint256,address,uint256)`
actually exists on whatever address you use — a quick call to the
contract on a block explorer confirms this before you find out the hard
way mid-launch.

---

## Step 1 — Get testnet funds

| Chain | Faucet |
|---|---|
| Sepolia (Ethereum) | https://sepoliafaucet.com or your RPC provider's own faucet |
| BSC Testnet | https://testnet.bnbchain.org/faucet-smart |
| Base Sepolia | Coinbase/Alchemy faucets, or bridge Sepolia ETH via https://bridge.base.org (testnet mode) |
| Robinhood Chain Testnet | https://faucet.testnet.chain.robinhood.com |
| Solana Devnet | `solana airdrop 2 --url devnet` (Solana CLI), or https://faucet.solana.com |

You'll need funds in **two** wallets: one to deploy contracts (the
`DEPLOYER_PRIVATE_KEY` in Hardhat), and one to actually test launching
through the bot (your personal test wallet, connected via the Mini App).
Keep these separate so you don't confuse "gas for deployment" with
"gas for testing a launch."

## Step 2 — Get RPC endpoints

Free tier from Alchemy or Infura covers Sepolia and Base Sepolia. BSC
Testnet and Robinhood Chain Testnet have public endpoints (already in
`hardhat.config.js`'s defaults) — fine for testing, not for production
load.

## Step 3 — Compile and deploy the contracts

```bash
npm install
npx hardhat compile
```
This is the first real test of the Solidity — if something's wrong with
the contracts, this is where you'll find out.

```bash
ROUTER_ADDRESS=<from the warning above> \
PLATFORM_TREASURY=<your fee-receiving wallet> \
npx hardhat run scripts/deploy.js --network sepolia
```
Repeat for `bscTestnet`, `baseSepolia`, `robinhoodTestnet`. Copy each
run's output into `api.py`'s `FACTORY_ADDRESSES`.

## Step 4 — Run the test suite

```bash
npx hardhat test
```
`test/LaunchToken.test.js` has real assertions for `LaunchToken` and
placeholder (`this.skip()`) tests for `BondingCurve` that need a mock
router fixture filled in — worth doing before trusting the curve
contract with anything, even test funds. The reentrancy test in
particular is the highest-value one to actually finish, not skip.

## Step 5 — Get a Reown Project ID

Free signup at https://dashboard.reown.com. Put the same ID in both
`miniapp/evm.html` and `miniapp/solana.html`.

## Step 6 — Deploy the bot infrastructure

Follow `LAUNCH_BOT_DEPLOYMENT.md` — droplet, nginx, HTTPS, both systemd
services. Point `.env`'s RPC URLs at your testnet endpoints from Step 2,
not mainnet ones.

## Step 7 — Solana-specific: set devnet everywhere

- Backend `.env`: `SOLANA_RPC_URL=https://api.devnet.solana.com`
- Your test wallet (Phantom/Solflare): switch its own network setting to
  Devnet — a mainnet-configured wallet will not sign devnet transactions
  correctly even if the RPC URL is right server-side.

## Step 8 — Do one real launch, end to end, per chain

`/launch` in Telegram → pick a chain → plain mode first (fewer moving
parts than bonding curve) → confirm it actually appears on that testnet's
block explorer. Only after plain mode works reliably, try bonding-curve
mode — and expect the Mini App JavaScript to need real fixes at this
point, since it's the least-tested part of the whole project.

## What's still blocking pump.fun routing specifically

`pumpfun_launch.py` has two `NotImplementedError` functions. Fill these
in against a live IDL fetch (the code already fetches pump.fun's IDL via
`anchorpy` — run it, inspect what comes back, and use that to build the
account context) before Solana's `pumpfun` mode will work at all. Test
this significantly later than everything else — it depends on the most
external, least-controlled piece of the whole stack.
