"""
Unsigned TRC-20 launch via a Tron-deployed LaunchTokenFactory.

Same Solidity factory as EVM. Deploy it with TronBox / Hardhat-Tron, then
set FACTORY_TRX_PLAIN. The Mini App signs the trigger with TronLink.
A launch fee in SUN is attached as a second TransferContract to
PLATFORM_TREASURY_TRX when that env is set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import requests

TRONGRID = (os.environ.get("TRONGRID_URL") or "https://api.trongrid.io").rstrip("/")


@dataclass
class TronLaunchTx:
    trigger: dict
    fee_transfer: dict | None
    factory: str
    note: str


def _headers() -> dict:
    key = (os.environ.get("TRONGRID_API_KEY") or "").strip()
    h = {"Content-Type": "application/json"}
    if key:
        h["TRON-PRO-API-KEY"] = key
    return h


def build_unsigned_launch_tx(
    creator_address: str,
    name: str,
    symbol: str,
    total_supply: int,
    project_url: str = "",
) -> TronLaunchTx:
    factory = (os.environ.get("FACTORY_TRX_PLAIN") or "").strip()
    if not factory:
        raise ValueError(
            "FACTORY_TRX_PLAIN is empty. Deploy LaunchTokenFactory on Tron "
            "and set that address before a live TRC-20 launch."
        )
    body = {
        "owner_address": creator_address,
        "contract_address": factory,
        "function_selector": "launchToken(string,string,uint256,string)",
        "parameter": "",
        "fee_limit": 150_000_000,
        "call_value": int(os.environ.get("LAUNCH_FEE_SUN") or "0"),
        "visible": True,
    }
    # TronGrid can encode parameters if we pass parameter as hex ABI.
    # Keep names in extra so the Mini App can encode with TronWeb.
    extra = {
        "name": name,
        "symbol": symbol,
        "total_supply": str(total_supply),
        "project_url": project_url,
    }
    try:
        r = requests.post(
            f"{TRONGRID}/wallet/triggersmartcontract",
            json=body,
            headers=_headers(),
            timeout=20,
        )
        trigger = r.json()
    except Exception as exc:
        trigger = {"error": str(exc), "params": extra}
    else:
        trigger["params"] = extra

    treasury = (os.environ.get("PLATFORM_TREASURY_TRX") or "").strip()
    fee = int(os.environ.get("LAUNCH_FEE_SUN") or "0")
    fee_transfer = None
    if treasury and fee > 0:
        fee_transfer = {
            "to_address": treasury,
            "owner_address": creator_address,
            "amount": fee,
            "visible": True,
        }
    return TronLaunchTx(
        trigger=trigger,
        fee_transfer=fee_transfer,
        factory=factory,
        note="Sign the factory trigger in TronLink. Fee goes to PLATFORM_TREASURY_TRX.",
    )
