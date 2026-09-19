# Source of truth — this zip

Root folder: Ferzan-Ecosystem

On disk the bot folders are titled with spaces so GitHub reads cleanly:

| Folder on disk | README name | Entry |
|---|---|---|
| Trade Desk/ | trade-desk/ | bot.py |
| Launch Bot/ | launch-bot/ | launch_bot.py + api.py |
| Buy Bot/ | buy-bot/ | buy_bot.py |
| Guardian Bot/ | guardian-bot/ | guardian_bot.py |
| Liquidity Bot/ | liquidity-bot/ | liq_bot.py |
| Signals/ | signals | README only — feed code is still in Trade Desk/bot.py |

Every bot folder is flat. Solidity (.sol), Mini App HTML, Hardhat
scripts, and liquidity modules sit next to the entry file.

Desk-only modules (do not duplicate):
chains.py, db.py, evm_signer.py, fees.py, signer.py, user_wallets.py, price_fetcher.py

Launch Bot nested folders (required):
  contracts/                 production Solidity
  contracts/mocks/           MockUniswapV2Router.sol, ReenteringTreasury.sol
  miniapp/                   evm.html, solana.html
  test/                      LaunchToken.test.js
  scripts/                   deploy.js

Liquidity Bot nested folders:
  liq/                       production modules
  sandbox/                   demo only — do not deploy

Every bot folder has its own .env.example.
