# Source of truth — this zip

There is no confluence-bot/ folder in this pack. Use these folders:

| Folder | What runs | Real entry file |
|---|---|---|
| trade-desk/ | Ferzan Trade Desk | bot.py |
| buy-bot/ | Ferzan Buy Bot | buy_bot.py |
| liquidity-bot/ | Ferzan Liquidity Bot | liq_bot.py |
| launch-bot/ | Launch Telegram bot + FastAPI + Mini App | launch_bot.py, api.py |
| guardian-bot/ | Ferzan Guardian | guardian_bot.py |

Shared Python used by the Desk lives ONLY in trade-desk/
(chains.py, db.py, evm_signer.py, fees.py, signer.py,
user_wallets.py, price_fetcher.py). Do not keep a second copy
elsewhere. Buy / Liq / Guardian are standalone and do not import those.

Launch files live ONLY in launch-bot/ (not launch/launch_bot.py).

On the droplet you can flatten into /opt/ferzan/app, but each
filename should exist once.
