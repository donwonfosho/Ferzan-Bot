"""/launchday for the Launch Bot (admins only): one screen that says what is ready for launch day and what is not.

Settings are read by NAME only: it prints "set" / "missing", never a value, so the screen is safe to screenshot.
"""

from __future__ import annotations

import calendar
import os
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

import requests

DEFAULT_LAUNCH_AT = calendar.timegm((2026, 10, 15, 20, 0, 0))  # Thu Oct 15 2026, 4:00 PM ET
SERVICES = ("ferzan-launch", "ferzan-launch-api", "ferzan-curve-indexer", "ferzan-trade", "ferzan-webapp",
            "ferzan-trade-api", "ferzan-buy", "ferzan-guardian", "ferzan-liq")
JOBS = ("ferzan-refill", "ferzan-watchdog", "ferzan-flywheel", "ferzan-offsite", "ferzan-backup", "ferzan-health", "ferzan-promo")
TIMERS = ("ferzan-refill.timer", "ferzan-watchdog.timer", "ferzan-promo.timer", "ferzan-health.timer", "ferzan-flagship.timer")


def launch_at() -> int:
    raw = _get("FERZAN_LAUNCH_AT")
    if raw:
        try:
            return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
        except ValueError:
            pass
    return DEFAULT_LAUNCH_AT


def countdown(now: float | None = None) -> str:
    left = launch_at() - int(now if now is not None else time.time())
    if left <= 0:
        return "live now"
    d, rem = divmod(left, 86400)
    h, rem = divmod(rem, 3600)
    return (f"{d}d " if d else "") + f"{h}h {rem // 60}m"


_SHARED: dict | None = None


def _get(name: str) -> str:
    """The bot's own environment first, then the shared /opt/ferzan/.env that the timer jobs read."""
    global _SHARED
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    if _SHARED is None:
        try:
            from dotenv import dotenv_values

            _SHARED = {k: (x or "") for k, x in dotenv_values(os.environ.get("FERZAN_SHARED_ENV") or "/opt/ferzan/.env").items()}
        except Exception:  # noqa: BLE001
            _SHARED = {}
    return (_SHARED.get(name) or "").strip()


def _set(*names: str) -> bool:
    return any(_get(n) for n in names)


def _on(name: str) -> bool:
    return _get(name).lower() in {"1", "true", "yes", "on"}


def _state(unit: str) -> str:
    try:
        r = subprocess.run(["systemctl", "is-active", unit], capture_output=True, text=True, timeout=4)
        return (r.stdout or "").strip() or "unknown"
    except Exception:  # noqa: BLE001
        return ""  # not a systemd box (or no permission): skip the section


def _timer_next(unit: str) -> int | None:
    try:
        r = subprocess.run(["systemctl", "show", unit, "-p", "NextElapseUSecRealtime", "--value"],
                           capture_output=True, text=True, timeout=4)
        txt = (r.stdout or "").strip()
        if not txt or txt == "n/a":
            return None
        d = subprocess.run(["date", "-d", txt, "+%s"], capture_output=True, text=True, timeout=4)
        return int((d.stdout or "").strip())
    except Exception:  # noqa: BLE001
        return None


def _launch_counts(db_path: str) -> tuple[dict, int] | None:
    """({status: count} over the last 24h, count stuck pending/built/submitted for over 30 min), or None."""
    try:
        c = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        try:
            by = dict(c.execute("SELECT status, COUNT(*) FROM launch_requests WHERE datetime(created_at) >= datetime('now','-1 day') GROUP BY status"))
            stuck = c.execute("SELECT COUNT(*) FROM launch_requests WHERE status IN ('pending','built','submitted') "
                              "AND datetime(created_at) < datetime('now','-30 minutes') "
                              "AND datetime(created_at) >= datetime('now','-1 day')").fetchone()[0]
        finally:
            c.close()
        return by, int(stuck)
    except Exception:  # noqa: BLE001
        return None


def _http_ok(url: str) -> bool | None:
    try:
        return requests.get(url, timeout=4).status_code < 400
    except Exception:  # noqa: BLE001
        return False


