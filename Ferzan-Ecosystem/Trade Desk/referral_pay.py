"""Automatic referral payouts (OFF unless REFERRAL_AUTOPAY=1).

A /claim that passes every check is paid straight from a dedicated payout wallet (REFERRAL_PAYOUT_SECRET) to the
user's Ferzan SOL wallet. Anything that does not pass falls back to the old flow: the claim stays 'claimed' and the
admins get the "Mark paid" message. The code never retries a send, and never marks a claim paid unless the transfer
confirmed on-chain.

Limits (env, all optional):
  REFERRAL_AUTOPAY_MAX_USD     biggest single payout paid automatically   (default 50)
  REFERRAL_AUTOPAY_DAILY_USD   total paid automatically per 24 hours       (default 200)
  REFERRAL_AUTOPAY_RESERVE_SOL SOL always left in the payout wallet        (default 0.02)
"""

from __future__ import annotations

import os
import time

import db

LAMPORTS = 1_000_000_000


def _flag(name: str) -> bool:
    return (os.getenv(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except ValueError:
        return default


def enabled() -> bool:
    return _flag("REFERRAL_AUTOPAY") and bool((os.getenv("REFERRAL_PAYOUT_SECRET") or "").strip())


def _ensure(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS referral_payouts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            usd REAL NOT NULL,
            lamports INTEGER NOT NULL,
            dest TEXT NOT NULL,
            status TEXT NOT NULL,          -- sending / paid / failed / unsure
            info TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )


def paid_since(seconds: int = 86400, uid: int | None = None) -> float:
    """USD sent or possibly sent (sending/paid/unsure) in the window. Unsure counts: it may have gone out."""
    cutoff = int(time.time()) - seconds
    with db.get_conn() as conn:
        _ensure(conn)
        q = "SELECT COALESCE(SUM(usd),0) FROM referral_payouts WHERE created_at >= ? AND status IN ('sending','paid','unsure')"
        args: list = [cutoff]
        if uid is not None:
            q += " AND user_id = ?"
            args.append(int(uid))
        return float(conn.execute(q, args).fetchone()[0])


def _record(uid: int, usd: float, lamports: int, dest: str, status: str, info: str = "") -> int:
    with db.get_conn() as conn:
        _ensure(conn)
        cur = conn.execute(
            "INSERT INTO referral_payouts (user_id, usd, lamports, dest, status, info, created_at) VALUES (?,?,?,?,?,?,?)",
            (int(uid), float(usd), int(lamports), dest, status, info[:300], int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)


def _finish(row_id: int, status: str, info: str) -> None:
    with db.get_conn() as conn:
        conn.execute("UPDATE referral_payouts SET status = ?, info = ? WHERE id = ?", (status, info[:300], row_id))
        conn.commit()


def try_pay(uid: int, usd: float, dest: str) -> tuple[str, str]:
    """('paid'|'declined'|'unsure', text). 'declined' means nothing was sent and the manual flow should take over."""
    import withdraw
    from price_fetcher import get_price_usd

    if not enabled():
        return "declined", "auto-pay is off"
    import feecollect

    if not feecollect.enabled():  # shares also accrue when no fee is being collected: never pay real money for those
        return "declined", "fee collection is off, so there is no real fee behind these earnings"
    ok, norm = withdraw.validate_address("sol", dest)
    if not ok:
        return "declined", "no valid SOL wallet on file"
    dest = norm
    if usd > _num("REFERRAL_AUTOPAY_MAX_USD", 50):
        return "declined", f"over the automatic limit (${_num('REFERRAL_AUTOPAY_MAX_USD', 50):.0f})"
    if paid_since(86400) + usd > _num("REFERRAL_AUTOPAY_DAILY_USD", 200):
        return "declined", "daily automatic limit reached"
    if paid_since(86400, uid) > 0:
        return "declined", "already paid to this user in the last 24 hours"
    try:
        px = float(get_price_usd("solana") or 0)
    except Exception:  # noqa: BLE001
        px = 0.0
    if px <= 0:
        return "declined", "no live SOL price"
    lamports = int(usd / px * LAMPORTS)
    if lamports < 10_000:
        return "declined", "amount too small to send"
    secret = (os.getenv("REFERRAL_PAYOUT_SECRET") or "").strip()
    try:
        import signer

        me = str(signer.keypair_from_secret(secret).pubkey())
        bal = withdraw.sol_balance(me)
    except Exception as exc:  # noqa: BLE001
        return "declined", f"payout wallet unreadable ({type(exc).__name__})"
    if bal - lamports < int(_num("REFERRAL_AUTOPAY_RESERVE_SOL", 0.02) * LAMPORTS):
        return "declined", "payout wallet is low"
    row = _record(uid, usd, lamports, dest, "sending")
    try:
        sent, info = withdraw.send_sol(secret, dest, lamports)
    except Exception as exc:  # noqa: BLE001  - we cannot know whether it left
        _finish(row, "unsure", f"{type(exc).__name__}: {exc}")
        return "unsure", f"{type(exc).__name__}"
    if sent is True:
        _finish(row, "paid", info)
        return "paid", info
    if sent is False:
        _finish(row, "failed", info)
        return "declined", "transfer did not go through"
    _finish(row, "unsure", info)
    return "unsure", info
