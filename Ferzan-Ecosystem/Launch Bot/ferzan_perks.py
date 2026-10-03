"""FERZAN holder perks: what holding FERZAN in a Solana wallet gets you on Ferzan.

  Holder  (FERZAN_PERK_HOLDER,  default  1,000,000 = 0.1% of supply) half-price Solana launch, bridge fee 0.15%
  Booster (FERZAN_PERK_BOOSTER, default  5,000,000 = 0.5%)           half-price Solana launch, bridge fee 0.10%
  Whale   (FERZAN_PERK_WHALE,   default 10,000,000 = 1%)             free Solana launch, bridge fee 0.05%
  Titan   (FERZAN_PERK_TITAN,   default 25,000,000 = 2.5%)           free Solana launch, no bridge fee
  Bridge fee for everyone else: BRIDGE_FEE_BPS (default 25 = 0.25%). Trade Bot fee discounts are listed in ladder().

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


def booster_min() -> float:
    """Between holder and whale (default 5,000,000 = 0.5% of supply)."""
    return min(max(holder_min(), _num("FERZAN_PERK_BOOSTER", 5_000_000)), whale_min())


def titan_min() -> float:
    """At or above whale (default 25,000,000 = 2.5% of supply)."""
    return max(whale_min(), _num("FERZAN_PERK_TITAN", 25_000_000))


def base_bridge_bps() -> int:
    """Ferzan's bridge fee for everyone without a holder discount, in basis points (25 = 0.25%)."""
    return int(max(0, min(100, _num("BRIDGE_FEE_BPS", 25))))


# tier, minimum, launch fee off %, badge, bridge fee bps, Trade Bot fee discount %  (highest first)
def _tiers() -> list[tuple]:
    return [
        ("titan", titan_min(), 100, "👑 FERZAN titan", 0, 40),
        ("whale", whale_min(), 100, "🐋 FERZAN whale", 5, 25),
        ("booster", booster_min(), 50, "🔶 FERZAN booster", 10, 15),
        ("holder", holder_min(), 50, "🔷 FERZAN holder", 15, 10),
    ]


def ladder() -> list[dict]:
    """The public tier table, lowest first, for the API and the website."""
    return [{"tier": t, "min": m, "badge": b, "launch_fee_off_pct": off, "bridge_fee_bps": min(bps, base_bridge_bps()),
             "trade_fee_discount_pct": disc} for (t, m, off, b, bps, disc) in reversed(_tiers())]


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
    """{'active', 'tier' (none|holder|booster|whale|titan), 'balance', 'launch_fee_off_pct', 'badge', 'holder_min',
    'whale_min', 'booster_min', 'titan_min', 'bridge_fee_bps', 'trade_fee_discount_pct', 'next_tier', 'next_min'}"""
    owner = (owner or "").strip()
    base = {"active": False, "tier": "none", "balance": 0.0, "launch_fee_off_pct": 0, "badge": "",
            "holder_min": holder_min(), "whale_min": whale_min(), "booster_min": booster_min(), "titan_min": titan_min(),
            "bridge_fee_bps": base_bridge_bps(), "trade_fee_discount_pct": 0, "next_tier": "holder", "next_min": holder_min()}
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
    ups = list(reversed(_tiers()))  # lowest first
    out["next_tier"], out["next_min"] = "", None
    for (t, mn, off, badge, bps, disc) in ups:
        if bal >= mn:
            out.update(tier=t, launch_fee_off_pct=off, badge=badge, bridge_fee_bps=min(bps, base_bridge_bps()),
                       trade_fee_discount_pct=disc)
        elif not out["next_tier"]:
            out["next_tier"], out["next_min"] = t, mn
    _CACHE[owner] = (time.time(), dict(out, _mint=m))
    return out


def fee_exempt(uid) -> bool:
    """Team members listed in FEE_EXEMPT_USER_IDS (comma-separated Telegram ids; same list the Trade Bot uses)."""
    try:
        uid = int(uid or 0)
    except (TypeError, ValueError):
        return False
    if uid <= 0:
        return False
    raw = os.environ.get("FEE_EXEMPT_USER_IDS") or ""
    if not raw.strip():
        try:
            from dotenv import dotenv_values

            raw = dotenv_values(os.environ.get("FERZAN_SHARED_ENV") or "/opt/ferzan/.env").get("FEE_EXEMPT_USER_IDS") or ""
        except Exception:  # noqa: BLE001
            raw = ""
    return str(uid) in {x.strip() for x in raw.replace(";", ",").split(",") if x.strip()}


def launch_fee_lamports(owner: str, base_fee: int, uid=None) -> tuple[int, str]:
    """(fee to charge, note for the cost line). Never raises."""
    if base_fee > 0 and fee_exempt(uid):
        return 0, "Ferzan team: no launch fee"
    try:
        p = perks(owner)
    except Exception:
        return base_fee, ""
    off = int(p.get("launch_fee_off_pct") or 0)
    if base_fee <= 0 or off <= 0:
        return base_fee, ""
    fee = base_fee * (100 - off) // 100
    return fee, f"{p['badge']}: {'no launch fee' if fee == 0 else f'launch fee {off}% off'}"
