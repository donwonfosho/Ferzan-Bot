"""
ton_pool_trades.py -- keeps a graduated TON coin's chart and trade list alive after it moves to its STON.fi pool.

The curve contract goes quiet once it graduates; every later buy and sell is a STON.fi swap. toncenter classifies
those as `jetton_swap` actions on the pool account, with who swapped, which side and both amounts, so this reads
them (from any wallet, the Trade Bot or anywhere else) and writes them into the same `trades` table the curve
trades use. The coin's price, market cap, volume and buy/sell counts follow.

  python ton_pool_trades.py probe <curve>   # prints what it would record for that curve, writes nothing
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import time

import requests

log = logging.getLogger("ton_pool_trades")
STON = "https://api.ston.fi/v1"
TON_ASSET = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"
SCHEMA = "CREATE TABLE IF NOT EXISTS ton_pool (curve TEXT PRIMARY KEY, pool TEXT, last_ts INTEGER DEFAULT 0, checked INTEGER DEFAULT 0);"
_SYNC_EVERY = 60
_last: dict = {}


def _api() -> str:
    return (os.environ.get("TONCENTER_URL") or "https://toncenter.com").rstrip("/")


def _headers() -> dict:
    k = (os.environ.get("TONCENTER_API_KEY") or "").strip()
    return {"X-API-Key": k} if k else {}


def _raw(addr) -> str:
    """Any TON address form -> canonical raw '0:HEX' (upper) so comparisons work. '' if not an address."""
    if not addr or not isinstance(addr, str):
        return ""
    try:
        from pytoniq_core import Address

        return Address(addr).to_str(is_user_friendly=False).upper()
    except Exception:
        return ""


def _friendly(addr) -> str:
    try:
        from pytoniq_core import Address

        return Address(addr).to_str(is_user_friendly=True, is_bounceable=False)
    except Exception:
        return str(addr or "")


def _amount(t) -> int:
    try:
        return int((t or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0


def parse_swap(action: dict, coin_raw: str) -> dict | None:
    """One toncenter action -> {is_buy, ton, tokens, trader, ts, id, seqno}, or None if it is not a swap of this coin
    against TON. The side that is not the coin is TON (native or STON.fi's proxy TON)."""
    if action.get("type") != "jetton_swap" or action.get("success") is False:
        return None
    d = action.get("details") or {}
    tin, tout = d.get("dex_incoming_transfer") or {}, d.get("dex_outgoing_transfer") or {}
    a_in, a_out = _raw(tin.get("asset")), _raw(tout.get("asset"))
    if a_out == coin_raw and a_in != coin_raw:
        is_buy, ton, tokens = True, _amount(tin), _amount(tout)
    elif a_in == coin_raw and a_out != coin_raw:
        is_buy, ton, tokens = False, _amount(tout), _amount(tin)
    else:
        return None
    if ton <= 0 or tokens <= 0:
        return None
    return {"is_buy": is_buy, "ton": ton, "tokens": tokens, "trader": _friendly(d.get("sender") or tin.get("source")),
            "ts": int(action.get("start_utime") or action.get("trace_end_utime") or 0),
            "id": action.get("action_id") or action.get("trace_id") or "",
            "seqno": int(action.get("trace_mc_seqno_end") or 0)}


def find_pool(token: str) -> str:
    r = requests.get(f"{STON}/pools/by_market/{TON_ASSET}/{token}", timeout=20)
    r.raise_for_status()
    pl = r.json().get("pool_list") or []
    return pl[0].get("address") if pl else ""


def fetch_swaps(pool: str, coin_raw: str, since_ts: int, max_pages: int = 6) -> list:
    out, offset = [], 0
    for _ in range(max_pages):
        params = {"account": pool, "action_type": "jetton_swap", "sort": "asc", "limit": 100, "offset": offset}
        if since_ts:
            params["start_utime"] = since_ts
        r = requests.get(f"{_api()}/api/v3/actions", params=params, headers=_headers(), timeout=25)
        r.raise_for_status()
        acts = r.json().get("actions") or []
        for a in acts:
            p = parse_swap(a, coin_raw)
            if p:
                out.append(p)
        if len(acts) < 100:
            break
        offset += 100
    return out


def sync(idx_conn, curve: str, token: str, force: bool = False) -> int:
    """Record new pool swaps of one graduated coin. Returns how many were new. Never raises."""
    now = int(time.time())
    if not force and now - _last.get(curve, 0) < _SYNC_EVERY:
        return 0
    _last[curve] = now
    try:
        with idx_conn() as c:
            c.executescript(SCHEMA)
            row = c.execute("SELECT pool, last_ts FROM ton_pool WHERE curve = ?", (curve,)).fetchone()
            cv = c.execute("SELECT total_supply FROM curves WHERE chain = 'ton' AND curve = ?", (curve,)).fetchone()
        if not cv:
            return 0
        pool, last_ts = (row[0], int(row[1] or 0)) if row else ("", 0)
        if not pool:
            pool = find_pool(token)
            if not pool:
                return 0
        coin_raw = _raw(token)
        swaps = fetch_swaps(pool, coin_raw, max(0, last_ts - 5))
        supply = int(cv[0] or 0)
        new = 0
        newest = last_ts
        with idx_conn() as c:
            for s in swaps:
                price = s["ton"] / s["tokens"]
                cur = c.execute(
                    "INSERT OR IGNORE INTO trades (chain, curve, block, ts, tx, log_index, trader, is_buy, native, tokens, price, "
                    "real_eth, fee, referrer) VALUES ('ton',?,?,?,?,0,?,?,?,?,?,'0',0,'')",
                    (curve, s["seqno"], s["ts"], s["id"], s["trader"], 1 if s["is_buy"] else 0, s["ton"] / 1e9,
                     s["tokens"] / 1e9, price))
                newest = max(newest, s["ts"])
                if cur.rowcount:
                    new += 1
                    c.execute(
                        "UPDATE curves SET price = ?, mcap = ?, volume = volume + ?, trades = trades + 1, buys = buys + ?, "
                        "sells = sells + ?, last_trade_ts = ? WHERE chain = 'ton' AND curve = ?",
                        (price, price * supply / 1e18, s["ton"] / 1e9, 1 if s["is_buy"] else 0, 0 if s["is_buy"] else 1,
                         s["ts"], curve))
            c.execute("INSERT INTO ton_pool (curve, pool, last_ts, checked) VALUES (?,?,?,?) ON CONFLICT(curve) DO UPDATE SET "
                      "pool = excluded.pool, last_ts = excluded.last_ts, checked = excluded.checked", (curve, pool, newest, now))
        if new:
            log.info("ton pool %s: %d new swaps", curve, new)
        return new
    except Exception as e:
        log.warning("ton pool trades %s: %s", curve, str(e)[:160])
        return 0


def _probe(curve: str) -> int:
    import curve_indexer as ci

    with ci.idx_conn() as c:
        row = c.execute("SELECT token FROM curves WHERE chain = 'ton' AND curve = ?", (curve,)).fetchone()
    if not row:
        print("curve not in the index")
        return 1
    token = row[0]
    pool = find_pool(token)
    print("pool", pool)
    swaps = fetch_swaps(pool, _raw(token), 0)
    print("swaps parsed:", len(swaps))
    for s in swaps:
        print(" ", "BUY " if s["is_buy"] else "SELL", f"{s['ton'] / 1e9:.4f} TON", f"{s['tokens'] / 1e9:,.2f} coins",
              f"price {s['ton'] / s['tokens']:.10f}", s["trader"][:10], time.strftime("%H:%M:%S", time.gmtime(s["ts"])))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if len(sys.argv) > 2 and sys.argv[1] == "probe":
        sys.exit(_probe(sys.argv[2]))
    print(__doc__)
