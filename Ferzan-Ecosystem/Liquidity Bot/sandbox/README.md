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
