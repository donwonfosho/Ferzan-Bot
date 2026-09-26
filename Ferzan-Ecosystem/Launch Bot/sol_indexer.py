"""
sol_indexer.py -- puts Ferzan's Solana (Meteora DBC) launches into the same curve index as the EVM
curves, so the feed, King of the Hill, 50%/90% + graduation alerts, X posts and the leaderboard cover
Solana too. Called from curve_indexer's loop; runs at most every SOL_POLL_SECONDS (default 45).

Amounts are stored in the index's 18-decimal convention (lamports * 1e9) so every existing
calculation (progress, "x / y SOL raised") works unchanged. Volume is the SOL that moved in/out of
the curve between polls (a floor on real volume); trades are confirmed transactions on the pool.
Read-only on chain. The only launch-DB write is recording each launch's pool address (curve_address).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("sol_indexer")
HERE = Path(__file__).resolve().parent
POLL = int(os.environ.get("SOL_POLL_SECONDS") or 45)
SCALE = 10**9  # lamports -> 18-decimal units
SUPPLY_TOKENS = 1_000_000_000  # Ferzan partner config: 1B supply
SCHEMA = ("CREATE TABLE IF NOT EXISTS sol_state (pool TEXT PRIMARY KEY, mint TEXT, last_sig TEXT, last_quote TEXT, seen_ts INTEGER);"
          "CREATE TABLE IF NOT EXISTS sol_vol (ts INTEGER, pool TEXT, vol REAL, trades INTEGER);"
          "CREATE INDEX IF NOT EXISTS idx_sol_vol_ts ON sol_vol (ts);")
_last_run = 0.0


def _ts(s) -> int:
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return 0


def _launches(launch_db: str) -> list:
    import sqlite3
    try:
        c = sqlite3.connect(f"file:{launch_db}?mode=ro", uri=True, timeout=30)
        rows = c.execute(
            "SELECT id, name, symbol, wallet_address, result_token_address, created_at, extra_params FROM launch_requests "
            "WHERE chain = 'solana' AND mode = 'meteora' AND status = 'confirmed' AND result_token_address IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 60").fetchall()
        c.close()
        return rows
    except sqlite3.Error as e:
        log.warning("launch db read failed: %s", e)
        return []


def _read_pools(items: list) -> list:
    config = (os.environ.get("METEORA_CONFIG") or "").strip()
    if not config or not items:
        return []
    rpc = (os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com").strip()
    try:
        p = subprocess.run(["node", str(HERE / "dbc" / "sol_pools.mjs")], input=json.dumps({"rpc": rpc, "config": config, "items": items}),
                           capture_output=True, text=True, timeout=240, cwd=str(HERE / "dbc"))
    except subprocess.TimeoutExpired:
        log.warning("sol_pools timed out")
        return []
    out = p.stdout or ""
    try:
        d = json.loads(out[out.find("{"):]) if "{" in out else {}
    except ValueError:
        d = {}
    if d.get("error") or p.returncode != 0:
        log.warning("sol_pools failed: %s", (d.get("error") or p.stderr or "")[-200:])
        return []
    return d.get("pools") or []


def run(idx_conn, launch_db: str, force: bool = False) -> int:
    """One pass. Returns how many pools were updated."""
    global _last_run
    if not force and time.time() - _last_run < POLL:
        return 0
    _last_run = time.time()
    rows = _launches(launch_db)
    if not rows:
        return 0
    with idx_conn() as c:
        c.executescript(SCHEMA)
        state = {r[1]: r for r in c.execute("SELECT pool, mint, last_sig, last_quote, seen_ts FROM sol_state")}
    items = [{"mint": r[4], "last_sig": (state.get(r[4]) or (None, None, ""))[2] or ""} for r in rows]
    pools = {p["mint"]: p for p in _read_pools(items)}
    now = int(time.time())
    n = 0
    for rid, name, symbol, wallet, mint, created, extra in rows:
        p = pools.get(mint)
        if not p or not p.get("found") or p.get("error"):
            continue
        pool, quote, thr = p["pool"], int(p.get("quote_reserve") or 0), int(p.get("threshold") or 0)
        price = float(p.get("price_sol") or 0)
        prev = state.get(mint)
        first = prev is None
        graduated = bool(p.get("migrated")) or (thr > 0 and quote >= thr)
        vol = (quote / 1e9) if first else abs(quote - int(prev[3] or 0)) / 1e9
        trades = max(0, int(p.get("new_sigs") or 0) - (1 if first else 0))  # first pass: don't count pool creation
        last_trade = int(p.get("newest_ts") or 0) if int(p.get("new_sigs") or 0) else None
        launched = _ts(created)
        with idx_conn() as c:
            c.execute(
                "INSERT INTO curves (chain, curve, token, creator, name, symbol, total_supply, curve_supply, grad_target, "
                "v_eth, v_token, start_time, real_eth, tokens_sold, price, mcap, launched_ts, launched_block, graduated) "
                "VALUES ('solana', ?, ?, ?, ?, ?, ?, '0', ?, '0', '0', ?, ?, '0', ?, ?, ?, NULL, 0) "
                "ON CONFLICT(chain, curve) DO UPDATE SET name = excluded.name, symbol = excluded.symbol, "
                "grad_target = excluded.grad_target, real_eth = excluded.real_eth, price = excluded.price, mcap = excluded.mcap",
                (pool, mint, wallet or "", name, symbol, str(SUPPLY_TOKENS * 10**18), str(thr * SCALE), launched,
                 str(quote * SCALE), price, price * SUPPLY_TOKENS, launched))
            c.execute("UPDATE curves SET volume = volume + ?, trades = trades + ?, "
                      "last_trade_ts = COALESCE(?, last_trade_ts) WHERE chain = 'solana' AND curve = ?",
                      (vol, trades, last_trade, pool))
            if not first and (vol > 0 or trades > 0):  # per-poll momentum for the Trending feed
                c.execute("INSERT INTO sol_vol (ts, pool, vol, trades) VALUES (?,?,?,?)", (now, pool, vol, trades))
                c.execute("DELETE FROM sol_vol WHERE ts < ?", (now - 3 * 86400,))
            if graduated:
                # already graduated when first seen: unknown time, so no stale "just graduated" alert / post
                c.execute("UPDATE curves SET graduated = 1, grad_native = ?, grad_ts = COALESCE(grad_ts, ?), "
                          "grad_notified = CASE WHEN ? THEN 1 ELSE grad_notified END WHERE chain = 'solana' AND curve = ? AND graduated = 0",
                          (thr / 1e9, 0 if first else now, 1 if first else 0, pool))
            if first:
                # milestones it had already passed before we started watching: record silently
                prog = (quote * 100.0 / thr) if thr else 0.0
                try:
                    for t in (50, 90):
                        if prog >= t:
                            c.execute("INSERT OR IGNORE INTO alerts_sent (chain, curve, kind, ts) VALUES ('solana', ?, ?, ?)",
                                      (pool, f"p{t}", now))
                except Exception:
                    pass
            c.execute("INSERT INTO sol_state (pool, mint, last_sig, last_quote, seen_ts) VALUES (?,?,?,?,?) "
                      "ON CONFLICT(pool) DO UPDATE SET last_sig = excluded.last_sig, last_quote = excluded.last_quote, seen_ts = excluded.seen_ts",
                      (pool, mint, p.get("newest_sig") or (prev[2] if prev else ""), str(quote), now))
        try:  # remember the pool on the launch itself (creator alerts, images and descriptions key on it)
            if json.loads(extra or "{}").get("curve_address") != pool:
                import launch_bot_db
                launch_bot_db.set_curve_address(rid, pool)
        except Exception as e:
            log.info("could not record pool for %s: %s", mint, e)
        n += 1
    return n
