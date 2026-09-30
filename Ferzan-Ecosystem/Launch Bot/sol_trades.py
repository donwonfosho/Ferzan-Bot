"""Ferzan Solana trade stream: every trade on a Ferzan Meteora curve, within about two seconds.

Every swap on a Ferzan Solana curve passes through its Ferzan launch config (the community config and the
FERZAN flagship config), so watching those accounts sees every trade, including ones routed through Jupiter.
For each new transaction it records one row in the curve index `trades` table (the same table the EVM and
Tron curves use): trader, buy/sell, SOL, tokens. Then it reads the pool's live price and updates the coin's
price, market cap and progress, and adds a chart point. The website's live feed, holders panel and profit
cards all read those rows.

Read-only on chain: it never signs or sends anything. sol_indexer.py keeps running as before (volume counters,
graduation alerts); this only adds the per-trade detail and faster prices.

  sol_trades.py run          the service loop (ferzan-sol-trades.service)
  sol_trades.py test [N]     dry-run: parse the last N transactions of each config and print them, write nothing
Settings (/opt/ferzan/.env): SOL_TRADES_OFF=1, SOL_TRADES_POLL (seconds, default 1.5), METEORA_CONFIG(S),
FERZAN_FLAGSHIP_CONFIG (default: the FERZAN config).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for f in ("/opt/ferzan/.env", str(HERE / ".env")):
    if os.path.isfile(f):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
import requests  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sol_trades")

RPC = (os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com").strip()
INDEX_DB = os.environ.get("CURVE_INDEX_DB") or "/opt/ferzan/app/launch/curve_index.db"
LAUNCH_DB = os.environ.get("LAUNCH_DB_PATH") or "/opt/ferzan/app/launch/launch_bot.db"
STATE = Path(os.environ.get("SOL_TRADES_STATE") or "/opt/ferzan/app/launch/sol_trades_state.json")
POLL = max(0.8, float(os.environ.get("SOL_TRADES_POLL") or 1.5))
WSOL = "So11111111111111111111111111111111111111112"
DBC = "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"
FLAGSHIP_DEFAULT = "8YoqjUBsyfgQv5s7fWR5nyjeMd43rKvMUEexAuYRCvbo"
SUPPLY = 1_000_000_000
SCALE = 10**9  # lamports -> the index's 18-decimal units (same as sol_indexer)


def configs() -> list[str]:
    raw = [os.environ.get("METEORA_CONFIG") or "", *(os.environ.get("METEORA_CONFIGS") or "").split(","),
           os.environ.get("FERZAN_FLAGSHIP_CONFIG") or FLAGSHIP_DEFAULT]
    out = []
    for x in raw:
        x = x.strip()
        if 32 <= len(x) <= 44 and x not in out:
            out.append(x)
    return out


def pool_authority() -> str:
    try:
        from solders.pubkey import Pubkey
        return str(Pubkey.find_program_address([b"pool_authority"], Pubkey.from_string(DBC))[0])
    except Exception:
        return ""


# ---------------------------------------------------- after graduation --
# Meteora migrates a filled curve into a DAMM v2 pool: the curve goes quiet and every later trade (this bot, Jupiter,
# anyone) is a swap in that pool. Its vaults are owned by the DAMM v2 pool authority, so the same balance-delta parse
# works; this finds each graduated coin's pool and follows it.
DAMM_AUTH = (os.environ.get("DAMM_POOL_AUTHORITY") or "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC").strip()
GRAD_POLL = 12.0
BACKFILL = 100  # signatures read the first time a graduated pool is seen, so past trades appear too
_grad_last = 0.0


def graduated_map() -> dict:
    """mint -> curve (the DBC pool key the site uses) for graduated Ferzan Solana coins."""
    try:
        with idx() as c:
            return {r["token"]: r["curve"] for r in c.execute("SELECT token, curve FROM curves WHERE chain = 'solana' AND graduated = 1")}
    except sqlite3.Error:
        return {}


def damm_pool_for(mint: str, dbc_pool: str) -> str:
    """The coin's live post-graduation pool, from DexScreener: the most liquid Solana pair that is not the old curve."""
    try:
        pairs = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=15).json().get("pairs") or []
    except Exception:
        return ""
    best, best_liq = "", -1.0
    for p in pairs:
        if p.get("chainId") != "solana" or p.get("pairAddress") == dbc_pool:
            continue
        if (p.get("baseToken") or {}).get("address") != mint:
            continue
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        if liq > best_liq:
            best, best_liq = p.get("pairAddress") or "", liq
    return best


