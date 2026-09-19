# Ferzan Buy Bot

Standalone Telegram bot that posts buy alerts and notifications —
a lightweight companion to the Trade Desk, not a trading engine itself.

## What it does

Watches for buy activity and pushes formatted alerts into Telegram.

## Deploy

Entry point: `buy_bot.py`. Runs as its own systemd service
(`ferzan-buy.service`), independent of the other bots.
