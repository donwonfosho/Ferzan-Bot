# Ferzan Ecosystem

Five Telegram bots covering token trading, token launching, liquidity
management, safety scanning, and buy alerts — built around Solana and
EVM chains, non-custodial throughout: every bot signs with the user's
own wallet, never holds user private keys or funds itself.

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

- Non-custodial: every trade or launch transaction is built unsigned
  by the backend and signed by the user's own wallet. No bot in this
  repo holds a user's private key.
- `launch-bot/contracts/` holds the on-chain Solidity contracts. These
  have not yet had a professional third-party audit — treat that as
  required before opening bonding-curve launches to users beyond
  internal testing.
