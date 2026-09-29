"""
ton_indexer.py -- puts Ferzan's TON bonding curves into the same curve index as the EVM, Solana and Tron curves, so the
website board, King of the Hill, 50%/90% + graduation alerts, X posts, the Buy Bot and the leaderboard cover TON too.
Called from curve_indexer's loop; runs at most every TON_POLL_SECONDS.

Trades come from the curve's own log: every buy/sell sends an external-out message (op 0x46545244) that toncenter
shows on the curve's transactions. State (progress, complete, graduated) is always re-read from the contract's
get_curve method, so a missed log can never leave the board wrong for long.

Graduation: when a curve is complete and not graduated, scripts/ton_graduate.py (keeper wallet) calls graduate() and then
opens the STON.fi pool and burns the LP. The key is only ever loaded by that script, never by this process.

Units follow the index's 18-decimal convention: nanoTON and coin units (both 9 decimals) are stored * 1e9.

  python ton_indexer.py selftest            # decodes a locally built log message (run on the droplet)
  python ton_indexer.py probe <curve>       # prints the state and the last decoded trades of one curve
"""
from __future__ import annotations

import base64
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

log = logging.getLogger("ton_indexer")
HERE = Path(__file__).resolve().parent
POLL = int(os.environ.get("TON_POLL_SECONDS") or 30)
IDLE_POLL = 600
SCALE = 10**9  # 9 decimals -> 18-decimal index units
GRAD_RETRY = 900
OP_TRADE_LOG = 0x46545244
SCHEMA = ("CREATE TABLE IF NOT EXISTS ton_state (curve TEXT PRIMARY KEY, token TEXT, v_ton TEXT, v_tok TEXT, "
          "last_lt INTEGER DEFAULT 0, checked_ts INTEGER DEFAULT 0, grad_try_ts INTEGER DEFAULT 0, note TEXT);")
_last_run = 0.0


def _testnet() -> bool:
    return (os.environ.get("TON_NETWORK") or "").strip().lower() == "testnet"


def _api() -> str:
    return (os.environ.get("TONCENTER_URL") or ("https://testnet.toncenter.com" if _testnet() else "https://toncenter.com")).rstrip("/")


def _headers() -> dict:
    k = (os.environ.get("TONCENTER_API_KEY") or "").strip()
    return {"X-API-Key": k} if k else {}


def decode_log(body_b64: str) -> dict | None:
    """One external-out body -> {is_buy, trader, ton, tokens, fee, real, sold}, or None if it is not a Ferzan trade log."""
    from pytoniq_core import Cell

    try:
        c = Cell.one_from_boc(base64.b64decode(body_b64))
        s = c.begin_parse()
        if s.remaining_bits < 32 + 1 or s.load_uint(32) != OP_TRADE_LOG:
            return None
        is_buy = s.load_uint(1)
        trader = s.load_address()
        ton, tokens, fee, real, sold = (s.load_coins() for _ in range(5))
    except Exception:
        return None
    return {"is_buy": bool(is_buy), "trader": trader.to_str(is_user_friendly=True, is_bounceable=False) if trader else "",
            "ton": ton, "tokens": tokens, "fee": fee, "real": real, "sold": sold}


def _txs(curve: str, since_lt: int) -> list:
    """Transactions of the curve after since_lt, oldest first (toncenter v3)."""
    out, params = [], {"account": curve, "sort": "asc", "limit": 100, "start_lt": since_lt + 1}
    for _ in range(5):
        r = requests.get(f"{_api()}/api/v3/transactions", params=params, headers=_headers(), timeout=20)
        r.raise_for_status()
        txs = r.json().get("transactions") or []
        out += txs
        if len(txs) < 100:
            break
        params["start_lt"] = int(txs[-1]["lt"]) + 1
    return out


def trades_from_tx(tx: dict) -> list:
    res = []
    for i, m in enumerate(tx.get("out_msgs") or []):
        if m.get("destination"):
            continue
        body = ((m.get("message_content") or {}).get("body")) or ""
        d = decode_log(body) if body else None
        if d:
            d["idx"] = i
            res.append(d)
    return res


def _launches(launch_db: str) -> list:
    try:
        c = sqlite3.connect(f"file:{launch_db}?mode=ro", uri=True, timeout=30)
        rows = c.execute(
            "SELECT id, name, symbol, wallet_address, result_token_address, created_at, extra_params, chat_id "
            "FROM launch_requests WHERE chain = 'ton' AND mode = 'bonding_curve' AND status = 'confirmed' "
            "AND result_token_address IS NOT NULL ORDER BY created_at DESC LIMIT 100").fetchall()
        c.close()
        return rows
    except sqlite3.Error as e:
        log.warning("launch db read failed: %s", e)
        return []


