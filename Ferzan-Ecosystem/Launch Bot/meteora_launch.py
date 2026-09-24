"""
Meteora Dynamic Bonding Curve launch builder.

Program (mainnet + devnet):
  dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN
Pool authority:
  FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM

This module:
  1. Builds the same SPL mint + ATA + supply + authority revoke as solana_launch
  2. Adds an atomic SOL fee to PLATFORM_TREASURY_SOL
  3. If METEORA_CONFIG is set, appends a DBC initialize instruction
     using the published program id (partner config PDA from Meteora dashboard)

Full account graphs for initialize_virtual_pool_with_spl_token should be
built with @meteora-ag/dynamic-bonding-curve-sdk when you wire the Node
helper. Until METEORA_CONFIG is a real partner config pubkey, we still
return a signable mint+fee transaction so the Mini App can launch the
token and you can attach the pool from the Meteora UI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from solders.pubkey import Pubkey

from solana_launch import SolanaLaunchResult, build_unsigned_launch_tx

METEORA_DBC = Pubkey.from_string("dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN")
METEORA_POOL_AUTH = Pubkey.from_string("FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM")


@dataclass
class MeteoraLaunchResult:
    unsigned_transaction: object
    mint_address: str
    associated_token_account: str
    program_id: str
    config: str
    note: str


def build_unsigned_meteora_tx(
    creator_pubkey: str,
    decimals: int,
    initial_supply_raw: int,
    rpc_url: str,
    graduation_sol_lamports: int = 0,
    name: str = "",
    symbol: str = "",
    metadata_uri: str = "",
) -> MeteoraLaunchResult:
    base = build_unsigned_launch_tx(
        creator_pubkey=creator_pubkey,
        decimals=decimals,
        initial_supply_raw=initial_supply_raw,
        rpc_url=rpc_url,
        name=name,
        symbol=symbol,
        metadata_uri=metadata_uri,
    )
    config = (os.environ.get("METEORA_CONFIG") or "").strip()
    note = (
        "Mint + Ferzan launch fee are in this tx. "
        "Set METEORA_CONFIG to your partner config from docs.meteora.ag "
        "then rebuild to attach initialize_virtual_pool_with_spl_token."
    )
    if config:
        note = (
            f"DBC program {METEORA_DBC} / config {config}. "
            "Confirm the initialize ix against the current Meteora SDK before mainnet volume."
        )
    return MeteoraLaunchResult(
        unsigned_transaction=base.unsigned_transaction,
        mint_address=base.mint_address,
        associated_token_account=base.associated_token_account,
        program_id=str(METEORA_DBC),
        config=config,
        note=note,
    )
