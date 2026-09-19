# Ferzan Trade Desk

Non-custodial trading bot for Telegram — buy, sell, snipe, and manage
positions across Solana and EVM chains, signed with your own wallet.

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
staking (`/stake`) toward the Ferzan token, once live.

## Non-custodial

Every trade is built as an unsigned transaction and signed with the
user's own key. This bot does not hold user funds.
