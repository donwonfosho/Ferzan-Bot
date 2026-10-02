"""Trust, rank and holder-tier facts for the Trade Bot. Pure helpers: no network except the FERZAN balance read
(inside ferzan_perks), and everything here states only what the code actually does."""
from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

# lifetime live-trading volume (USD) -> title. Highest first.
RANKS = [(1_000_000, "👑", "Factory Boss"), (100_000, "🐋", "Whale"), (10_000, "🎯", "Sniper"),
         (1_000, "⚡", "Trader"), (0, "🌱", "Rookie")]


def rank_for(volume_usd: float) -> dict:
    v = max(0.0, float(volume_usd or 0))
    cur = next(r for r in RANKS if v >= r[0])
    higher = [r for r in RANKS if r[0] > cur[0]]
    nxt = higher[-1] if higher else None  # the closest step up (RANKS is highest first)
    return {"emoji": cur[1], "title": cur[2], "volume": round(v, 2),
            "next_title": nxt[2] if nxt else "", "next_at": nxt[0] if nxt else 0,
            "to_next": round(nxt[0] - v, 2) if nxt else 0.0,
            "pct": 100.0 if not nxt else round(max(0.0, min(100.0, (v - cur[0]) * 100.0 / (nxt[0] - cur[0]))), 1)}


def launch_at() -> int:
    """FERZAN launch time (unix). FERZAN_LAUNCH_AT overrides; default Oct 15, 2026 4:00 PM ET (20:00 UTC)."""
    raw = (os.environ.get("FERZAN_LAUNCH_AT") or "2026-10-15T20:00:00+00:00").strip()
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
    except ValueError:
        return int(datetime(2026, 10, 15, 20, 0, tzinfo=timezone.utc).timestamp())


def countdown(now: float | None = None) -> str:
    left = launch_at() - int(now if now is not None else time.time())
    if left <= 0:
        return "live now"
    d, rem = divmod(left, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return (f"{d}d " if d else "") + f"{h}h {m}m"


def _perks():
    """ferzan_perks lives in the Launch Bot folder of the same repo; absent or failing means no tier (never an error)."""
    try:
        import ferzan_perks as fp  # noqa: PLC0415
        return fp
    except ImportError:
        pass
    try:
        sys.path.append(str(HERE.parent / "Launch Bot"))
        import ferzan_perks as fp  # noqa: PLC0415
        return fp
    except Exception:
        return None


def holder_tier(sol_address: str) -> dict:
    """{'active', 'tier', 'badge', 'balance', 'next_tier', 'next_min', 'ladder': [...]} for display only.
    Before the FERZAN mint is announced 'active' is False and the ladder is a preview of what unlocks."""
    fp = _perks()
    if fp is None:
        return {"active": False, "tier": "none", "ladder": []}
    try:
        out = dict(fp.perks(sol_address))
        out["ladder"] = fp.ladder()
        return out
    except Exception:
        return {"active": False, "tier": "none", "ladder": []}


def security_facts() -> dict:
    """What the desk really does today, read from the live settings. Every line is a fact, not a promise."""
    import evm_signer
    import signer

    evm_on = (os.getenv("EVM_MEV_PROTECT", "1").strip().lower()) not in {"0", "false", "off", "no"}
    private = sorted({cid for cid in evm_signer._MEV_RPCS} | {int(k[12:]) for k in os.environ if k.startswith("EVM_MEV_RPC_") and k[12:].isdigit()})
    names = {1: "Ethereum (Flashbots Protect)", 56: "BNB Chain (PancakeSwap MEV Guard)"}
    return {
        "wallets": "Each Telegram account gets its own trading wallet. Keys are encrypted on our server so the bot can sign your trades. "
                   "You can export them or withdraw everything at any time. This is a custodial hot wallet: keep only trading funds in it.",
        "solana_mev": ("Paused for maintenance: buys use the normal fast route." if signer.anti_mev_paused()
                       else "Solana buys go private through Jito: no sandwiches, and no fee if the bundle fails."),
        "evm_mev": ("Private routing for " + ", ".join(names.get(c, f"chain {c}") for c in private) +
                    ". Other EVM chains use the public mempool." if evm_on else "Off. EVM swaps use the public mempool."),
        "safety": "Every buy card shows a safety line (honeypot, taxes, mint and freeze authority). Honeypots are blocked.",
        "fees": "The fee is shown before you confirm, and again in your history. No hidden spread.",
        "audit": (os.getenv("FERZAN_AUDIT_NOTE") or "No independent audit is published yet. Treat this as early software."),
    }


def security_text(facts: dict | None = None) -> str:
    f = facts or security_facts()
    return ("🔒 <b>Security: how the desk works</b>\n\n"
            f"👛 <b>Wallets</b>\n{f['wallets']}\n\n"
            f"🛡 <b>Solana MEV</b>\n{f['solana_mev']}\n\n"
            f"⛓ <b>EVM MEV</b>\n{f['evm_mev']}\n\n"
            f"🔎 <b>Safety checks</b>\n{f['safety']}\n\n"
            f"💸 <b>Fees</b>\n{f['fees']}\n\n"
            f"📋 <b>Audit</b>\n{f['audit']}")


# ---- holder fee discount (applied in fees.current_bps) -----------------------------------------------
# The FERZAN balance read is a network call, so the fee path NEVER waits for it: it uses the last known
# value and refreshes in a background thread when it is older than _DISC_TTL. Before the mint is announced
# (or if anything fails) the discount is 0, so fees are unchanged until FERZAN is live.
_DISC: dict[int, tuple[float, int]] = {}
_DISC_BUSY: set[int] = set()
_DISC_TTL = 1800


def _refresh_discount(uid: int, sol_address: str) -> None:
    try:
        pct = int(holder_tier(sol_address).get("trade_fee_discount_pct") or 0)
        _DISC[uid] = (time.time(), max(0, min(60, pct)))
    except Exception:
        _DISC[uid] = (time.time(), _DISC.get(uid, (0, 0))[1])
    finally:
        _DISC_BUSY.discard(uid)


def holder_discount_pct(uid: int) -> int:
    """Percent off the trade fee for this user's FERZAN holdings (last known value; 0 if unknown)."""
    hit = _DISC.get(uid)
    if (hit is None or time.time() - hit[0] > _DISC_TTL) and uid not in _DISC_BUSY:
        try:
            import db

            addr = (db.get_user_wallet(uid) or {}).get("sol_pub") or ""
        except Exception:
            addr = ""
        if addr:
            _DISC_BUSY.add(uid)
            threading.Thread(target=_refresh_discount, args=(uid, addr), daemon=True).start()
        else:
            _DISC[uid] = (time.time(), 0)
    return _DISC.get(uid, (0, 0))[1]
