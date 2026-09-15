# Launch Bot — Complete Stack

Non-custodial multi-chain token launcher for Telegram: Solana, Ethereum,
BNB Chain, Base, and Robinhood Chain, with pump.fun-style bonding-curve
revenue on the EVM chains and fee-on-usage routing through pump.fun's
own program for Solana.

**This is now a complete architecture, not just building blocks** — every
piece from "user types /launch" to "token exists on-chain" has code.
Read "Honest status" below before assuming any of it is production-ready,
though — a lot of it needs real-world testing this sandbox couldn't do.

## Full file map

```
launch_bot.py                  -- Telegram bot: menu, conversation flow, opens the Mini App
launch_bot_db.py                -- shared SQLite state between the bot and the API
api.py                          -- FastAPI backend the Mini App talks to
miniapp/
  evm.html                      -- wallet-connect + signing UI for Ethereum/BNB/Base/Robinhood
  solana.html                   -- wallet-connect + signing UI for Solana
contracts/
  LaunchToken.sol                -- fixed-supply ERC-20, no mint function
  LaunchTokenFactory.sol         -- plain launch factory (mode 1)
  BondingCurve.sol                -- pump.fun-style curve, 1% fee, 70/30 platform/creator split
  BondingCurveFactory.sol         -- deploys token + curve together
evm_launch.py                   -- unsigned-tx builders (plain + bonding curve), 4 EVM chains
solana_launch.py                -- unsigned-tx builder, plain SPL launch
pumpfun_launch.py               -- routes Solana launches through pump.fun + your fee
requirements.txt
LAUNCH_BOT_DEPLOYMENT.md        -- full deployment walkthrough (nginx, HTTPS, systemd x2)
```

## How a launch actually flows, end to end

1. User sends `/launch` in Telegram
2. Bot conversation collects: chain → mode → name → symbol → supply
3. Bot writes a `launch_request` row to shared SQLite, opens the
   chain-appropriate Mini App (`evm.html` or `solana.html`) with that
   request's ID in the URL
4. Mini App loads request details, user taps "Connect Wallet" (Reown
   AppKit — MetaMask/WalletConnect for EVM, Phantom/Solflare for Solana)
5. Mini App calls the backend to build the actual unsigned transaction
   (this is where `evm_launch.py`/`solana_launch.py`/`pumpfun_launch.py`
   get invoked)
6. User's own wallet shows them the real transaction to approve — **the
   bot and backend never see a private key at any point**
7. Wallet signs and broadcasts; Mini App reports the result back to the
   backend, which notifies the user in Telegram chat

## Revenue model recap

| Chain | Mechanism |
|---|---|
| Ethereum, BNB, Base, Robinhood Chain | Your own `BondingCurve.sol` — 1% fee on every trade, forever, split 70/30 platform/creator |
| Solana | Fee bundled into transactions your bot initiates, routed through pump.fun's live program — not a persistent cut of all trading, since you don't own that venue |

## Getting to live testing

**→ Start with `TESTNET_SETUP.md`** — the concrete, ordered path from
this code to an actual test launch on real testnets, including a real
compatibility issue it flags (Uniswap V2 vs V3 router interfaces) and
faucet links for every chain.

Quick version:
```bash
pip install -r requirements.txt
npm install
npx hardhat compile
npx hardhat test          # test/LaunchToken.test.js -- some cases still need a mock-router fixture filled in
```
Then deploy per `TESTNET_SETUP.md`'s ordering, and full bot/API
deployment per `LAUNCH_BOT_DEPLOYMENT.md`.

## Verified facts (checked directly during this build, not from memory)

- Chain IDs: Ethereum 1, BNB Chain 56, Base 8453, Robinhood Chain 4663
- WalletConnect Inc. rebranded to **Reown**; their SDK is **Reown AppKit**,
  which explicitly documents Telegram Mini App support and covers both
  EVM and Solana
- pump.fun's program ID and public account-structure fragments, from
  public documentation (not a live on-chain check)

## Honest status, file by file — confidence varies a lot

- **`launch_bot.py`, `launch_bot_db.py`** — high confidence. The
  conversation flow was actually simulated end-to-end with stub objects
  during development (not just eyeballed) — state transitions, data
  handling, and the database write were all verified to work correctly.
  One real bug (a misregistered callback handler that would have broken
  the bot on every user's first click) was caught this way and fixed.

- **`api.py`** — high confidence in structure (FastAPI's core API is
  very stable), but not run against a live server — do a real local
  test before deploying.

- **`evm_launch.py`, `LaunchToken.sol`, `LaunchTokenFactory.sol`** —
  high confidence. Stable, well-trodden patterns.

- **`BondingCurve.sol`, `BondingCurveFactory.sol`** — **the highest-risk
  code in this project.** Not compiled or tested. Needs unit tests, fuzz
  testing, and a professional audit before real funds — this is not
  optional for a custom AMM/bonding-curve contract holding real money.

- **`solana_launch.py`** — medium confidence; `solana-py`/`solders` have
  broken compatibility across versions before.

- **`pumpfun_launch.py`** — lowest Python-side confidence, and
  incomplete by design (two functions intentionally raise
  `NotImplementedError` rather than guess at pump.fun's exact account
  structure).

- **`miniapp/evm.html`, `miniapp/solana.html`** — **lowest confidence of
  anything in this entire project.** Never run in a browser, never
  connected to a real wallet, never hit a real RPC endpoint — I have no
  way to test JavaScript/browser code in the environment I built this
  in. The overall flow (connect → build-tx → sign → report back) is the
  right architecture, but treat every Reown AppKit API call as
  "needs verification against current docs," not "known correct." This
  is the part of the whole stack most likely to need real debugging
  before it works.

## What's genuinely still missing

- Contracts aren't deployed anywhere yet — `TESTNET_SETUP.md` walks
  through this
- A Reown Project ID (free signup) needs to go in both Mini App files
- pump.fun's account context (the two `NotImplementedError`s)
- A real security audit of the bonding curve contracts — required before
  mainnet, not before testnet
- The `BondingCurve` test cases that currently call `this.skip()` need a
  real mock-router fixture
