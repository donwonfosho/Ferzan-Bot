# Ferzan Trade Desk

Custodial hot-wallet trading bot for Telegram — buy, sell, snipe, and manage
positions across Solana, EVM, Tron and TON from a per-user trading wallet.

## Features

- Buy/sell any token by pasting a contract address
- Sniping — arm a first-block buy on new launches
- Copy trading — watch and mirror a wallet's trades
- Limit orders, take-profit/stop-loss, and trailing stops (`/trail`)
- Honeypot detection and anti-MEV protection (on by default)
- LP-yank rug protection (`/lpguard`) — off by default, opt in per user
- Cross-chain bridging via Relay and deBridge
- Referral fee rebates (`/refer`, `/referwallet`) and volume-based fee tiers

## Fees

Per-trade cut, disclosed. Discounted by 30-day trading volume and by
FERZAN holder fee discounts, once the token is live.

## Custody

This is a custodial hot wallet. Each Telegram account gets a trading wallet whose
key is encrypted on the server so the bot can sign trades. Users can export keys or
withdraw at any time; keep only trading funds in it. (The Launch Bot is the
non-custodial one: it never holds keys.)