def _graduate(curve: str) -> dict:
    try:
        p = subprocess.run([sys.executable, "-W", "ignore", str(HERE / "scripts" / "ton_graduate.py"), curve],
                           capture_output=True, text=True, timeout=600, cwd=str(HERE))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timed out waiting for TON"}
    for line in reversed((p.stdout or "").splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {"ok": False, "error": (p.stderr or "no answer")[-200:]}


def _admin_note(idx_conn, key: str, text: str) -> None:
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
    if not (os.environ.get("TON_CURVE_MASTER") or "").strip():
        return 0
    rows = _launches(launch_db)
    if not rows:
        return 0
    with idx_conn() as c:
        c.executescript(SCHEMA)
        state = {r[0]: r for r in c.execute(
            "SELECT curve, token, v_ton, v_tok, last_lt, checked_ts, grad_try_ts, note FROM ton_state")}
        known = {r[0]: r for r in c.execute(
            "SELECT curve, graduated, last_trade_ts, real_eth, grad_target FROM curves WHERE chain = 'ton'")}
    now = int(time.time())
    n = 0
    for rid, name, symbol, wallet, token, created, extra, chat in rows:
        try:
            ex = json.loads(extra or "{}")
        except ValueError:
            ex = {}
        curve = (ex.get("curve_address") or ex.get("ton_curve") or "").strip()
        if not curve:
            continue
        k, st = known.get(curve), state.get(curve)
        if k and k[1] and not (st and st[7] != "done" and now - int(st[6] or 0) >= GRAD_RETRY):
            continue  # graduated and the keeper finished: it trades on STON.fi now
        idle = k is not None and (k[2] or 0) < now - 6 * 3600
        if st and idle and now - (st[5] or 0) < IDLE_POLL:
            continue
        try:
            n += _one(idx_conn, curve, token, name, symbol, wallet, created, st, k is None, now)
        except Exception as e:
            log.warning("ton curve %s: %s", curve, str(e)[:160])
    return n


def _one(idx_conn, curve, token, name, symbol, wallet, created, st, first, now) -> int:
    import ton_curve as tc

    cs = tc.curve_state(curve)
    supply, grad, start = cs["supply"], cs["grad"], cs["start"]
    v_ton, v_tok = grad // 3, supply * 16 // 15  # the curve's virtual reserves (see ferzan_curve.fc quote_buy)
    if st is None:
        try:
            launched = int(datetime.fromisoformat(str(created).replace("Z", "+00:00")).timestamp())
        except ValueError:
            launched = now
        price0 = v_ton / v_tok if v_tok else 0.0
        with idx_conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO curves (chain, curve, token, creator, name, symbol, total_supply, curve_supply, "
                "grad_target, v_eth, v_token, start_time, real_eth, tokens_sold, price, mcap, launched_ts, graduated) "
                "VALUES ('ton', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '0', '0', ?, ?, ?, 0)",
                (curve, token, wallet or "", name or "", symbol or "", str(supply * SCALE), str(supply * SCALE),
                 str(grad * SCALE), str(v_ton * SCALE), str(v_tok * SCALE), start, price0, price0 * supply / 1e9, launched))
            c.execute("INSERT OR IGNORE INTO ton_state (curve, token, v_ton, v_tok, last_lt, checked_ts) "
                      "VALUES (?, ?, ?, ?, 0, 0)", (curve, token, str(v_ton), str(v_tok)))
        st = (curve, token, str(v_ton), str(v_tok), 0, 0, 0, "")
    last_lt = int(st[4] or 0)
    txs = _txs(curve, last_lt)
    with idx_conn() as c:
        for tx in txs:
            lt = int(tx.get("lt") or 0)
            ts = int(tx.get("now") or 0)
            for d in trades_from_tx(tx):
                price = (v_ton + d["real"]) / (v_tok - d["sold"]) if v_tok > d["sold"] else 0.0
                cur = c.execute(
                    "INSERT OR IGNORE INTO trades (chain, curve, block, ts, tx, log_index, trader, is_buy, native, tokens, "
                    "price, real_eth, fee, referrer) VALUES ('ton',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (curve, int(tx.get("mc_block_seqno") or 0), ts, tx.get("hash") or "", d["idx"], d["trader"],
                     1 if d["is_buy"] else 0, d["ton"] / 1e9, d["tokens"] / 1e9, price, str(d["real"] * SCALE),
                     d["fee"] / 1e9, ""))
                if cur.rowcount:
                    c.execute(
                        "UPDATE curves SET real_eth = ?, tokens_sold = ?, price = ?, mcap = ?, volume = volume + ?, "
                        "trades = trades + 1, buys = buys + ?, sells = sells + ?, last_trade_ts = ? "
                        "WHERE chain = 'ton' AND curve = ?",
                        (str(d["real"] * SCALE), str(d["sold"] * SCALE), price, price * supply / 1e9, d["ton"] / 1e9,
                         1 if d["is_buy"] else 0, 0 if d["is_buy"] else 1, ts, curve))
            last_lt = max(last_lt, lt)
        # the contract is the truth: re-sync progress from it even if a log was missed
        price = (v_ton + cs["real"]) / (v_tok - cs["sold"]) if v_tok > cs["sold"] else 0.0
        c.execute("UPDATE curves SET real_eth = ?, tokens_sold = ?, price = ?, mcap = ? WHERE chain = 'ton' AND curve = ?",
                  (str(cs["real"] * SCALE), str(cs["sold"] * SCALE), price, price * supply / 1e9, curve))
        if first and txs:  # milestones already passed before we started watching: record silently
            prog = cs["real"] * 100.0 / grad if grad else 0.0
            for t in (50, 90):
                if prog >= t:
                    c.execute("INSERT OR IGNORE INTO alerts_sent (chain, curve, kind, ts) VALUES ('ton', ?, ?, ?)",
                              (curve, f"p{t}", now))
        c.execute("UPDATE ton_state SET last_lt = ?, checked_ts = ? WHERE curve = ?", (last_lt, now, curve))
    if cs["complete"] or cs["graduated"]:
        _check_graduation(idx_conn, curve, cs, first, now, st)
    return 1


