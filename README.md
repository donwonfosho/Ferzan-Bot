# CONFLUENCE

A Telegram bot for **scored trading signals**, **paper execution**, **wallet watches**, **drawdown alerts**, and a **disclosed per-trade cut**.

It starts from the price-alert bot Claude already wrote (`/price`, `/alert`, `/list`, `/remove`) and adds the thing Banana Gun-class bots do not do: **it will refuse a trade**.

Banana Gun, Trojan, Maestro, BonkBot optimize for speed — paste a contract, buy in the same chat. That is a crowded product. Confluence scores five independent factors (liquidity, activity, momentum, order flow, structure) and only paper-fills when they agree.

## What this is

- Signal cards with the math visible
- Paper long with auto stop / target
- Daily loss circuit breaker
- Watchlist scanner that pings you only on high-confluence names
- Original CoinGecko price alerts, kept intact
- Allowlist so you can run it as a private bot
- Wallet-activity pings (Etherscan V2 + Helius / public Solana RPC)
- Portfolio drawdown alerts off paper peak
- 0.50% platform cut on paper fills, ready to map onto Jupiter/0x fee accounts

## What this is not

- Not a live sniper
- Not a custodial wallet
- Not financial advice
- Not a “guaranteed signal” service

Live on-chain execution (Jupiter / Uniswap, key custody, MEV relays) is how those other bots work — and how users get drained when a bot is compromised. That path is intentionally left out. If you later want a live adapter, it should sign on *your* machine, never store a seed in this process, and stay behind the same confluence + risk checks.

## Setup

```bash
cd confluence-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

1. Open Telegram, talk to [@BotFather](https://t.me/BotFather)
2. `/newbot` → copy the token into `.env` as `TELEGRAM_BOT_TOKEN`
3. Optional: set `ALLOWED_USER_IDS` to your numeric id (`@userinfobot` will tell you)
4. Run it:

```bash
python bot.py
```

Then DM your bot `/start`.

## Commands

| Command | What it does |
|---|---|
| `/signal sol` | Score a ticker or contract |
| `/buy jup` | Paper-buy only if score ≥ your floor |
| `/positions` | Open book + recent closes |
| `/sell 3` | Close position `#3` at mark |
| `/watch bonk` | Add to the scanner |
| `/journal` | Decision log |
| `/settings floor 70` | Raise the refusal threshold |
| `/price sol` | CoinGecko spot (original bot) |
| `/alert sol above 200` | Price ping (original bot) |
| `/resetpaper` | Wipe the paper book back to $10k |
| `/watchwallet sol <addr>` | Ping on new on-chain prints |
| `/wallets` | List watched wallets |
| `/drawdown` | Paper peak vs now |
| `/fees` | Your cut paid + live fee-account status |
| `/treasury` | Operator fee ledger |

Pasting a ticker or contract with no slash also scores it.

## Fees (your cut)

Paper fills take `FEE_BPS` (default 50 = 0.50%) out of notional on both buy and sell. The ticket text shows the cut before it is recorded. Hard cap in code is 1%.

Live money does **not** flow through this process. When you later attach Jupiter or 0x:

- Solana: pass `platformFeeBps` + your `JUPITER_FEE_ACCOUNT` (token account for the fee mint)
- EVM: pass `swapFeeRecipient` + `swapFeeBps` to 0x

The router pays `FEE_WALLET_SOL` / `FEE_WALLET_EVM`. No user seed is stored. That is the same economic model as Banana Gun without the custody hole.

## On-chain providers

Free, no card:

1. [Etherscan API V2](https://etherscan.io/apis) — one key, `chainid` selects eth/base/bsc/arb/op/polygon
2. [Helius](https://dashboard.helius.dev) — parsed Solana history. If the key is empty, Solana watch falls back to public `getSignaturesForAddress`

Without an Etherscan key, `/watchwallet eth …` will tell you to add one. Solana watch works the same day with no key.

## How scoring works

Each factor is 0–100, then weighted:

- Liquidity 22%
- Activity 18%
- Momentum 24%
- Order flow 16%
- Structure 20%

Hard vetoes (brand-new pool, sub-$40k liquidity, already +25% on 5m) cap the score at 54 so the bot cannot “like” a rug setup. Bias is `LONG` / `WATCH` / `AVOID`. A `LONG` below your floor still requires the Override button.

## Files

```
bot.py             Telegram layer
db.py              SQLite: alerts, users, positions, wallets, fees
price_fetcher.py   CoinGecko + DexScreener
confluence.py      Scoring
trading.py         Paper broker + risk vault
onchain.py         Etherscan V2 + Helius/public Solana
fees.py            Cut math, ledger, Jupiter/0x param helpers
```

Same three dependencies Claude used: `python-telegram-bot`, `requests`, `python-dotenv`.
