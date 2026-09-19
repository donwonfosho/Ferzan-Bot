# Ferzan Ecosystem — latest pack

One folder per bot. Deploy each into `/opt/ferzan/app` as you already do
(or keep them in these folders and point systemd WorkingDirectory there).

## Folders

| Folder | Bot | Entry |
|---|---|---|
| trade-desk/ | Ferzan Trade Desk | bot.py |
| buy-bot/ | Ferzan Buy Bot | buy_bot.py |
| liquidity-bot/ | Ferzan Liquidity Bot | liq_bot.py |
| launch-bot/ | Ferzan Launch Bot + Mini App + API | launch_bot.py + api.py |
| guardian-bot/ | Ferzan Guardian | guardian_bot.py |

## Rules

- Source of truth is these folders. Do not keep a second bot.py at repo root.
- Env template: each bot folder has .env.example. See SOURCE.md.
- Launch Mini App: launch-bot/miniapp/evm.html and solana.html.
- Trade Desk talks to Launch API at LAUNCH_API_URL (default http://127.0.0.1:8000).
  Set INTERNAL_API_TOKEN on both processes.

## Restart after upload

cd /opt/ferzan/app
unzip -o ferzan-ecosystem.zip
systemctl restart ferzan ferzan-buy ferzan-liq ferzan-launch ferzan-guardian
