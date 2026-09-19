# Ferzan Launch — mainnet checklist

Treasury already in use:
- SOL `6yxsKcSeqAcoLXgyKDtVVW7Hb2d4uLYVT8X9zGa64HRp`
- EVM `0x4d5955afb9ABF5943729CB74A0196498483e4622`

V2 routers (addLiquidityETH) — bonding curve only:
- Ethereum Uniswap V2 `0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D`
- BNB Pancake V2 `0x10ED43C718714eb63d5aA57B78B54704E256024E`
- Base Uniswap V2 `0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24`
- Hood: set `ROUTER_ADDRESS` from current docs before curve deploy
- Arc: **do not deploy a bonding-curve factory.** Arc DEX is Uniswap v4. `_graduate()` needs V2 `addLiquidityETH` and will not work.

## 1. Compile and test on the droplet

```
cd /opt/ferzan/app/launch
npm install
npx hardhat compile
npx hardhat test
```

## 2. Deploy factories (one network at a time)

```
export DEPLOYER_PRIVATE_KEY=0xYOUR_DEPLOYER
export PLATFORM_TREASURY=0x4d5955afb9ABF5943729CB74A0196498483e4622
export LAUNCH_FEE_WEI=10000000000000000
export ROUTER_ADDRESS=0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D
npx hardhat run scripts/deploy.js --network ethereum
```

`LAUNCH_FEE_WEI` is required for a paid plain EVM launch. If you omit it, `deploy.js` and `evm_launch.py` default to **0** and plain launches charge nothing.

Repeat with the matching router for `bsc`, `base`, `robinhood`. Do **not** run curve deploy on `arc`.

Copy the printed addresses into `/opt/ferzan/.env`:

```
FACTORY_ETH_PLAIN=
FACTORY_ETH_CURVE=
FACTORY_BSC_PLAIN=
FACTORY_BSC_CURVE=
FACTORY_BASE_PLAIN=
FACTORY_BASE_CURVE=
FACTORY_HOOD_PLAIN=
FACTORY_HOOD_CURVE=
FACTORY_ARC_PLAIN=
FACTORY_ARC_CURVE=
FACTORY_TRX_PLAIN=
ARC_LAUNCH_LIVE=0
LAUNCH_FEE_ARC=1000000
PLATFORM_TREASURY_EVM=0x4d5955afb9ABF5943729CB74A0196498483e4622
PLATFORM_TREASURY_SOL=6yxsKcSeqAcoLXgyKDtVVW7Hb2d4uLYVT8X9zGa64HRp
PLATFORM_TREASURY_TRX=
PLATFORM_TREASURY_TON=
LAUNCH_FEE_WEI=10000000000000000
LAUNCH_FEE_LAMPORTS=50000000
LAUNCH_FEE_SUN=50000000
LAUNCH_FEE_NANOTON=100000000
METEORA_CONFIG=
ARC_RPC_URL=https://rpc.mainnet.arc.io
```

## 3. Launch one token yourself per live path

Telegram → Ferzan Launch → chain:

- EVM (ETH / BNB / Base / Hood): **Plain**, then **Bonding curve** after that factory is deployed
- Arc: held. Confirm RPC. Fees use `LAUNCH_FEE_ARC` (6-dec USDC). Curve stays off (Uniswap v4, no V2). Set `ARC_LAUNCH_LIVE=1` only after a dry check
- Solana: **Plain SPL**. Meteora = coming soon (mint + fee, no DBC pool)
- Tron: coming soon — Mini App cannot sign a TronWeb tx yet
- TON: fee memo only, jetton minter coming soon

Buy and sell on an EVM curve. Confirm fee hits the treasury. Confirm graduation burns LP to `0xdead`.

Do not open the bot to other users until you have signed one real token on each path you advertise as live.
