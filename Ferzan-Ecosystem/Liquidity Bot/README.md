# Ferzan Liquidity Desk

Paste a CA. Get DexScreener TVL, 24h volume, and a rough price-impact estimate.

This is **not** a volume bot. It does not place buys to inflate candles.

## Do not

- Reuse `TELEGRAM_BOT_TOKEN` from Ferzan Trade Bot (`getUpdates` conflict).
- Run this as a replacement for `/opt/ferzan/app/bot.py`.
- Claim the impact numbers are exact on CLMM / Pump curves.

## Run (separate process)

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# put LIQ_BOT_TOKEN in .env
python3 telegram_bot.py
```

If you want this inside the trade bot later, say so — we add `/liq` to Ferzan instead of a second poller.

## Earn (referral) needs Trade Desk's internal API running

The Earn screen pulls real numbers from Trade Desk's `referral_ledger`
over `Trade Desk/internal_api.py` (its own small FastAPI process, port
8011, loopback only — see `ferzan-trade-api.service`). For it to work:

- Trade Desk's `ferzan-trade-api.service` must be running alongside its
  main bot process.
- Set `INTERNAL_API_TOKEN` in `/opt/ferzan/.env` to the **same** value
  Trade Desk uses (also consumed by Launch Bot's `api.py`) — all three
  bots share this one secret.
- Optionally set `TRADE_API_URL` here if the API isn't on
  `http://127.0.0.1:8011` (e.g. different port).

If the internal API is unreachable, Earn shows an honest "couldn't
reach the ledger" message instead of fabricated zeros.

## Holders needs per-chain API keys

Real holder counts come from a second data source per chain (neither
DexScreener nor free chain-explorer tiers give this away):

- `SOLSCAN_API_KEY` — Solana. Get a free-tier key at pro-api.solscan.io.
- `COVALENT_API_KEY` — Ethereum / BSC / Base. Get a free key at
  covalenthq.com (GoldRush).

Set either or both in `/opt/ferzan/.env` and restart the bot; no code
changes needed. Robinhood chain ("hood") has no supported provider yet.
Missing a key, or a failed lookup, shows "Holders — not available"
rather than a fake number.
