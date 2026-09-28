"""
tron_indexer.py -- puts Ferzan's Tron bonding curves into the same curve index as the EVM and Solana
curves, so the website board, King of the Hill, 50%/90% + graduation alerts, X posts, the Buy Bot and
the leaderboard cover Tron too. Called from curve_indexer's loop; runs at most every TRON_POLL_SECONDS.

It is also the graduation keeper: when a curve fills (complete) and nobody has graduated it yet, it runs
scripts/tron_graduate.py, which calls graduate() from the deployer wallet (the curve pays that wallet the
graduation reward for the energy). The key is only ever loaded by that script, never by this process.

Units follow the index's 18-decimal convention: sun and coin units (both 6 decimals) are stored * 1e12,
so progress and "x / y raised" work unchanged. Trades are confirmed Trade events from TronGrid.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

import tron_launch as tl

log = logging.getLogger("tron_indexer")
HERE = Path(__file__).resolve().parent
POLL = int(os.environ.get("TRON_POLL_SECONDS") or 45)
IDLE_POLL = 600  # curves with no trade for 6h are checked every 10 minutes
SCALE = 10**12   # 6 decimals -> 18-decimal index units
GRAD_RETRY = 600
SCHEMA = ("CREATE TABLE IF NOT EXISTS tron_state (curve TEXT PRIMARY KEY, token TEXT, v_sun TEXT, v_tok TEXT, "
          "last_ms INTEGER DEFAULT 0, checked_ts INTEGER DEFAULT 0, grad_try_ts INTEGER DEFAULT 0, note TEXT);")
_last_run = 0.0


def _q(curve: str, sig: str) -> int:
    h = tl.to_hex41(curve)
    r = tl._post("/wallet/triggerconstantcontract", {"owner_address": h, "contract_address": h,
                                                    "function_selector": sig, "parameter": ""})
    if any(x.get("ret") == "FAILED" for x in ((r.get("transaction") or {}).get("ret") or [])):
        raise RuntimeError(f"{sig} reverted")
    res = (r.get("constant_result") or [""])[0]
    if not res:
        raise RuntimeError(f"{sig}: no answer")
    return int(res[:64], 16)


def _q_str(contract: str, sig: str) -> str:
    h = tl.to_hex41(contract)
    r = tl._post("/wallet/triggerconstantcontract", {"owner_address": h, "contract_address": h,
                                                    "function_selector": sig, "parameter": ""})
    res = (r.get("constant_result") or [""])[0] or ""
    try:
        n = int(res[64:128], 16)
        return bytes.fromhex(res[128:128 + n * 2]).decode("utf-8", "ignore")
    except ValueError:
        return ""


def _addr(v) -> str:
    s = str(v or "")
    if s.startswith("T") and len(s) == 34:
        return s
    h = s.lower().replace("0x", "")
    return tl.to_b58(h[-40:]) if len(h) >= 40 else ""


def _launches(launch_db: str) -> list:
    try:
        c = sqlite3.connect(f"file:{launch_db}?mode=ro", uri=True, timeout=30)
        rows = c.execute(
            "SELECT id, name, symbol, wallet_address, result_token_address, created_at, extra_params, chat_id "
            "FROM launch_requests WHERE chain = 'tron' AND mode = 'bonding_curve' AND status = 'confirmed' "
            "AND result_token_address IS NOT NULL ORDER BY created_at DESC LIMIT 100").fetchall()
        c.close()
        return rows
    except sqlite3.Error as e:
        log.warning("launch db read failed: %s", e)
        return []


def _events(curve: str, since_ms: int) -> list:
    """Confirmed Trade events after since_ms, oldest first."""
    url = f"{tl._grid()}/v1/contracts/{curve}/events"
    params = {"event_name": "Trade", "only_confirmed": "true", "order_by": "block_timestamp,asc", "limit": 200,
              "min_block_timestamp": since_ms + 1}
    h = {"TRON-PRO-API-KEY": tl._setting("TRONGRID_API_KEY")} if tl._setting("TRONGRID_API_KEY") else {}
    out = []
    for _ in range(5):
        d = requests.get(url, params=params, headers=h, timeout=20).json()
        if d.get("success") is False:
            raise RuntimeError(str(d.get("error") or d)[:120])
        out += d.get("data") or []
        fp = (d.get("meta") or {}).get("fingerprint")
        if not fp or len(d.get("data") or []) < 200:
            break
        params["fingerprint"] = fp
    return out


def _graduate(curve: str) -> dict:
    try:
        p = subprocess.run([sys.executable, "-W", "ignore", str(HERE / "scripts" / "tron_graduate.py"), curve],
                           capture_output=True, text=True, timeout=240, cwd=str(HERE))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timed out waiting for Tron"}
    for line in reversed((p.stdout or "").splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {"ok": False, "error": (p.stderr or "no answer")[-200:]}


def _admin_note(idx_conn, key: str, text: str) -> None:
    """One Telegram note to the admin per key (e.g. 'the keeper wallet needs TRX')."""
    admins = {x.strip() for x in ((os.environ.get("FERZAN_ADMIN_IDS") or "") + "," +
                                  (os.environ.get("ADMIN_TELEGRAM_ID") or "")).split(",") if x.strip()}
    with idx_conn() as c:
        if c.execute("SELECT 1 FROM alert_state WHERE k = ?", (key,)).fetchone():
            return
        c.execute("INSERT OR REPLACE INTO alert_state (k, v) VALUES (?, ?)", (key, str(int(time.time()))))
    token = os.environ.get("LAUNCHBOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    for admin in admins if token else ():
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": admin, "text": text, "disable_web_page_preview": True}, timeout=15)
        except requests.RequestException:
            pass


def run(idx_conn, launch_db: str, force: bool = False) -> int:
    """One pass. Returns how many curves were updated."""
    global _last_run
    if not force and time.time() - _last_run < POLL:
        return 0
    _last_run = time.time()
    if not tl.curve_factory():
        return 0
    rows = _launches(launch_db)
    if not rows:
        return 0
    with idx_conn() as c:
        c.executescript(SCHEMA)
        state = {r[0]: r for r in c.execute(
            "SELECT curve, token, v_sun, v_tok, last_ms, checked_ts, grad_try_ts FROM tron_state")}
        known = {r[0]: r for r in c.execute(
            "SELECT curve, graduated, last_trade_ts, real_eth, grad_target FROM curves WHERE chain = 'tron'")}
    now = int(time.time())
    n = 0
    for rid, name, symbol, wallet, token, created, extra, chat in rows:
        try:
            curve = (json.loads(extra or "{}").get("curve_address") or "").strip()
        except ValueError:
            curve = ""
        if not (curve.startswith("T") and len(curve) == 34):
            continue
        k, st = known.get(curve), state.get(curve)
        if k and k[1]:
            continue  # graduated: it trades on SunSwap now (DEX sites and the Trade Bot take over)
        idle = k is not None and (k[2] or 0) < now - 6 * 3600
        if st and idle and now - (st[5] or 0) < IDLE_POLL:
            continue
        try:
            n += _one(idx_conn, curve, token, name, symbol, wallet, created, st, k is None, now)
        except Exception as e:
            log.warning("tron curve %s: %s", curve, str(e)[:160])
    return n


def _one(idx_conn, curve, token, name, symbol, wallet, created, st, first, now) -> int:
    if st is None:  # constants, read once and checked against our factory
        if tl.curve_factory() != _addr(f"{_q(curve, 'factory()'):040x}"):
            raise RuntimeError("not made by the Ferzan Tron curve factory")
        if _addr(f"{_q(curve, 'token()'):040x}") != token:
            raise RuntimeError("curve does not belong to this coin")
        v_sun, v_tok = _q(curve, "virtualEth()"), _q(curve, "virtualToken()")
        supply, grad, start = _q(token, "totalSupply()"), _q(curve, "gradTarget()"), _q(curve, "startTime()")
        try:
            launched = int(datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp())
        except ValueError:
            launched = now
        with idx_conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO curves (chain, curve, token, creator, name, symbol, total_supply, curve_supply, "
                "grad_target, v_eth, v_token, start_time, real_eth, tokens_sold, price, mcap, launched_ts, graduated) "
                "VALUES ('tron', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '0', '0', ?, ?, ?, 0)",
                (curve, token, wallet or "", name or _q_str(token, "name()"), symbol or _q_str(token, "symbol()"),
                 str(supply * SCALE), str(supply * SCALE), str(grad * SCALE), str(v_sun * SCALE), str(v_tok * SCALE),
                 start, v_sun / v_tok if v_tok else 0.0, (v_sun / v_tok if v_tok else 0.0) * supply / 1e6, launched))
            c.execute("INSERT OR IGNORE INTO tron_state (curve, token, v_sun, v_tok, last_ms, checked_ts) "
                      "VALUES (?, ?, ?, ?, 0, 0)", (curve, token, str(v_sun), str(v_tok)))
        st = (curve, token, str(v_sun), str(v_tok), 0, 0, 0)
    v_sun, v_tok, last_ms = int(st[2]), int(st[3]), int(st[4] or 0)
    evs = _events(curve, last_ms)
    with idx_conn() as c:
        cv = c.execute("SELECT total_supply FROM curves WHERE chain = 'tron' AND curve = ?", (curve,)).fetchone()
        supply = int(cv[0]) // SCALE if cv else 0
        for e in evs:
            r = e.get("result") or {}
            is_buy = str(r.get("isBuy", r.get("1", ""))).lower() in ("true", "1")
            native, toks = int(r.get("nativeAmount") or r.get("2") or 0), int(r.get("tokenAmount") or r.get("3") or 0)
            fee = int(r.get("fee") or r.get("4") or 0)
            real_after, sold_after = int(r.get("realEthAfter") or r.get("6") or 0), int(r.get("tokensSoldAfter") or r.get("7") or 0)
            ts = int(e.get("block_timestamp") or 0) // 1000
            price = (v_sun + real_after) / (v_tok - sold_after) if v_tok > sold_after else 0.0
            cur = c.execute(
                "INSERT OR IGNORE INTO trades (chain, curve, block, ts, tx, log_index, trader, is_buy, native, tokens, "
                "price, real_eth, fee, referrer) VALUES ('tron',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (curve, int(e.get("block_number") or 0), ts, e.get("transaction_id") or "", int(e.get("event_index") or 0),
                 _addr(r.get("trader") or r.get("0")), 1 if is_buy else 0, native / 1e6, toks / 1e6, price,
                 str(real_after * SCALE), fee / 1e6, _addr(r.get("referrer") or r.get("5"))))
            last_ms = max(last_ms, int(e.get("block_timestamp") or 0))
            if cur.rowcount == 0:
                continue
            c.execute(
                "UPDATE curves SET real_eth = ?, tokens_sold = ?, price = ?, mcap = ?, volume = volume + ?, "
                "trades = trades + 1, buys = buys + ?, sells = sells + ?, last_trade_ts = ? WHERE chain = 'tron' AND curve = ?",
                (str(real_after * SCALE), str(sold_after * SCALE), price, price * supply / 1e6, native / 1e6,
                 1 if is_buy else 0, 0 if is_buy else 1, ts, curve))
        if first and evs:  # milestones already passed before we started watching: record silently
            row = c.execute("SELECT real_eth, grad_target FROM curves WHERE chain = 'tron' AND curve = ?", (curve,)).fetchone()
            prog = int(row[0]) * 100.0 / int(row[1]) if row and int(row[1] or 0) else 0.0
            for t in (50, 90):
                if prog >= t:
                    c.execute("INSERT OR IGNORE INTO alerts_sent (chain, curve, kind, ts) VALUES ('tron', ?, ?, ?)",
                              (curve, f"p{t}", now))
        c.execute("UPDATE tron_state SET last_ms = ?, checked_ts = ? WHERE curve = ?", (last_ms, now, curve))
        row = c.execute("SELECT real_eth, grad_target FROM curves WHERE chain = 'tron' AND curve = ?", (curve,)).fetchone()
    near_full = row and int(row[1] or 0) and int(row[0]) * 100 >= int(row[1]) * 95
    if near_full or first:
        _check_graduation(idx_conn, curve, first, now, st)
    return 1


def _check_graduation(idx_conn, curve, first, now, st) -> None:
    graduated, complete = bool(_q(curve, "graduated()")), bool(_q(curve, "complete()"))
    if complete and not graduated and now - int(st[6] or 0) >= GRAD_RETRY:
        with idx_conn() as c:
            c.execute("UPDATE tron_state SET grad_try_ts = ? WHERE curve = ?", (now, curve))
        res = _graduate(curve)
        log.info("tron keeper %s: %s", curve, res)
        if res.get("ok") or res.get("error") == "already graduated":
            graduated = True
        elif res.get("error") == "low_balance":
            _admin_note(idx_conn, f"tron_keeper_low_{curve}",
                        f"A Tron curve filled but the keeper wallet {res.get('address', '')} has only "
                        f"{res.get('balance_trx', 0):.0f} TRX and graduating costs about 230 TRX of energy (it earns the "
                        f"graduation reward back). Send about {res.get('need_trx', 260):.0f} TRX to it, or anyone can call "
                        f"graduate() on {curve}.")
    if graduated:
        pool = _addr(f"{_q(curve, 'pool()'):040x}")
        with idx_conn() as c:
            row = c.execute("SELECT real_eth FROM curves WHERE chain = 'tron' AND curve = ?", (curve,)).fetchone()
            c.execute(
                "UPDATE curves SET graduated = 1, pool = ?, grad_ts = ?, grad_native = ?, "
                "grad_notified = CASE WHEN ? THEN 1 ELSE grad_notified END WHERE chain = 'tron' AND curve = ? AND graduated = 0",
                (pool, 0 if first else now, int(row[0] if row else 0) / 1e18, 1 if first else 0, curve))
