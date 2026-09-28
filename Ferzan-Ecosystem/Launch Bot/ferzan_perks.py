"""FERZAN holder perks: what holding FERZAN in a Solana wallet gets you on Ferzan.

  Holder  (FERZAN_PERK_HOLDER, default 1,000,000 FERZAN = 0.1% of supply)
          -> FERZAN holder badge, half-price Solana launch fee
  Whale   (FERZAN_PERK_WHALE, default 10,000,000 FERZAN = 1% of supply)
          -> whale badge, free Solana launch fee

The perks switch on by themselves when FERZAN is announced (the mint is read from the flagship
state file, and stays private until then). FERZAN_PERK_MINT in /opt/ferzan/.env overrides the mint
(for testing with another token); FERZAN_PERKS_OFF=1 turns everything off.

Fails soft: if the balance can't be read, the wallet simply gets no perk, and a launch is never blocked.
Read-only: it only reads token balances.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests

FLAGSHIP_STATE = Path("/opt/ferzan/dbc-keys/ferzan-flagship-state.json")
DECIMALS = 6
_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 300
_SOL = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def _num(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key) or default)
    except ValueError:
        return default


def holder_min() -> float:
    return _num("FERZAN_PERK_HOLDER", 1_000_000)


def whale_min() -> float:
    return max(holder_min(), _num("FERZAN_PERK_WHALE", 10_000_000))


def mint() -> str:
    """The FERZAN mint once it is announced ('' before that)."""
    if os.environ.get("FERZAN_PERKS_OFF") == "1":
        return ""
    override = (os.environ.get("FERZAN_PERK_MINT") or "").strip()
    if override:
        return override if _SOL.fullmatch(override) else ""
    try:
        st = json.loads(FLAGSHIP_STATE.read_text())
    except Exception:
        return ""
    m = str(st.get("mint") or "") if st.get("announced") else ""
    return m if _SOL.fullmatch(m) else ""


def _rpc() -> str:
    return os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"


def balance(owner: str, mint_addr: str) -> float | None:
    """Whole FERZAN held by owner across all its token accounts, or None if the RPC failed."""
    try:
        r = requests.post(_rpc(), json={
            "jsonrpc": "2.0", "id": 1, "method": "getTokenAccountsByOwner",
            "params": [owner, {"mint": mint_addr}, {"encoding": "jsonParsed", "commitment": "confirmed"}]}, timeout=8).json()
        if "error" in r:
            return None
        total = 0
        for acc in (r.get("result") or {}).get("value") or []:
            info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            total += int((info.get("tokenAmount") or {}).get("amount") or 0)
        return total / 10 ** DECIMALS
    except Exception:
        return None


def perks(owner: str) -> dict:
    """{'active', 'tier' (none|holder|whale), 'balance', 'launch_fee_off_pct', 'badge', 'holder_min', 'whale_min'}"""
    owner = (owner or "").strip()
    base = {"active": False, "tier": "none", "balance": 0.0, "launch_fee_off_pct": 0, "badge": "",
            "holder_min": holder_min(), "whale_min": whale_min()}
    m = mint()
    if not m or not _SOL.fullmatch(owner):
        return base
    hit = _CACHE.get(owner)
    if hit and time.time() - hit[0] < _TTL and hit[1].get("_mint") == m:
        return {k: v for k, v in hit[1].items() if not k.startswith("_")}
    bal = balance(owner, m)
    out = dict(base, active=True)
    if bal is None:
        out["error"] = "balance unavailable"
        return out  # not cached: try again next time
    out["balance"] = bal
    if bal >= whale_min():
        out.update(tier="whale", launch_fee_off_pct=100, badge="🐋 FERZAN whale")
    elif bal >= holder_min():
        out.update(tier="holder", launch_fee_off_pct=50, badge="🔷 FERZAN holder")
    _CACHE[owner] = (time.time(), dict(out, _mint=m))
    return out


def launch_fee_lamports(owner: str, base_fee: int) -> tuple[int, str]:
    """(fee to charge, note for the cost line). Never raises."""
    try:
        p = perks(owner)
    except Exception:
        return base_fee, ""
    off = int(p.get("launch_fee_off_pct") or 0)
    if base_fee <= 0 or off <= 0:
        return base_fee, ""
    fee = base_fee * (100 - off) // 100
    return fee, f"{p['badge']}: {'no launch fee' if fee == 0 else f'launch fee {off}% off'}"
