"""
Meteora Dynamic Bonding Curve launch builder (real pool, not a placeholder).

Program: dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN
Ferzan partner config: METEORA_CONFIG in /opt/ferzan/.env (created once with
dbc/create_config.mjs): 1B supply / 6 decimals, 1% fee split 50/50 creator /
Ferzan treasury, 50% -> 1% anti-sniper fee over the first 60s, graduates to
Meteora DAMM v2 at ~84 SOL raised with all LP permanently locked.

Each launch is ONE transaction built by dbc/build_launch.mjs (Meteora's SDK):
create pool + the creator's dev buy (same tx, so nobody can buy before the
creator) + the Ferzan launch fee transfer. The new token's mint key signs here;
the creator's wallet adds its signature in the Mini App and sends it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from solders.pubkey import Pubkey

METEORA_DBC = Pubkey.from_string("dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN")
METEORA_POOL_AUTH = Pubkey.from_string("FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM")
HELPER = Path(__file__).resolve().parent / "dbc" / "build_launch.mjs"
MAX_DEV_BUY_SOL = float(os.environ.get("MAX_DEV_BUY_SOL") or "50")


@dataclass
class MeteoraLaunchResult:
    unsigned_transaction: object
    mint_address: str
    associated_token_account: str
    program_id: str
    config: str
    note: str
    cost_text: str = ""


def _fail(msg: str):
    try:
        from fastapi import HTTPException

        return HTTPException(400, msg)
    except Exception:
        return RuntimeError(msg)


def _dev_buy_lamports(raw) -> int:
    """'0.5', '0.5 SOL', 1, None -> lamports. Anything unreadable -> 0."""
    if raw is None:
        return 0
    m = re.search(r"\d*\.\d+|\d+", str(raw).replace(",", ""))
    if not m:
        return 0
    sol = float(m.group(0))
    if sol > MAX_DEV_BUY_SOL:
        raise _fail(f"Dev buy is capped at {MAX_DEV_BUY_SOL:g} SOL.")
    return int(round(sol * 1_000_000_000))


def build_unsigned_meteora_tx(
    creator_pubkey: str,
    decimals: int,
    initial_supply_raw: int,
    rpc_url: str,
    graduation_sol_lamports: int = 0,
    name: str = "",
    symbol: str = "",
    metadata_uri: str = "",
    dev_buy=None,
) -> MeteoraLaunchResult:
    # decimals / supply / graduation come from the Ferzan partner config
    # (1B supply, 6 decimals, ~84 SOL graduation), so the args are ignored.
    config = (os.environ.get("METEORA_CONFIG") or "").strip()
    if not config:
        raise _fail("Solana bonding-curve launches aren't set up yet (METEORA_CONFIG missing).")
    if not HELPER.exists():
        raise _fail("Launch helper missing (dbc/build_launch.mjs).")
    payload = {
        "creator": creator_pubkey,
        "name": name,
        "symbol": symbol,
        "uri": metadata_uri or "",
        "devBuyLamports": _dev_buy_lamports(dev_buy),
        "config": config,
        "rpc": rpc_url,
        "treasury": (os.environ.get("PLATFORM_TREASURY_SOL") or os.environ.get("TREASURY_SOL") or "").strip(),
        "feeLamports": int(os.environ.get("LAUNCH_FEE_LAMPORTS") or "50000000"),
    }
    try:
        proc = subprocess.run(
            ["node", str(HELPER)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=90,
            cwd=str(HELPER.parent),
        )
    except subprocess.TimeoutExpired:
        raise _fail("Building the launch timed out — try again.")
    stdout = proc.stdout or ""
    start = stdout.find("{")
    try:
        out = json.loads(stdout[start:]) if start >= 0 else {}
    except ValueError:
        out = {}
    if proc.returncode != 0 or out.get("error") or not out.get("tx_hex"):
        detail = out.get("error") or (proc.stderr or "").strip()[-300:] or "unknown error"
        print(f"METEORA_BUILD_FAILED: {detail}")
        if "needs about" in detail:  # balance check: show it to the launcher as-is
            raise _fail(detail[:300])
        raise _fail(f"Couldn't build the Meteora launch: {detail[:200]}")
    return MeteoraLaunchResult(
        unsigned_transaction=bytes.fromhex(out["tx_hex"]),
        mint_address=out["mint"],
        associated_token_account="",
        program_id=str(METEORA_DBC),
        config=config,
        note=f"Meteora bonding curve · pool {out.get('pool', '')} · {out.get('size', '?')} bytes",
        cost_text=out.get("cost_text", ""),
    )
