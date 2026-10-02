# Ferzan Ecosystem

Five Telegram bots covering token trading, token launching, liquidity
management, safety scanning, and buy alerts — built around Solana and
EVM chains. The Launch Bot is non-custodial (the user's own wallet signs). The Trade Bot is a
custodial hot wallet with encrypted keys that users can export or withdraw at any time.

## Bots

| Folder | Bot | Entry point | What it does |
|---|---|---|---|
| `trade-desk/` | Ferzan Trade Desk | `bot.py` | Buy/sell, sniping, copy trading, limit orders, trailing stops, cross-chain bridging |
| `launch-bot/` | Ferzan Launch | `launch_bot.py` + `api.py` | Non-custodial token launcher — plain mint or bonding curve, multi-chain |
| `buy-bot/` | Ferzan Buy Bot | `buy_bot.py` | Buy alerts and notifications |
| `guardian-bot/` | Ferzan Guardian | `guardian_bot.py` | Contract safety scanning (honeypot, mint authority, LP checks) |
| `liquidity-bot/` | Ferzan Liquidity | `liq_bot.py` | Liquidity management and market-making tools |

Each bot is self-contained in its own folder with its own
`.env.example` and systemd `.service` file. See `SOURCE.md` for the
exact file map and which files are the real, deployed ones.

## Status

- **Live:** trading, sniping, copy-trading, limit/trailing-stop orders
  on the trade desk; plain-mint and bonding-curve launches on Ethereum,
  BNB Chain, Base, and Robinhood Chain; plain SPL launches on Solana.
- **Labeled coming soon (honest, not hidden):** Meteora-based Solana
  bonding curves, Tron launches, TON launches, Arc — each held behind
  an explicit flag or a clear "coming soon" label until finished and
  tested. See `launch-bot/README.md` for the specific reason each one
  is held.

## Architecture note

The trade desk and launch bot are independent services that
communicate over a small internal HTTP API (`/internal/referrer-wallet`,
`/internal/curve-for-token`) rather than sharing files or a database
directly — they can be deployed, updated, and restarted independently.

## Security

- Launch Bot is non-custodial: launch transactions are built unsigned and signed by the
  user's own wallet. The Trade Bot is custodial: it holds encrypted per-user trading keys.
- `launch-bot/contracts/` holds the on-chain Solidity contracts. These
  have not yet had a professional third-party audit — treat that as
  required before opening bonding-curve launches to users beyond
  internal testing.
