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
from solders.instruction import Instruction, AccountMeta
from solders.sysvar import RENT as SYSVAR_RENT_PUBKEY
from solders.system_program import ID as SYS_PROGRAM_ID
import asyncio
from solana.rpc.async_api import AsyncClient

from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import (
    initialize_mint,
    create_associated_token_account,
    get_associated_token_address,
    mint_to,
    set_authority,
)
from spl.token.models import (
    InitializeMintParams,
    MintToParams,
    SetAuthorityParams,
    AuthorityType,
)

MINT_ACCOUNT_SPACE = 82  # fixed size of an SPL Token mint account, per the SPL Token program spec

METAPLEX_PROGRAM_ID = Pubkey.from_string("metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s")

def _borsh_string(s: str) -> bytes:
    b = s.encode("utf-8")
    return len(b).to_bytes(4, "little") + b

def _create_metadata_instruction(mint_pubkey, mint_authority, payer, name, symbol, uri):
    metadata_pda, _ = Pubkey.find_program_address(
        [b"metadata", bytes(METAPLEX_PROGRAM_ID), bytes(mint_pubkey)],
        METAPLEX_PROGRAM_ID,
    )
    data = bytes([33])  # CreateMetadataAccountV3 discriminator
    data += _borsh_string(name[:32])
    data += _borsh_string(symbol[:10])
    data += _borsh_string(uri[:200])
    data += (0).to_bytes(2, "little")  # seller_fee_basis_points
    data += bytes([0])  # creators: None
    data += bytes([0])  # collection: None
    data += bytes([0])  # uses: None
    data += bytes([1])  # is_mutable: True
    data += bytes([0])  # collection_details: None

    accounts = [
        AccountMeta(pubkey=metadata_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
        AccountMeta(pubkey=mint_authority, is_signer=True, is_writable=False),
        AccountMeta(pubkey=payer, is_signer=True, is_writable=True),
        AccountMeta(pubkey=mint_authority, is_signer=True, is_writable=False),
        AccountMeta(pubkey=SYS_PROGRAM_ID, is_signer=False, is_writable=False),
        AccountMeta(pubkey=SYSVAR_RENT_PUBKEY, is_signer=False, is_writable=False),
    ]
    return Instruction(program_id=METAPLEX_PROGRAM_ID, accounts=accounts, data=data)

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
    name: str = "",
    symbol: str = "",
    metadata_uri: str = "",
    revoke_mint_authority: bool = True,
    revoke_freeze_authority: bool = True,
) -> SolanaLaunchResult:
    creator = Pubkey.from_string(creator_pubkey)

    mint_keypair = Keypair()  # fresh, one-time, controls nothing but this new mint
    mint_pubkey = mint_keypair.pubkey()

    async def _fetch_rpc_data():
        async with AsyncClient(rpc_url) as client:
            rent_resp = await client.get_minimum_balance_for_rent_exemption(MINT_ACCOUNT_SPACE)
            blockhash_resp = await client.get_latest_blockhash()
        return rent_resp.value, blockhash_resp.value.blockhash

    try:
        rent_lamports, latest_blockhash = asyncio.run(_fetch_rpc_data())
    except Exception as e:
        raise ConnectionError(f"Could not reach Solana RPC: {e}") from e

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
    if metadata_uri:
        instructions.append(
            _create_metadata_instruction(mint_pubkey, creator, creator, name, symbol, metadata_uri)
        )

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
                    authority=AuthorityType.MINT_TOKENS,
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
                    authority=AuthorityType.FREEZE_ACCOUNT,
                    current_authority=creator,
                    new_authority=None,
                )
            )
        )

    # latest_blockhash already fetched above

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
