"""/launchday (admins only): one screen that says what is ready for the FERZAN launch and what is not.

Reads settings by NAME only: it reports "set" / "missing", never a value, so the screen is safe to screenshot.
"""

from __future__ import annotations

import os
import subprocess
import time

import db
import trust

SERVICES = ("ferzan-trade", "ferzan-webapp", "ferzan-launch", "ferzan-launch-api", "ferzan-buy", "ferzan-guardian", "ferzan-liquidity")


def _set(name: str) -> bool:
    return bool((os.getenv(name) or "").strip())


def _on(name: str, default: str = "") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in {"1", "true", "yes", "on"}


def _services() -> dict[str, str]:
    out = {}
    for s in SERVICES:
        try:
            r = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True, timeout=4)
            st = (r.stdout or "").strip() or "unknown"
        except Exception:
            return {}  # not a systemd box (or no permission): skip the section
        if st != "unknown" and st != "inactive":
            out[s] = st
        elif st == "inactive":
            out[s] = st
    return out


def _fee_health(hours: int = 24) -> tuple[int, int]:
    since = int(time.time()) - hours * 3600
    try:
        with db.get_conn() as conn:
            ok = conn.execute("SELECT COUNT(*) FROM fee_ledger WHERE created_at >= ? AND kind IN ('live_buy','live_sell')", (since,)).fetchone()[0]
            bad = conn.execute("SELECT COUNT(*) FROM fee_ledger WHERE created_at >= ? AND kind LIKE '%failed'", (since,)).fetchone()[0]
        return int(ok), int(bad)
    except Exception:
        return -1, -1


def checks() -> list[tuple[str, str, str]]:
    """(state, label, detail) with state in ok / warn / bad."""
    rows: list[tuple[str, str, str]] = []

    def add(ok: bool, label: str, good: str, bad_: str, level: str = "bad") -> None:
        rows.append(("ok" if ok else level, label, good if ok else bad_))

    add(_on("FEE_COLLECT_LIVE"), "Fees on buys", "collecting", "OFF: set FEE_COLLECT_LIVE=1")
    add(_on("FEE_COLLECT_SELLS"), "Fees on sells", "collecting", "OFF: set FEE_COLLECT_SELLS=1", "warn")
    add(_set("FEE_WALLET_SOL"), "Fee wallet (Solana)", "set", "missing: FEE_WALLET_SOL")
    add(_set("FEE_WALLET_EVM"), "Fee wallet (EVM)", "set", "missing: FEE_WALLET_EVM")
    add(_on("FEE_COLLECT_TON") and _set("FEE_WALLET_TON"), "Fees on TON buys", "collecting", "off (optional): FEE_COLLECT_TON + FEE_WALLET_TON", "warn")
    add(_on("FEE_COLLECT_TRON") and _set("FEE_WALLET_TRON"), "Fees on Tron buys", "collecting", "off (optional): FEE_COLLECT_TRON + FEE_WALLET_TRON", "warn")
    add(not _on("FEE_STAKE_LEDGER"), "Self-declared /stake discount", "off (correct)", "ON: only keep if stake_units is verified")
    add(_set("FERZAN_TOKEN_MINT"), "FERZAN token mint", "set: holder tiers can work", "not set: holder discounts stay at 0 until announced", "warn")
    add(_set("TRONGRID_API_KEY"), "TronGrid key", "set", "missing: Tron trades may be rate-limited", "warn")
    add(_on("EVM_MEV_PROTECT", "1"), "EVM private routing", "on (Ethereum and BNB)", "OFF")
    add(_set("FERZAN_ADMIN_IDS"), "Admin alerts", "set", "missing: FERZAN_ADMIN_IDS")
    ok, bad = _fee_health()
    if ok >= 0:
        if ok + bad == 0:
            rows.append(("warn", "Fee ledger, last 24h", "no fee rows yet: do the $20 buy and sell test"))
        else:
            rows.append(("ok" if bad == 0 else "warn", "Fee ledger, last 24h", f"{ok} collected, {bad} failed"))
    for name, st in _services().items():
        rows.append(("ok" if st == "active" else "bad", f"Service {name}", st))
    return rows


def report() -> str:
    rows = checks()
    icon = {"ok": "✅", "warn": "🟡", "bad": "🔴"}
    lines = [f"🚀 <b>Launch-day check</b>  ·  FERZAN in <b>{trust.countdown()}</b>\n"]
    lines += [f"{icon[s]} {label}: {detail}" for s, label, detail in rows]
    bad = sum(1 for s, *_ in rows if s == "bad")
    warn = sum(1 for s, *_ in rows if s == "warn")
    lines.append("\n" + ("🟢 <b>All clear.</b>" if not bad and not warn else f"<b>{bad} must-fix, {warn} to review.</b>"))
    lines.append("<i>Still manual: gas tanks funded, promo cap raised, X handles checked, rehearsal done.</i>")
    return "\n".join(lines)
