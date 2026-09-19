"""
TON launch request + treasury fee memo.

A full jetton minter deploy needs the official minter code + Tonkeeper
signing in the Mini App. Until that minter is pinned, we:

  1. Record the launch request (name / symbol / supply)
  2. Build a TON transfer to PLATFORM_TREASURY_TON with comment
     FERZAN_LAUNCH:<request_id>
  3. Return that payload for Tonkeeper

Do not advertise this as an on-chain jetton until TON_MINTER_CODE is set.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class TonLaunchTx:
    to: str
    amount_nano: int
    comment: str
    note: str


def build_unsigned_launch_tx(request_id: str, creator_address: str = "") -> TonLaunchTx:
    treasury = (os.environ.get("PLATFORM_TREASURY_TON") or "").strip()
    if not treasury:
        raise ValueError("Set PLATFORM_TREASURY_TON before a TON launch fee.")
    amount = int(os.environ.get("LAUNCH_FEE_NANOTON") or "100000000")
    minter = (os.environ.get("TON_MINTER_CODE") or "").strip()
    note = (
        "Fee memo only. Jetton minter not pinned — TON_MINTER_CODE empty."
        if not minter
        else f"Minter code set ({minter[:12]}…). Attach Tonkeeper deploy next."
    )
    return TonLaunchTx(
        to=treasury,
        amount_nano=amount,
        comment=f"FERZAN_LAUNCH:{request_id}",
        note=note,
    )
