"""
pumpfun_launch.py

Routes Solana token launches (and optionally buys) through pump.fun's
OWN, already-deployed, already-battle-tested program via CPI-equivalent
instruction composition -- we never deploy or own any Solana program
ourselves. Your fee is a plain SOL transfer bundled into the SAME
transaction as pump.fun's instruction, so it's atomic: the launch and
your fee either both happen or neither does.

WHY THIS APPROACH: writing a custom Solana bonding-curve program carries
real audit cost and risk (see the project notes on this). Routing
through pump.fun's existing program means the actual trading logic runs
on code that's processed years of real volume, and our own code surface
is limited to "compose a couple of instructions and add a transfer" --
a much smaller, more auditable footprint.

WHAT THIS DOES NOT DO: it does not tax every future trade on the token
the way owning the bonding curve would -- only actions your bot itself
initiates (the launch, and any buy/sell your UI offers) generate a fee.
Trades made directly on pump.fun's own site, or via anyone else's tool,
pay you nothing. That's the real cost of not owning the venue.

============================================================
VERIFY BEFORE USE -- THIS INTEGRATES WITH A THIRD PARTY'S PROGRAM
============================================================
- PUMPFUN_PROGRAM_ID below was found via public documentation during
  research for this project, not verified against a live RPC call from
  where I built this (no network access in that sandbox). Confirm it
  against pump.fun's current official docs before relying on it.
- This uses anchorpy to fetch pump.fun's IDL directly from their
  on-chain IDL account, rather than me hand-encoding instruction bytes
  -- deliberately, to avoid an entire class of "I guessed the account
  order/discriminator wrong" bugs. If pump.fun doesn't publish an
  on-chain IDL (some programs disable this), you'll need to source
  their IDL JSON from documented mirrors instead and load it manually.
- pump.fun can change their program's interface at any time, entirely
  outside your control -- this integration can silently break on their
  end. Monitor it; don't treat it as a "set and forget" revenue stream
  the way owning your own contract would be.
- I have not been able to run this against live Solana infrastructure.
  Test thoroughly on devnet (pump.fun may or may not have a devnet
  deployment -- confirm) before pointing this at mainnet with real fees
  attached.
"""

from dataclasses import dataclass
from typing import Optional

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import TransferParams, transfer
from solders.message import Message
from solders.transaction import Transaction
from solana.rpc.api import Client

from anchorpy import Program, Provider, Wallet

# Found via public documentation -- VERIFY against pump.fun's current
# official docs before production use; program addresses and interfaces
# for third-party protocols can change.
PUMPFUN_PROGRAM_ID = Pubkey.from_string("6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P")

LAMPORTS_PER_SOL = 1_000_000_000


@dataclass
class RoutedLaunchResult:
    unsigned_transaction: Transaction
    mint_address: str


async def _load_pumpfun_program(rpc_url: str) -> Program:
    """
    Fetches pump.fun's IDL directly from their on-chain IDL account and
    builds a typed client from it -- more robust than hardcoding
    instruction layouts, and self-correcting if their published IDL
    stays current with their deployed program.
    """
    client = Client(rpc_url)
    provider = Provider(client, Wallet.dummy())  # read-only use here; we're not signing with this wallet
    try:
        return await Program.fetch(PUMPFUN_PROGRAM_ID, provider)
    except Exception as e:
        raise RuntimeError(
            "Could not fetch pump.fun's on-chain IDL. Either the program ID is "
            "stale, they don't publish an on-chain IDL, or the RPC is unreachable. "
            f"Underlying error: {e}"
        ) from e


async def build_routed_launch_tx(
    creator_pubkey: str,
    name: str,
    symbol: str,
    metadata_uri: str,
    platform_fee_lamports: int,
    platform_treasury: str,
    rpc_url: str,
    dev_buy_sol: Optional[float] = None,
) -> RoutedLaunchResult:
    """
    Builds an unsigned transaction that launches a token via pump.fun's
    own `create` instruction, optionally an initial `buy` (dev buy), and
    a SOL transfer to your treasury as your platform fee -- all in one
    atomic transaction for the creator's own wallet to sign.

    `metadata_uri` must point to already-uploaded token metadata (image +
    JSON per the Metaplex standard) -- pump.fun's own site normally
    handles this upload step; your bot needs an equivalent (e.g. pinning
    to IPFS/Arweave) before calling this.
    """
    program = await _load_pumpfun_program(rpc_url)
    client = Client(rpc_url)

    creator = Pubkey.from_string(creator_pubkey)
    treasury = Pubkey.from_string(platform_treasury)
    mint_keypair = Keypair()  # fresh, one-time -- same pattern as solana_launch.py

    # NOTE: pump.fun's `create` instruction requires several derived
    # accounts (bonding_curve PDA, associated_bonding_curve, metadata PDA,
    # global PDA, event_authority, etc.). anchorpy's `.methods` builder
    # (once you have the fetched IDL in hand) can resolve most of these
    # via `.accounts_strict()` or explicit PDA derivation -- the exact
    # calls depend on the IDL anchorpy fetches at runtime, which is why
    # this function fetches it live rather than assuming a fixed shape.
    # Fill in the account list here against the IDL you actually get back
    # -- this is the one part of this file that cannot be finalized
    # without a live fetch, by design.
    create_ix = program.instruction["create"](
        name,
        symbol,
        metadata_uri,
        ctx=_build_create_context(program, creator, mint_keypair.pubkey()),
    )

    instructions = [create_ix]

    if dev_buy_sol:
        buy_ix = program.instruction["buy"](
            int(dev_buy_sol * LAMPORTS_PER_SOL),
            0,  # min tokens out -- set real slippage protection before production use
            ctx=_build_buy_context(program, creator, mint_keypair.pubkey()),
        )
        instructions.append(buy_ix)

    if platform_fee_lamports > 0:
        instructions.append(
            transfer(
                TransferParams(
                    from_pubkey=creator,
                    to_pubkey=treasury,
                    lamports=platform_fee_lamports,
                )
            )
        )

    latest_blockhash = client.get_latest_blockhash().value.blockhash
    message = Message.new_with_blockhash(instructions, creator, latest_blockhash)
    tx = Transaction.new_unsigned(message)
    tx.partial_sign([mint_keypair], latest_blockhash)

    return RoutedLaunchResult(unsigned_transaction=tx, mint_address=str(mint_keypair.pubkey()))


def _build_create_context(program: Program, creator: Pubkey, mint: Pubkey):
    """
    Placeholder for the account context `create` needs. Populate this
    against the IDL `program` actually resolves at runtime -- account
    names/order here are the part of this integration most likely to
    need correction against pump.fun's real, current interface.
    """
    raise NotImplementedError(
        "Fill in pump.fun's `create` account context here once you've "
        "inspected the IDL fetched via _load_pumpfun_program() -- this "
        "cannot be hardcoded reliably without a live reference."
    )


def _build_buy_context(program: Program, creator: Pubkey, mint: Pubkey):
    """Same caveat as _build_create_context -- resolve against the live IDL."""
    raise NotImplementedError(
        "Fill in pump.fun's `buy` account context here once you've "
        "inspected the IDL fetched via _load_pumpfun_program()."
    )