def poll_graduated(st: dict) -> list:
    """New swaps in the pools of graduated coins, parsed like curve trades and flagged post_grad."""
    global _grad_last
    if time.time() - _grad_last < GRAD_POLL:
        return []
    _grad_last = time.time()
    found = []
    damm = st.setdefault("damm", {})
    last = st.setdefault("damm_last", {})
    for mint, dbc_pool in graduated_map().items():
        pool = damm.get(mint)
        if not pool:
            if time.time() - float(st.setdefault("damm_try", {}).get(mint, 0)) < 300:
                continue
            st["damm_try"][mint] = time.time()
            pool = damm_pool_for(mint, dbc_pool)
            if not pool:
                continue
            damm[mint] = pool
        until = last.get(pool, "")
        if until:
            sigs = new_signatures(pool, until)
        else:
            page = rpc("getSignaturesForAddress", [pool, {"limit": BACKFILL, "commitment": "confirmed"}]) or []
            sigs = list(reversed(page))
        if not sigs:
            continue
        ok = [x["signature"] for x in sigs if not x.get("err")]
        for sig, tx in zip(ok, fetch_txs(ok)):
            t = parse(tx, {mint: dbc_pool}, DAMM_AUTH) if tx else None
            if t and not t["created"]:
                found.append(dict(t, sig=sig, post_grad=True))
        last[pool] = sigs[-1]["signature"]
    return found


# ------------------------------------------------------------------- rpc --
_S = requests.Session()


def rpc(method: str, params: list, timeout: int = 15):
    r = _S.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout).json()
    if "error" in r:
        raise RuntimeError(f"{method}: {str(r['error'])[:160]}")
    return r.get("result")