def _check_graduation(idx_conn, curve, cs, first, now, st) -> None:
    graduated = cs["graduated"]
    if cs["complete"] and st[7] != "done" and now - int(st[6] or 0) >= GRAD_RETRY:
        with idx_conn() as c:
            c.execute("UPDATE ton_state SET grad_try_ts = ? WHERE curve = ?", (now, curve))
        res = _graduate(curve)
        log.info("ton keeper %s: %s", curve, res)
        if res.get("dry_run"):
            pass  # keeper is in dry-run mode: nothing was sent, so nothing has graduated
        elif res.get("ok") and res.get("stage") == "done":
            graduated = True
            with idx_conn() as c:
                c.execute("UPDATE ton_state SET note = 'done' WHERE curve = ?", (curve,))
        elif res.get("error") == "low_balance":
            _admin_note(idx_conn, f"ton_keeper_low_{curve}",
                        f"A TON curve filled but the keeper wallet {res.get('address', '')} has only "
                        f"{res.get('balance_ton', 0):.2f} TON. Send about {res.get('need_ton', 1):.1f} TON to it so it can "
                        f"graduate {curve}.")
        elif res.get("stage") == "pool":  # coins reached the keeper but the pool step failed: a human should look
            _admin_note(idx_conn, f"ton_pool_{curve}",
                        f"TON curve {curve} graduated (funds are with the keeper) but the STON.fi pool step failed: "
                        f"{str(res.get('error'))[:160]}. Re-run scripts/ton_graduate.py {curve}.")
    if graduated:
        with idx_conn() as c:
            row = c.execute("SELECT real_eth FROM curves WHERE chain = 'ton' AND curve = ?", (curve,)).fetchone()
            c.execute(
                "UPDATE curves SET graduated = 1, grad_ts = ?, grad_native = ?, "
                "grad_notified = CASE WHEN ? THEN 1 ELSE grad_notified END WHERE chain = 'ton' AND curve = ? AND graduated = 0",
                (0 if first else now, int(cs["grad"]) / 1e9, 1 if first else 0, curve))


# --------------------------------------------------------------------------- self tests (droplet)
def _selftest() -> int:
    from pytoniq_core import Address, begin_cell

    addr = Address("0:" + "11" * 32)
    body = (begin_cell().store_uint(OP_TRADE_LOG, 32).store_uint(1, 1).store_address(addr).store_coins(10_000_000_000)
            .store_coins(123_456_789).store_coins(100_000_000).store_coins(9_900_000_000).store_coins(123_456_789).end_cell())
    d = decode_log(base64.b64encode(body.to_boc()).decode())
    assert d and d["is_buy"] and d["ton"] == 10_000_000_000 and d["tokens"] == 123_456_789 and d["sold"] == 123_456_789, d
    sell = (begin_cell().store_uint(OP_TRADE_LOG, 32).store_uint(0, 1).store_address(addr).store_coins(1).store_coins(2)
            .store_coins(3).store_coins(4).store_coins(5).end_cell())
    d2 = decode_log(base64.b64encode(sell.to_boc()).decode())
    assert d2 and not d2["is_buy"] and (d2["ton"], d2["tokens"], d2["fee"], d2["real"], d2["sold"]) == (1, 2, 3, 4, 5), d2
    other = begin_cell().store_uint(0xDEADBEEF, 32).end_cell()
    assert decode_log(base64.b64encode(other.to_boc()).decode()) is None
    assert decode_log("not-a-boc") is None
    tx = {"out_msgs": [{"destination": "EQ...", "message_content": {"body": "AAAA"}},
                       {"destination": None, "message_content": {"body": base64.b64encode(body.to_boc()).decode()}}]}
    assert len(trades_from_tx(tx)) == 1 and trades_from_tx(tx)[0]["idx"] == 1
    print("ton_indexer selftest: OK")
    return 0


def _probe(curve: str) -> int:
    import ton_curve as tc

    print("state:", tc.curve_state(curve))
    txs = _txs(curve, 0)
    print(f"{len(txs)} transactions")
    for tx in txs[-10:]:
        for d in trades_from_tx(tx):
            print(" ", tx.get("hash", "")[:12], "BUY " if d["is_buy"] else "SELL", d["ton"] / 1e9, "TON", d["tokens"] / 1e9, "coins")
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "selftest":
        sys.exit(_selftest())
    if len(sys.argv) >= 3 and sys.argv[1] == "probe":
        sys.exit(_probe(sys.argv[2]))
    print(__doc__)
