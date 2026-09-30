"""
tron_pool_trades.py -- keeps a graduated Tron coin's chart and trade list alive from its SunSwap pool.

graduate() opens a SunSwap V2 pair (curve.pool()) and the curve goes quiet, so every later buy and sell is a Swap event
on that pair. TronGrid serves those events; they are written into the same `trades` table as curve trades, and the
coin's price, market cap, volume and buy/sell counts follow. Trades from any wallet count, the Trade Bot and the site
included.

  python tron_pool_trades.py probe <curve>    # prints what it would record for that curve, writes nothing
"""
from __future__ import annotations

import logging
import os
import sys
import time

import requests

import tron_indexer as ti
import tron_launch as tl

log = logging.getLogger("tron_pool_trades")
SCHEMA = "CREATE TABLE IF NOT EXISTS tron_pool (curve TEXT PRIMARY KEY, pair TEXT, t0_is_token INTEGER, last_ms INTEGER DEFAULT 0);"
_SYNC_EVERY = 45
_last: dict = {}


def parse_swap(ev: dict, t0_is_token: bool) -> dict | None:
    """One TronGrid Swap event -> {is_buy, trx, tokens, trader, ts, ...} or None. The pool paid tokens out = a buy."""
    r = ev.get("result") or {}

    def num(*keys):
        for k in keys:
            if r.get(k) not in (None, ""):
                try:
                    return int(r[k])
                except (TypeError, ValueError):
                    pass
        return 0

    a0i, a1i, a0o, a1o = num("amount0In", "1"), num("amount1In", "2"), num("amount0Out", "3"), num("amount1Out", "4")
    tok_in, tok_out = (a0i, a0o) if t0_is_token else (a1i, a1o)
    trx_in, trx_out = (a1i, a1o) if t0_is_token else (a0i, a0o)
    is_buy = tok_out > 0
    trx, tokens = (trx_in, tok_out) if is_buy else (trx_out, tok_in)
    if trx <= 0 or tokens <= 0:
        return None
    return {"is_buy": is_buy, "trx": trx, "tokens": tokens, "trader": ti._addr(r.get("to") or r.get("5") or r.get("sender")),
            "ts_ms": int(ev.get("block_timestamp") or 0), "tx": ev.get("transaction_id") or "",
            "block": int(ev.get("block_number") or 0), "idx": int(ev.get("event_index") or 0)}


def fetch_swaps(pair: str, t0_is_token: bool, since_ms: int, max_pages: int = 6) -> list:
    url = f"{tl._grid()}/v1/contracts/{pair}/events"
    params = {"event_name": "Swap", "only_confirmed": "true", "order_by": "block_timestamp,asc", "limit": 200,
              "min_block_timestamp": since_ms + 1}
    key = tl._setting("TRONGRID_API_KEY")
    h = {"TRON-PRO-API-KEY": key} if key else {}
    out = []
    for _ in range(max_pages):
        d = requests.get(url, params=params, headers=h, timeout=20).json()
        if d.get("success") is False:
            raise RuntimeError(str(d.get("error") or d)[:120])
        data = d.get("data") or []
        for e in data:
            p = parse_swap(e, t0_is_token)
            if p:
                out.append(p)
        fp = (d.get("meta") or {}).get("fingerprint")
        if not fp or len(data) < 200:
            break
        params["fingerprint"] = fp
    return out


def _pair_of(curve: str) -> str:
    return ti._addr(f"{ti._q(curve, 'pool()'):040x}")


def sync(idx_conn, curve: str, token: str, force: bool = False) -> int:
    """Record new pool swaps of one graduated coin. Returns how many were new. Never raises."""
    now = int(time.time())
    if not force and now - _last.get(curve, 0) < _SYNC_EVERY:
        return 0
    _last[curve] = now
    try:
        with idx_conn() as c:
            c.executescript(SCHEMA)
            row = c.execute("SELECT pair, t0_is_token, last_ms FROM tron_pool WHERE curve = ?", (curve,)).fetchone()
            cv = c.execute("SELECT total_supply FROM curves WHERE chain = 'tron' AND curve = ?", (curve,)).fetchone()
        if not cv:
            return 0
        if row:
            pair, t0, last_ms = row[0], bool(row[1]), int(row[2] or 0)
        else:
            pair = _pair_of(curve)
            if not pair or pair == ti._addr("0" * 40):
                return 0
            t0 = ti._addr(f"{ti._q(pair, 'token0()'):040x}") == token
            last_ms = 0
        supply = int(cv[0] or 0) // ti.SCALE
        swaps = fetch_swaps(pair, t0, max(0, last_ms - 2000))
        new, newest = 0, last_ms
        with idx_conn() as c:
            for s in swaps:
                price = s["trx"] / s["tokens"]  # both 6 decimals: TRX per coin
                cur = c.execute(
                    "INSERT OR IGNORE INTO trades (chain, curve, block, ts, tx, log_index, trader, is_buy, native, tokens, price, "
                    "real_eth, fee, referrer) VALUES ('tron',?,?,?,?,?,?,?,?,?,?,'0',0,'')",
                    (curve, s["block"], s["ts_ms"] // 1000, s["tx"], s["idx"], s["trader"], 1 if s["is_buy"] else 0,
                     s["trx"] / 1e6, s["tokens"] / 1e6, price))
                newest = max(newest, s["ts_ms"])
                if cur.rowcount:
                    new += 1
                    c.execute(
                        "UPDATE curves SET price = ?, mcap = ?, volume = volume + ?, trades = trades + 1, buys = buys + ?, "
                        "sells = sells + ?, last_trade_ts = ? WHERE chain = 'tron' AND curve = ?",
                        (price, price * supply / 1e6, s["trx"] / 1e6, 1 if s["is_buy"] else 0, 0 if s["is_buy"] else 1,
                         s["ts_ms"] // 1000, curve))
            c.execute("INSERT INTO tron_pool (curve, pair, t0_is_token, last_ms) VALUES (?,?,?,?) ON CONFLICT(curve) DO UPDATE SET "
                      "last_ms = excluded.last_ms", (curve, pair, 1 if t0 else 0, newest))
        if new:
            log.info("tron pool %s: %d new swaps", curve, new)
        return new
    except Exception as e:
        log.warning("tron pool trades %s: %s", curve, str(e)[:160])
        return 0


def _probe(curve: str) -> int:
    import curve_indexer as ci

    with ci.idx_conn() as c:
        row = c.execute("SELECT token FROM curves WHERE chain = 'tron' AND curve = ?", (curve,)).fetchone()
    if not row:
        print("curve not in the index")
        return 1
    token = row[0]
    pair = _pair_of(curve)
    print("pair", pair)
    t0 = ti._addr(f"{ti._q(pair, 'token0()'):040x}") == token
    swaps = fetch_swaps(pair, t0, 0)
    print("swaps parsed:", len(swaps))
    for s in swaps[-15:]:
        print(" ", "BUY " if s["is_buy"] else "SELL", f"{s['trx'] / 1e6:.2f} TRX", f"{s['tokens'] / 1e6:,.2f} coins",
              s["trader"][:8], time.strftime("%H:%M:%S", time.gmtime(s["ts_ms"] // 1000)))
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if len(sys.argv) > 2 and sys.argv[1] == "probe":
        sys.exit(_probe(sys.argv[2]))
    print(__doc__)
