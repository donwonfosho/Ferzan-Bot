/**
 * scripts/deploy.js
 *
 * Deploys LaunchTokenFactory and BondingCurveFactory to whichever
 * network you run this against.
 *
 * Usage:
 *   ROUTER_ADDRESS=0x... PLATFORM_TREASURY=0x... \
 *     npx hardhat run scripts/deploy.js --network sepolia
 *
 * ROUTER_ADDRESS is deliberately NOT hardcoded or defaulted. During
 * research for this project, searches for official Uniswap-V2-style
 * router addresses on these testnets returned inconsistent results
 * across sources -- not confident enough to bake any specific address
 * into code that deploys contracts holding real (test) funds.
 *
 * Two ways to get a real ROUTER_ADDRESS:
 *   1. Look up the CURRENT official address yourself:
 *        - Uniswap: https://docs.uniswap.org/contracts/v2/reference/smart-contracts/v2-deployments
 *        - PancakeSwap (BSC): https://developer.pancakeswap.finance/contracts/exchange/addresses
 *      Confirm it's a V2-interface router (has addLiquidityETH) --
 *      V3 routers use a fundamentally different interface and will NOT
 *      work with BondingCurve.sol as written.
 *   2. Deploy your own minimal Uniswap-V2-compatible router+factory to
 *      the testnet first. This guarantees interface compatibility and
 *      sidesteps the address-verification problem entirely -- often the
 *      more reliable choice for a first testnet pass. Uniswap's V2 core
 *      + periphery contracts are open source and standard to redeploy.
 */

const hre = require("hardhat");

async function main() {
  const routerAddress = process.env.ROUTER_ADDRESS;
  const platformTreasury = process.env.PLATFORM_TREASURY;

  if (!routerAddress) {
    throw new Error(
      "ROUTER_ADDRESS not set -- see the comment at the top of this script " +
      "for why this isn't defaulted, and how to get a real one."
    );
  }
  if (!platformTreasury) {
    throw new Error("PLATFORM_TREASURY not set -- your fee-receiving wallet address.");
  }

  const [deployer] = await hre.ethers.getSigners();
  console.log("Deploying with account:", deployer.address);
  console.log("Network:", hre.network.name);

  const launchFeeWei = process.env.LAUNCH_FEE_WEI || "0";
  const LaunchTokenFactory = await hre.ethers.getContractFactory("LaunchTokenFactory");
  const plainFactory = await LaunchTokenFactory.deploy(platformTreasury, launchFeeWei);
  await plainFactory.waitForDeployment();
  console.log("LaunchTokenFactory deployed:", await plainFactory.getAddress());
  console.log("Plain launch fee (wei):", launchFeeWei);

  const BondingCurveFactory = await hre.ethers.getContractFactory("BondingCurveFactory");
  const curveFactory = await BondingCurveFactory.deploy(routerAddress, platformTreasury);
  await curveFactory.waitForDeployment();
  console.log("BondingCurveFactory deployed:", await curveFactory.getAddress());

  console.log("\n--- Copy these into api.py's FACTORY_ADDRESSES ---");
  console.log(`"${hre.network.name}": {`);
  console.log(`  "plain": "${await plainFactory.getAddress()}",`);
  console.log(`  "bonding_curve": "${await curveFactory.getAddress()}",`);
  console.log(`}`);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
