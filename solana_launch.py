"""
solana_launch.py

Builds an unsigned Solana transaction that:
  1. Creates a new SPL token mint account
  2. Initializes it (decimals, mint authority = creator)
  3. Creates the creator's associated token account (ATA)
  4. Mints the initial supply to that ATA
  5. By default, permanently revokes mint AND freeze authority in the
     same transaction -- the SPL equivalent of the EVM contract's "no
     mint function" design: once this transaction lands, supply can
     never be inflated and the creator can never freeze holders' tokens.

NON-CUSTODIAL SIGNING -- this transaction needs TWO signers, and only one
of them touches anything sensitive:
  1. A freshly generated, one-time mint keypair. Solana requires a new
     account to sign its own creation as proof of address ownership.
     This keypair is generated right here, controls nothing else, and
     is safe for the backend to sign with immediately -- it has no
     access to the creator's funds or any other account.
  2. The creator's own wallet, as fee payer and initial mint/freeze
     authority. THIS signature must come from their own wallet
     (Phantom/Solflare via your Mini App) -- never handled here.

CONFIDENCE NOTE: I wasn't able to run this against a live Solana RPC or
even import solana-py/solders in the environment I built this in (no
network access), and those libraries have had real breaking changes
across versions in the past. The instruction sequence and account model
here are correct and standard -- but treat exact function names/import
paths as "verify against your installed version's docs," not "known
correct," the way I could for the web3.py-based EVM code. Test thoroughly
on devnet before mainnet.
"""

import os
from dataclasses import dataclass

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.system_program import CreateAccountParams, TransferParams, create_account, transfer
from solders.message import Message
from solders.transaction import Transaction
from solana.rpc.api import Client

from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    initialize_mint,
    InitializeMintParams,
    create_associated_token_account,
    get_associated_token_address,
    mint_to,
    MintToParams,
    set_authority,
    SetAuthorityParams,
    AuthorityType,
)

MINT_ACCOUNT_SPACE = 82  # fixed size of an SPL Token mint account, per the SPL Token program spec


@dataclass
class SolanaLaunchResult:
    unsigned_transaction: Transaction  # already partially signed by the mint keypair
    mint_address: str
    associated_token_account: str


def build_unsigned_launch_tx(
    creator_pubkey: str,
    decimals: int,
    initial_supply_raw: int,  # already scaled by 10**decimals -- caller's responsibility, not auto-applied here
    rpc_url: str,
    revoke_mint_authority: bool = True,
    revoke_freeze_authority: bool = True,
) -> SolanaLaunchResult:
    client = Client(rpc_url)
    creator = Pubkey.from_string(creator_pubkey)

    mint_keypair = Keypair()  # fresh, one-time, controls nothing but this new mint
    mint_pubkey = mint_keypair.pubkey()

    try:
        rent_lamports = client.get_minimum_balance_for_rent_exemption(
            MINT_ACCOUNT_SPACE
        ).value
    except Exception as e:
        raise ConnectionError(f"Could not reach Solana RPC for rent calculation: {e}") from e

    ata = get_associated_token_address(creator, mint_pubkey)

    instructions = [
        create_account(
            CreateAccountParams(
                from_pubkey=creator,
                to_pubkey=mint_pubkey,
                lamports=rent_lamports,
                space=MINT_ACCOUNT_SPACE,
                owner=TOKEN_PROGRAM_ID,
            )
        ),
        initialize_mint(
            InitializeMintParams(
                program_id=TOKEN_PROGRAM_ID,
                mint=mint_pubkey,
                decimals=decimals,
                mint_authority=creator,
                freeze_authority=creator,  # set now; optionally revoked below in the SAME tx
            )
        ),
        create_associated_token_account(payer=creator, owner=creator, mint=mint_pubkey),
        mint_to(
            MintToParams(
                program_id=TOKEN_PROGRAM_ID,
                mint=mint_pubkey,
                dest=ata,
                mint_authority=creator,
                amount=initial_supply_raw,
            )
        ),
    ]

    treasury = (os.environ.get("PLATFORM_TREASURY_SOL") or os.environ.get("TREASURY_SOL") or "").strip()
    fee_lamports = int(os.environ.get("LAUNCH_FEE_LAMPORTS") or "50000000")
    if treasury and fee_lamports > 0:
        instructions.append(
            transfer(
                TransferParams(
                    from_pubkey=creator,
                    to_pubkey=Pubkey.from_string(treasury),
                    lamports=fee_lamports,
                )
            )
        )

    if revoke_mint_authority:
        instructions.append(
            set_authority(
                SetAuthorityParams(
                    program_id=TOKEN_PROGRAM_ID,
                    account=mint_pubkey,
                    authority=AuthorityType.MintTokens,
                    current_authority=creator,
                    new_authority=None,
                )
            )
        )
    if revoke_freeze_authority:
        instructions.append(
            set_authority(
                SetAuthorityParams(
                    program_id=TOKEN_PROGRAM_ID,
                    account=mint_pubkey,
                    authority=AuthorityType.FreezeAccount,
                    current_authority=creator,
                    new_authority=None,
                )
            )
        )

    try:
        latest_blockhash = client.get_latest_blockhash().value.blockhash
    except Exception as e:
        raise ConnectionError(f"Could not fetch latest blockhash from Solana RPC: {e}") from e

    message = Message.new_with_blockhash(instructions, creator, latest_blockhash)
    tx = Transaction.new_unsigned(message)

    # Partial-sign now with the throwaway mint keypair. The creator's own
    # wallet signature (fee payer + authority) is added afterward via the
    # Mini App -- this function never has access to that key.
    tx.partial_sign([mint_keypair], latest_blockhash)

    return SolanaLaunchResult(
        unsigned_transaction=tx,
        mint_address=str(mint_pubkey),
        associated_token_account=str(ata),
    )
