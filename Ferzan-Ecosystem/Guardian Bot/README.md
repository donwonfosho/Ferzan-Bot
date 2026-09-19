# Ferzan Guardian

Contract safety scanner for Telegram — checks a token before you buy it.

## What it checks

- Honeypot detection (can you actually sell?)
- Mint authority / freeze authority status
- Known scam-contract flags

## Deploy

Entry point: `guardian_bot.py`. Runs as its own systemd service
(`ferzan-guardian.service`), independent of the other bots.