def rpc_batch(calls: list, timeout: int = 25) -> list:
    if not calls:
        return []
    body = [{"jsonrpc": "2.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(calls)]
    try:
        res = _S.post(RPC, json=body, timeout=timeout).json()
    except Exception:
        res = None
    if isinstance(res, list):
        by = {x.get("id"): x.get("result") for x in res if isinstance(x, dict)}
        return [by.get(i) for i in range(len(calls))]
    out = []  # the RPC does not take batches: one by one
    for m, p in calls:
        try:
            out.append(rpc(m, p, timeout=15))
        except Exception:
            out.append(None)
    return out


def new_signatures(addr: str, until: str, cap: int = 400) -> list[dict]:
    """Signatures newer than `until`, oldest first."""
    got, before = [], None
    while len(got) < cap:
        opts = {"limit": 100, "commitment": "confirmed"}
        if until:
            opts["until"] = until
        if before:
            opts["before"] = before
        page = rpc("getSignaturesForAddress", [addr, opts]) or []
        got.extend(page)
        if len(page) < 100 or not until:
            break
        before = page[-1]["signature"]
    return list(reversed(got))


def fetch_txs(sigs: list[str]) -> list:
    out = []
    for i in range(0, len(sigs), 20):
        out.extend(rpc_batch([("getTransaction", [s, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0,
                                                       "commitment": "confirmed"}]) for s in sigs[i:i + 20]]))
    return out


# ----------------------------------------------------------------- parse --
def _amt(b) -> int:
    try:
        return int(((b or {}).get("uiTokenAmount") or {}).get("amount") or 0)
    except (TypeError, ValueError):
        return 0


def parse(tx: dict, mints: dict, pa: str) -> dict | None:
    """One Ferzan curve trade from a transaction, or None (pool creation without dev buy, fee claims, migration...)."""
    if not tx or not tx.get("meta") or tx["meta"].get("err"):
        return None
    meta = tx["meta"]
    keys = [k["pubkey"] if isinstance(k, dict) else k for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
    if not keys:
        return None
    payer = keys[0]
    pre = {b["accountIndex"]: b for b in meta.get("preTokenBalances") or []}
    post = {b["accountIndex"]: b for b in meta.get("postTokenBalances") or []}
    rows = []
    for idx in set(pre) | set(post):
        b = post.get(idx) or pre.get(idx)
        rows.append({"owner": b.get("owner") or "", "mint": b.get("mint") or "", "d": _amt(post.get(idx)) - _amt(pre.get(idx)),
                     "new": idx not in pre, "dec": int(((b.get("uiTokenAmount") or {}).get("decimals")) or 6)})
    # the curve's two vaults: owned by the DBC pool authority; if that is ever different, any one owner holding both
    # a Ferzan coin and wrapped SOL that moved in opposite directions
    owners = [pa] if pa and any(r["owner"] == pa for r in rows) else sorted({r["owner"] for r in rows if r["owner"] != payer})
    for owner in owners:
        base = [r for r in rows if r["owner"] == owner and r["mint"] in mints]
        quote = [r for r in rows if r["owner"] == owner and r["mint"] == WSOL]
        if len({r["mint"] for r in base}) != 1 or not quote:
            continue
        mint, dec = base[0]["mint"], base[0]["dec"]
        bd, qd = sum(r["d"] for r in base), sum(r["d"] for r in quote)
        created = any(r["new"] for r in base)
        if created:  # pool creation in this transaction: the dev buy is what the creator's wallet received
            td = sum(r["d"] for r in rows if r["owner"] == payer and r["mint"] == mint)
            if td <= 0 or qd <= 0:
                return None
            is_buy, tokens, native = True, td, qd
        elif bd < 0 < qd:
            is_buy, tokens, native = True, -bd, qd
        elif qd < 0 < bd:
            is_buy, tokens, native = False, bd, -qd
        else:
            return None
        t, n = tokens / 10**dec, native / 1e9
        return {"mint": mint, "pool": mints[mint], "trader": payer, "is_buy": is_buy, "tokens": t, "native": n,
                "price": n / t if t else 0.0, "slot": int(tx.get("slot") or 0), "ts": int(tx.get("blockTime") or time.time()),
                "created": created}
    return None


# ------------------------------------------------------------ pool state --
class PoolState:
    """Long-lived node helper (dbc/pool_state.mjs) for live price / reserve reads."""

    def __init__(self):
        self.p = None
        self.n = 0

    def _start(self):
        self.p = subprocess.Popen(["node", str(HERE / "dbc" / "pool_state.mjs"), RPC], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, cwd=str(HERE / "dbc"), bufsize=1)
        first = self.p.stdout.readline()
        if '"ready"' not in first:
            raise RuntimeError("pool helper did not start")

    def read(self, pools: list[str]) -> dict:
        if not pools:
            return {}
        for attempt in range(2):
            try:
                if self.p is None or self.p.poll() is not None:
                    self._start()
                self.n += 1
                self.p.stdin.write(json.dumps({"id": self.n, "pools": pools}) + "\n")
                self.p.stdin.flush()
                line = self.p.stdout.readline()
                return (json.loads(line) or {}).get("states") or {}
            except Exception as e:
                log.warning("pool helper: %s", str(e)[:120])
                try:
                    self.p.kill()
                except Exception:
                    pass
                self.p = None
        return {}


# ----------------------------------------------------------------- store --
def idx() -> sqlite3.Connection:
    c = sqlite3.connect(INDEX_DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA busy_timeout=30000")
    return c


def pool_map() -> dict:
    """mint -> pool for every Ferzan Solana curve we know."""
    try:
        with idx() as c:
            return {r["token"]: r["curve"] for r in c.execute("SELECT token, curve FROM curves WHERE chain = 'solana'")}
    except sqlite3.Error:
        return {}


def store(trades: list, ps: PoolState) -> None:
    if not trades:
        return
    with idx() as c:
        for t in trades:
            cur = c.execute("INSERT OR IGNORE INTO trades (chain, curve, block, ts, tx, log_index, trader, is_buy, native, tokens, price) "
                            "VALUES ('solana', ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
                            (t["pool"], t["slot"], t["ts"], t["sig"], t["trader"], 1 if t["is_buy"] else 0, t["native"], t["tokens"], t["price"]))
            if t.get("post_grad") and cur.rowcount and t["price"] > 0:
                # the curve is finished, sol_indexer no longer counts it: keep price, volume and counts moving from the pool
                c.execute("UPDATE curves SET price = ?, mcap = ?, volume = volume + ?, trades = trades + 1, buys = buys + ?, "
                          "sells = sells + ?, last_trade_ts = MAX(COALESCE(last_trade_ts, 0), ?) WHERE chain = 'solana' AND curve = ?",
                          (t["price"], t["price"] * SUPPLY, t["native"], 1 if t["is_buy"] else 0, 0 if t["is_buy"] else 1, t["ts"], t["pool"]))
                try:
                    c.execute("INSERT INTO sol_px (ts, pool, price) VALUES (?,?,?)", (t["ts"], t["pool"], t["price"]))
                except sqlite3.Error:
                    pass
    trades = [t for t in trades if not t.get("post_grad")]
    states = ps.read(sorted({t["pool"] for t in trades}))
    now = int(time.time())
    last = {}
    for t in trades:
        last[t["pool"]] = max(last.get(t["pool"], 0), t["ts"])
    with idx() as c:
        for pool, st in states.items():
            price = float(st.get("price_sol") or 0)
            if st.get("error") or price <= 0:
                continue
            c.execute("UPDATE curves SET price = ?, mcap = ?, real_eth = ?, last_trade_ts = MAX(COALESCE(last_trade_ts, 0), ?) "
                      "WHERE chain = 'solana' AND curve = ?",
                      (price, price * SUPPLY, str(int(st.get("quote_reserve") or 0) * SCALE), last.get(pool, now), pool))
            try:
                c.execute("INSERT INTO sol_px (ts, pool, price) VALUES (?,?,?)", (now, pool, price))
            except sqlite3.Error:
                pass  # sol_indexer creates the table on its first pass


# ------------------------------------------------------------------ main --
def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save_state(st: dict) -> None:
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(STATE)


def test(n: int) -> int:
    pa, mints = pool_authority(), pool_map()
    print(f"configs: {len(configs())} | pool authority: {pa or '?'} | Ferzan Solana curves known: {len(mints)}")
    total = found = 0
    for cfg in configs():
        sigs = [s["signature"] for s in (rpc("getSignaturesForAddress", [cfg, {"limit": n, "commitment": "confirmed"}]) or []) if not s.get("err")]
        txs = fetch_txs(sigs)
        total += len(sigs)
        for sig, tx in zip(sigs, txs):
            t = parse(tx, mints, pa)
            if t:
                found += 1
                print(f"  {'BUY ' if t['is_buy'] else 'SELL'} {t['native']:.4f} SOL  {t['tokens']:,.0f} tokens  "
                      f"{t['trader'][:4]}…{t['trader'][-4:]}  {'(launch + dev buy) ' if t['created'] else ''}{sig[:10]}…")
    print(f"parsed {found} trades from {total} transactions (the rest are launches without a dev buy, fee claims or migrations)")
    return 0


def run() -> int:
    if os.environ.get("SOL_TRADES_OFF") == "1":
        log.info("off (SOL_TRADES_OFF=1)")
        while True:
            time.sleep(3600)
    pa, ps = pool_authority(), PoolState()
    st = load_state()
    mints, map_ts = {}, 0.0
    pending: dict = {}  # sig -> (first_seen, tx) for coins the index does not know yet (just launched)
    log.info("watching %d Ferzan configs, poll %.1fs", len(configs()), POLL)
    while True:
        t0 = time.time()
        try:
            refreshed = time.time() - map_ts > 20
            if refreshed:
                mints, map_ts = pool_map(), time.time()
            found = []
            for cfg in configs():
                last = st.get(cfg, "")
                if not last:  # first start: begin from now, no backfill
                    top = rpc("getSignaturesForAddress", [cfg, {"limit": 1, "commitment": "confirmed"}]) or []
                    st[cfg] = top[0]["signature"] if top else ""
                    continue
                sigs = new_signatures(cfg, last)
                if not sigs:
                    continue
                ok = [s["signature"] for s in sigs if not s.get("err")]
                for sig, tx in zip(ok, fetch_txs(ok)):
                    if tx is None:
                        continue
                    t = parse(tx, mints, pa)
                    if t:
                        found.append(dict(t, sig=sig))
                    else:  # maybe a coin we have not indexed yet: retry for a few minutes
                        pending[sig] = (time.time(), tx)
                st[cfg] = sigs[-1]["signature"]
            if pending and refreshed:  # the coin list just refreshed
                for sig, (seen, tx) in list(pending.items()):
                    t = parse(tx, mints, pa)
                    if t:
                        found.append(dict(t, sig=sig))
                        pending.pop(sig, None)
            for sig, (seen, _tx) in list(pending.items()):
                if time.time() - seen > 180 or len(pending) > 500:
                    pending.pop(sig, None)
            try:
                found += poll_graduated(st)
            except Exception as e:
                log.warning("graduated pools: %s", str(e)[:160])
            if found:
                store(found, ps)
                log.info("%d trade(s): %s", len(found), ", ".join(f"{'buy' if t['is_buy'] else 'sell'} {t['native']:.3f} SOL"
                                                                   for t in found[:6]))
            st["heartbeat"] = int(time.time())
            save_state(st)
        except Exception as e:
            log.warning("pass failed: %s", str(e)[:200])
            time.sleep(3)
        time.sleep(max(0.2, POLL - (time.time() - t0)))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "test":
        sys.exit(test(int(sys.argv[2]) if len(sys.argv) > 2 else 30))
    sys.exit(run())