def checks() -> list[tuple[str, str, str]]:
    """(state, label, detail) with state in ok / warn / bad."""
    rows: list[tuple[str, str, str]] = []

    def add(ok: bool, label: str, good: str, bad_: str, level: str = "bad") -> None:
        rows.append(("ok" if ok else level, label, good if ok else bad_))

    add(_set("TELEGRAM_BOT_TOKEN", "LAUNCHBOT_TOKEN"), "Launch Bot token", "set", "missing")
    add(_set("FERZAN_ADMIN_IDS", "ADMIN_TELEGRAM_ID"), "Admin alerts", "set", "missing: FERZAN_ADMIN_IDS")
    add(_set("FERZAN_LAUNCHES_CHANNEL"), "Launches channel", "set", "missing: FERZAN_LAUNCHES_CHANNEL")
    add(_set("MINI_APP_BASE_URL"), "Mini App address", "set", "missing: MINI_APP_BASE_URL")
    add(_set("SOLANA_RPC_URL"), "Solana RPC", "set", "missing: SOLANA_RPC_URL")
    add(_set("PLATFORM_TREASURY_SOL", "TREASURY_SOL"), "Fee wallet (Solana)", "set", "missing: PLATFORM_TREASURY_SOL")
    add(_set("PLATFORM_TREASURY_EVM"), "Fee wallet (EVM)", "set", "missing: PLATFORM_TREASURY_EVM")
    add(_set("PLATFORM_TREASURY_TON"), "Fee wallet (TON)", "set", "missing: PLATFORM_TREASURY_TON", "warn")
    add(_set("METEORA_CONFIG"), "Meteora config", "set", "missing: METEORA_CONFIG", "warn")
    add(_set("INTERNAL_API_TOKEN"), "Internal API token", "set", "missing: INTERNAL_API_TOKEN", "warn")
    add(_on("TON_LAUNCH_LIVE"), "TON launches", "live", "OFF: set TON_LAUNCH_LIVE=1 when you decide", "warn")
    add(_on("TRON_LAUNCH_LIVE"), "Tron launches", "live", "OFF: set TRON_LAUNCH_LIVE=1 when you decide", "warn")
    add(_on("PROMO_LIVE") and not _on("PROMO_OFF"), "Promo posts", "live", "not posting publicly (PROMO_LIVE=1 and PROMO_OFF unset)", "warn")
    add(_on("FLYWHEEL_LIVE") and not _on("FLYWHEEL_OFF"), "Flywheel (buy + burn)", "live", "not acting (FLYWHEEL_LIVE=1 and FLYWHEEL_OFF unset)", "warn")
    add(not _on("REFILL_OFF"), "Gas tank refill", "on", "switched off (REFILL_OFF=1)", "warn")

    db_path = _get("LAUNCH_DB_PATH") or "/opt/ferzan/app/launch/launch_bot.db"
    got = _launch_counts(db_path)
    if got is None:
        rows.append(("warn", "Launch records", "could not read the launch database"))
    else:
        by, stuck = got
        ok_n, bad_n = by.get("confirmed", 0), by.get("failed", 0)
        total = sum(by.values())
        if total == 0:
            rows.append(("warn", "Launches, last 24h", "none yet: do the rehearsal launch"))
        else:
            rows.append(("ok" if bad_n == 0 and stuck == 0 else "warn", "Launches, last 24h",
                         f"{ok_n} confirmed, {bad_n} failed" + (f", {stuck} stuck over 30 min" if stuck else "")))

    base = _get("MINI_APP_BASE_URL").rstrip("/")
    if base:
        page_ok = _http_ok(base + "/launches.html")
        rows.append(("ok" if page_ok else "bad", "Launch Mini App page", "loads" if page_ok else "does not load: " + base))
    api_ok = _http_ok("http://127.0.0.1:8000/api/launches")
    rows.append(("ok" if api_ok else "bad", "Launch API", "answering" if api_ok else "not answering on 127.0.0.1:8000"))

    if _state("ferzan-launch"):
        for name in SERVICES:
            st = _state(name)
            rows.append(("ok" if st == "active" else "bad", f"Service {name}", st))
        for name in JOBS:
            if _state(name) == "failed":
                rows.append(("bad", f"Job {name}", f"last run FAILED: journalctl -u {name} -n 20"))
        for name in TIMERS:
            st = _state(name)
            if st != "active":
                rows.append(("bad", f"Timer {name}", st or "unknown"))
        nxt = _timer_next("ferzan-flagship.timer")
        if nxt:
            fmt = lambda t: time.strftime("%a %b %d %H:%M UTC", time.gmtime(t))  # noqa: E731
            gap = abs(nxt - launch_at())
            rows.append(("ok" if gap <= 6 * 3600 else "warn", "Flagship launch timer",
                         f"fires {fmt(nxt)}" if gap <= 6 * 3600 else f"fires {fmt(nxt)} but the launch time is {fmt(launch_at())}: confirm which is right"))
    return rows


def report() -> str:
    rows = checks()
    icon = {"ok": "✅", "warn": "🟡", "bad": "🔴"}
    lines = [f"🚀 <b>Launch Bot · launch-day check</b>  ·  FERZAN in <b>{countdown()}</b>\n"]
    lines += [f"{icon[s]} {label}: {detail}" for s, label, detail in rows]
    bad = sum(1 for s, *_ in rows if s == "bad")
    warn = sum(1 for s, *_ in rows if s == "warn")
    lines.append("\n" + ("🟢 <b>All clear.</b>" if not bad and not warn else f"<b>{bad} must-fix, {warn} to review.</b>"))
    lines.append("<i>Still manual: gas tanks funded, promo cap raised, X handles checked, rehearsal done.</i>")
    return "\n".join(lines)
