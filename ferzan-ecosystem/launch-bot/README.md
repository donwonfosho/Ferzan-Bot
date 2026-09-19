# Ferzan Launch Bot

Non-custodial launcher from Telegram. Creator signs in the Mini App.
Ferzan takes a treasury fee. No custody of user keys.

## Live today

- EVM plain ERC-20 — ETH, Base, BNB, Hood
- EVM bonding curve — same chains, launchFull (allocs, start window, max buy, dev buy)
- Solana plain SPL + treasury fee
- Mini App: miniapp/evm.html, miniapp/solana.html
- Internal API for the Trade Desk: /internal/referrer-wallet/{id}, /internal/curve-for-token/{addr}

## Labeled coming soon (honest)

- Meteora DBC (button exists; mints plain + fee until the pool ix is wired)
- Tron TRC-20
- TON
- Arc (decimals / V2 router not safe yet; ARC_LAUNCH_LIVE=0)

## Files

launch_bot.py            Telegram conversation
launch_bot_db.py         SQLite (LAUNCH_DB_PATH must be absolute)
api.py                   FastAPI + receipt parse + internal lookups
evm_launch.py            unsigned EVM txs
solana_launch.py         unsigned SPL + fee
meteora_launch.py        placeholder until DBC ix ships
tron_launch.py           coming-soon builder
ton_launch.py            coming-soon builder
contracts/               LaunchToken + BondingCurve + factories
miniapp/evm.html
miniapp/solana.html      Reown getProviders().solana
scripts/deploy.js
DEPLOY_MAINNET.md

## Run

LAUNCHBOT_TOKEN=...
LAUNCH_DB_PATH=/opt/ferzan/app/launch/launch_bot.db
MINI_APP_BASE_URL=https://launch.ferzaneco.com
LAUNCH_FEE_WEI=10000000000000000
PLATFORM_TREASURY_EVM=0x4d5955afb9ABF5943729CB74A0196498483e4622
PLATFORM_TREASURY_SOL=6yxsKcSeqAcoLXgyKDtVVW7Hb2d4uLYVT8X9zGa64HRp
INTERNAL_API_TOKEN=long-random
python launch_bot.py
uvicorn api:app --host 127.0.0.1 --port 8000
