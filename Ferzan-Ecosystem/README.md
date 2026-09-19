# Ferzan Ecosystem

Five Telegram bots covering token trading, token launching, liquidity
management, safety scanning, and buy alerts — built around Solana and
EVM chains, non-custodial throughout: every bot that moves funds signs
with the user's own wallet, never holds user private keys or funds itself.

## Folders

| Folder | Bot | Entry point | What it does |
|---|---|---|---|
| `Trade Desk/` | Ferzan Trade Desk | `bot.py` | Buy/sell, sniping, copy trading, limit orders, trailing stops, bridging |
| `Launch Bot/` | Ferzan Launch Bot | `launch_bot.py` + `api.py` | Non-custodial token launcher — plain mint or bonding curve, multi-chain |
| `Buy Bot/` | Ferzan Buy Bot | `buy_bot.py` | Buy alerts and notifications |
| `Guardian Bot/` | Ferzan Guardian | `guardian_bot.py` | Contract safety scanning (honeypot, mint authority, LP checks) |
| `Liquidity Bot/` | Ferzan Liquidity Bot | `liq_bot.py` | Liquidity management and market-making tools |
| `Signals/` | — | — | Docs only for now; the code still lives in `Trade Desk/bot.py` |

Each bot folder is self-contained with its own dependencies and its
own systemd `.service` file. See `SOURCE.md` for the exact file map
and which files are the real, deployed ones.

## Status

**Live:** trading, sniping, copy-trading, limit and trailing-stop
orders on the Trade Desk; plain-mint and bonding-curve launches on
Ethereum, BNB Chain, Base, and Robinhood Chain; plain SPL launches on
Solana; referral fee rebates and volume-tiered fees.

**Labeled coming soon (honest, not hidden):** Meteora-based Solana
bonding curves, Tron launches, TON launches, and Arc — each held
behind an explicit flag or a clear "coming soon" label until finished
and tested. See `Launch Bot/README.md` for the specific reason each
one is held.

## Architecture

Trade Desk and Launch Bot are independent services that communicate
over a small internal HTTP API (`/internal/referrer-wallet`,
`/internal/curve-for-token`) instead of sharing files or a database
directly — each can be deployed, updated, and restarted on its own.

## Security

- Non-custodial: every trade or launch transaction is built unsigned
  by the backend and signed by the user's own wallet.
- `Launch Bot/contracts/` holds the on-chain Solidity. These have not
  yet had a professional third-party audit — required before opening
  bonding-curve launches to users beyond internal testing.

## Deploy

Each folder can run from its own directory — point that bot's
`WorkingDirectory` in its `.service` file at its folder, and give it
its own `.env` (see each folder's `.env.example`).
