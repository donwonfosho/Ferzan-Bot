# Ferzan Liquidity Bot

Telegram bot for liquidity management and market-making tools.

## Folders

- `liq/` — the real bot logic: `market_maker.py`,
  `liquidity_commands.py`, `subscription.py`, `credentials_db.py`
- `sandbox/` — a separate demo/reference implementation, kept apart
  from production code on purpose. Do not deploy from `sandbox/`.

## Deploy

Entry point: `liq_bot.py`. Runs as its own systemd service
(`ferzan-liq.service`), independent of the other bots.
