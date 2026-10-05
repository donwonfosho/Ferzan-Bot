"""Collect the Ferzan trading fee on LIVE buys (and, with FEE_COLLECT_SELLS=1, sells).

After a live buy lands, send the fee as one small native-coin transfer from
the user's own wallet to the Ferzan fee wallet (FEE_WALLET_SOL / FEE_WALLET_EVM).
The fee is on top of the trade size. It never touches the trade itself: if
the transfer fails the trade stands, nothing is retried, and the failure is
logged in the fee ledger so it can be looked at.

Off until FEE_COLLECT_LIVE=1 is set on the droplet. While it is off nothing
changes: no transfer, and referral shares are credited the way they always were.
"""

from __future__ import annotations

import logging
import os

import db
import fees

log = logging.getLogger("ferzan_feecollect")

MIN_FEE_USD = 0.05  # below this the transfer costs more than it earns
KIND_EXTRA_BPS = {"manual": 0, "snipe": 50, "auto": 50}  # snipes and auto orders: +0.50% (1.00% at the default)


def enabled() -> bool:
    return (os.getenv("FEE_COLLECT_LIVE") or "").strip().lower() in {"1", "true", "yes", "on"}


def sells_enabled() -> bool:
    """Fee on sells has its own switch (FEE_COLLECT_SELLS=1) on top of FEE_COLLECT_LIVE, so buys can run alone."""
    return enabled() and (os.getenv("FEE_COLLECT_SELLS") or "").strip().lower() in {"1", "true", "yes", "on"}


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def exempt(user_id: int) -> bool:
    """Operators listed in FEE_EXEMPT_USER_IDS (comma-separated Telegram ids) trade with no fee."""
    raw = os.getenv("FEE_EXEMPT_USER_IDS") or ""
    if not raw.strip():  # the shared file is where the Launch Bot reads the same list from
        try:
            from dotenv import dotenv_values

            raw = dotenv_values(os.getenv("FERZAN_SHARED_ENV") or "/opt/ferzan/.env").get("FEE_EXEMPT_USER_IDS") or ""
        except Exception:  # noqa: BLE001
            raw = ""
    return str(int(user_id)) in {x.strip() for x in raw.replace(";", ",").split(",") if x.strip()}


def live_bps(user_id: int, kind: str = "manual") -> int:
    """Manual trades pay the normal (volume-discounted) rate; snipes and auto orders pay 0.5% more, capped at 1%."""
    return max(0, min(fees.MAX_FEE_BPS, fees.current_bps(user_id) + KIND_EXTRA_BPS.get(kind, 0)))


def _native_usd(coingecko_id: str) -> float:
    from price_fetcher import get_price_usd

    return float(get_price_usd(coingecko_id) or 0)


def skim_buy(
    uid: int,
    usd: float,
    family: str,
    *,
    sol_secret: str = "",
    evm_secret: str = "",
    evm_chain: str = "base",
    kind: str = "manual",
    note: str = "",
    side: str = "buy",
) -> tuple[bool, str]:
    """Returns (collected, line for the trade message). collected=False means no fee was taken.
    side="sell": usd is the estimated value sold; the fee comes out of the native coin the sale just paid."""
    if not enabled() or exempt(uid) or (side == "sell" and not sells_enabled()):
        return False, ""
    bps = live_bps(uid, kind)
    fee_usd = round(float(usd) * bps / 10_000.0, 6)
    if fee_usd < MIN_FEE_USD:
        return False, ""
    pct = bps / 100.0
    try:
        import withdraw

        if family == "sol":
            dest = fees.fee_wallets()["sol"]
            if not dest or not sol_secret:
                return False, ""
            import signer

            lamports = int(fee_usd / signer.sol_usd() * 1_000_000_000)
            ok, _res = withdraw.send_sol(sol_secret, dest, lamports)
        elif family == "evm":
            dest = fees.fee_wallets()["evm"]
            if not dest or not evm_secret:
                return False, ""
            cg = {"bsc": "binancecoin", "avax": "avalanche-2"}.get(evm_chain, "ethereum")
            px = _native_usd(cg)
            if px <= 0:
                return False, ""
            ok, _res = withdraw.send_evm_native(evm_secret, evm_chain, dest, int(fee_usd / px * 1e18))
        elif family == "ton":
            # Off unless FEE_COLLECT_TON=1 and FEE_WALLET_TON is set (sells too, with FEE_COLLECT_SELLS=1).
            dest = fees.fee_wallets()["ton"]
            if not _flag("FEE_COLLECT_TON") or not dest or not sol_secret:
                return False, ""
            px = _native_usd("the-open-network")
            if px <= 0:
                return False, ""
            ok, _res = withdraw.send_ton(sol_secret, dest, int(fee_usd / px * 1e9))
        elif family == "trx":
            # Off unless FEE_COLLECT_TRON=1 and FEE_WALLET_TRON is set (sells too, with FEE_COLLECT_SELLS=1).
            dest = fees.fee_wallets()["trx"]
            if not _flag("FEE_COLLECT_TRON") or not dest or not evm_secret:
                return False, ""
            px = _native_usd("tron")
            if px <= 0:
                return False, ""
            ok, _res = withdraw.send_trx(evm_secret, dest, int(fee_usd / px * 1e6))
        else:
            return False, ""
    except Exception as exc:
        log.warning("fee transfer errored for %s: %s", uid, str(exc)[:160])
        ok = False
    try:
        if ok is True:
            led_kind, led_fee, led_note = f"live_{side}", fee_usd, f"{kind} {note}".strip()
        elif ok is None:  # sent but not proven: it may have landed, so it is not logged as a plain failure
            led_kind, led_fee, led_note = f"live_{side}_unconfirmed", 0.0, f"{kind} ${fee_usd:.4f} unconfirmed {note}".strip()
        else:
            led_kind, led_fee, led_note = f"live_{side}_failed", 0.0, f"{kind} {note}".strip()
        db.add_fee(uid, led_kind, float(usd), bps, led_fee, note=led_note[:80])
    except Exception:
        log.exception("fee ledger write failed")
    if ok is True:
        return True, f"💸 Fee {pct:.2f}% (${fee_usd:.2f}) to Ferzan"
    return False, ""
