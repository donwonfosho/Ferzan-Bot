# Ferzan Launch Bot

Non-custodial multi-chain token launcher for Telegram. Creator signs
in the Mini App; Ferzan takes a treasury fee. No custody of user keys.

## Live today

- EVM plain ERC-20 — Ethereum, Base, BNB Chain, Robinhood Chain
- EVM bonding curve — same chains, via `launchFull` (team allocations,
  start-time window, max buy per wallet, dev buy at launch)
- Solana plain SPL + treasury fee
- Mini App: `miniapp/evm.html`, `miniapp/solana.html`
- Internal API for the Trade Desk: `/internal/referrer-wallet/{id}`,
  `/internal/curve-for-token/{addr}`

## Labeled coming soon (honest, not hidden)

- Meteora DBC bonding curve for Solana — button exists; mints plain +
  fee today, no pool yet
- Tron TRC-20 — held, Mini App can't sign a Tron tx yet
- TON — fee memo only, jetton minter not wired
- Arc — held: needs 6-decimal gas handling verified and no V2-style
  router confirmed live yet (`ARC_LAUNCH_LIVE=0`)

## Files

- `launch_bot.py` — Telegram conversation
- `launch_bot_db.py` — SQLite state (`LAUNCH_DB_PATH` must be absolute)
- `api.py` — FastAPI backend, receipt parsing, internal lookups
- `evm_launch.py` / `solana_launch.py` / `meteora_launch.py` /
  `tron_launch.py` / `ton_launch.py` — per-chain unsigned-tx builders
- `contracts/` — the on-chain Solidity (not yet professionally
  audited — required before opening bonding curves beyond internal
  testing)
- `miniapp/` — wallet-connect + signing UI
- `test/`, `scripts/` — Hardhat tests and deploy script

## Security

Non-custodial throughout: transactions are built unsigned and signed
by the creator's own wallet.
