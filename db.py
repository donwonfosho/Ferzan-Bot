"""
SQLite persistence.

Keeps Claude's price-alert table and adds paper-trading accounts,
positions, watchlists, and a trade journal.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import sqlite3

DB_PATH = Path(os.getenv("DB_PATH", "/opt/ferzan/app/ferzan.db"))

PAPER_STARTING_BALANCE = float(os.getenv("PAPER_STARTING_BALANCE", "10000"))
DEFAULT_SIZE_PCT = float(os.getenv("DEFAULT_SIZE_PCT", "5"))
MIN_CONFLUENCE = int(os.getenv("MIN_CONFLUENCE", "62"))
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "8"))


@contextmanager
def get_conn():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                coin_id TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('above', 'below')),
                target_price REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                paper_cash REAL NOT NULL,
                starting_equity REAL NOT NULL,
                size_pct REAL NOT NULL,
                min_confluence INTEGER NOT NULL,
                max_daily_loss_pct REAL NOT NULL,
                alerts_on INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                query TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                entry REAL NOT NULL,
                stop REAL,
                take REAL,
                opened_at INTEGER NOT NULL,
                closed_at INTEGER,
                exit_price REAL,
                pnl REAL,
                reason TEXT,
                signal_json TEXT
            );

            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                kind TEXT NOT NULL,
                body TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watchlist (
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                PRIMARY KEY(user_id, query)
            );

            CREATE TABLE IF NOT EXISTS user_wallets (
                user_id INTEGER PRIMARY KEY,
                sol_pub TEXT NOT NULL,
                sol_key TEXT NOT NULL,
                evm_pub TEXT NOT NULL,
                evm_key TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS last_signal (
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                score INTEGER NOT NULL,
                sent_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, query)
            );

            CREATE TABLE IF NOT EXISTS watched_wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chain TEXT NOT NULL,
                address TEXT NOT NULL,
                label TEXT,
                cursor TEXT,
                created_at INTEGER NOT NULL,
                UNIQUE(user_id, chain, address)
            );

            CREATE TABLE IF NOT EXISTS fee_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                kind TEXT NOT NULL,
                notional_usd REAL NOT NULL,
                fee_bps INTEGER NOT NULL,
                fee_usd REAL NOT NULL,
                note TEXT
            );

            CREATE TABLE IF NOT EXISTS user_flags (
                user_id INTEGER NOT NULL,
                flag TEXT NOT NULL,
                onoff INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY(user_id, flag)
            );

            CREATE TABLE IF NOT EXISTS lp_marks (
                user_id INTEGER NOT NULL,
                mint TEXT NOT NULL,
                liq_usd REAL NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, mint)
            );

            CREATE TABLE IF NOT EXISTS live_exits (
                user_id INTEGER NOT NULL,
                mint TEXT NOT NULL,
                tp_pct REAL,
                sl_pct REAL,
                PRIMARY KEY(user_id, mint)
            );

            CREATE TABLE IF NOT EXISTS live_basis (
                user_id INTEGER NOT NULL,
                mint TEXT NOT NULL,
                cost_usd REAL NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(user_id, mint)
            );

            CREATE TABLE IF NOT EXISTS equity_marks (
                user_id INTEGER NOT NULL,
                marked_at INTEGER NOT NULL,
                equity REAL NOT NULL,
                peak REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                chain TEXT,
                usd REAL NOT NULL,
                min_liq REAL NOT NULL DEFAULT 0,
                min_score INTEGER NOT NULL DEFAULT 0,
                max_age_h REAL,
                require_long INTEGER NOT NULL DEFAULT 1,
                block_veto INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'armed',
                result TEXT,
                created_at INTEGER NOT NULL,
                finished_at INTEGER
            );
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "peak_equity" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN peak_equity REAL")
        if "drawdown_alert_pct" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN drawdown_alert_pct REAL DEFAULT 12")
        if "last_query" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN last_query TEXT")
        if "copy_paper" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN copy_paper INTEGER DEFAULT 0")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                side TEXT NOT NULL,
                target REAL NOT NULL,
                usd REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.commit()


def add_alert(chat_id: int, symbol: str, coin_id: str, direction: str, target_price: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO alerts (chat_id, symbol, coin_id, direction, target_price) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, symbol.upper(), coin_id, direction, target_price),
        )
        conn.commit()
        return cur.lastrowid


def list_alerts(chat_id: int, active_only: bool = True):
    query = "SELECT * FROM alerts WHERE chat_id = ?"
    params: list[Any] = [chat_id]
    if active_only:
        query += " AND active = 1"
    query += " ORDER BY id DESC"
    with get_conn() as conn:
        return conn.execute(query, params).fetchall()


def get_all_active_alerts():
    with get_conn() as conn:
        return conn.execute("SELECT * FROM alerts WHERE active = 1").fetchall()


def deactivate_alert(alert_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE alerts SET active = 0 WHERE id = ?", (alert_id,))
        conn.commit()


def delete_alert(alert_id: int, chat_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM alerts WHERE id = ? AND chat_id = ?", (alert_id, chat_id)
        )
        conn.commit()
        return cur.rowcount > 0


def ensure_user(user_id: int, username: str | None) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row:
            return dict(row)
        conn.execute(
            """
            INSERT INTO users (
                user_id, username, paper_cash, starting_equity, size_pct,
                min_confluence, max_daily_loss_pct, alerts_on, created_at,
                peak_equity, drawdown_alert_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 12)
            """,
            (
                user_id,
                username,
                PAPER_STARTING_BALANCE,
                PAPER_STARTING_BALANCE,
                DEFAULT_SIZE_PCT,
                MIN_CONFLUENCE,
                MAX_DAILY_LOSS_PCT,
                int(time.time()),
                PAPER_STARTING_BALANCE,
            ),
        )
        for q in ("SOL", "BTC", "ETH", "JUP", "WIF"):
            conn.execute(
                "INSERT OR IGNORE INTO watchlist (user_id, query) VALUES (?, ?)",
                (user_id, q),
            )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row)


def get_user(user_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def update_user(user_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [user_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE users SET {cols} WHERE user_id = ?", vals)
        conn.commit()


def list_alert_users() -> list[int]:
    with get_conn() as conn:
        return [r[0] for r in conn.execute("SELECT user_id FROM users WHERE alerts_on = 1")]


def watchlist_of(user_id: int) -> list[str]:
    with get_conn() as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT query FROM watchlist WHERE user_id = ?", (user_id,)
            )
        ]


def add_watch(user_id: int, query: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist (user_id, query) VALUES (?, ?)",
            (user_id, query.upper().strip()),
        )
        conn.commit()


def remove_watch(user_id: int, query: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM watchlist WHERE user_id = ? AND query = ?",
            (user_id, query.upper().strip()),
        )
        conn.commit()


def open_position(
    user_id: int,
    symbol: str,
    query: str,
    side: str,
    qty: float,
    entry: float,
    stop: float | None,
    take: float | None,
    reason: str,
    signal: dict[str, Any],
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO positions (
                user_id, symbol, query, side, qty, entry, stop, take,
                opened_at, reason, signal_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                symbol,
                query,
                side,
                qty,
                entry,
                stop,
                take,
                int(time.time()),
                reason,
                json.dumps(signal),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def close_position(pos_id: int, exit_price: float, pnl: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE positions SET closed_at = ?, exit_price = ?, pnl = ? WHERE id = ?",
            (int(time.time()), exit_price, pnl, pos_id),
        )
        conn.commit()


def get_position(pos_id: int, user_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM positions WHERE id = ? AND user_id = ?",
            (pos_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def open_positions(user_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM positions WHERE user_id = ? AND closed_at IS NULL ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def all_open_positions() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM positions WHERE closed_at IS NULL").fetchall()
        return [dict(r) for r in rows]


def recent_closed(user_id: int, limit: int = 8) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM positions
            WHERE user_id = ? AND closed_at IS NOT NULL
            ORDER BY closed_at DESC LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def realized_today(user_id: int) -> float:
    start = int(time.time()) - (int(time.time()) % 86400)
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(pnl), 0) FROM positions
            WHERE user_id = ? AND closed_at >= ?
            """,
            (user_id, start),
        ).fetchone()
        return float(row[0] or 0)


def add_journal(user_id: int, kind: str, body: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO journal (user_id, created_at, kind, body) VALUES (?, ?, ?, ?)",
            (user_id, int(time.time()), kind, body),
        )
        conn.commit()


def recent_journal(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM journal WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def should_resend_signal(user_id: int, query: str, score: int, cooldown_s: int = 45 * 60) -> bool:
    now = int(time.time())
    with get_conn() as conn:
        row = conn.execute(
            "SELECT score, sent_at FROM last_signal WHERE user_id = ? AND query = ?",
            (user_id, query),
        ).fetchone()
        if row and now - int(row["sent_at"]) < cooldown_s and int(row["score"]) >= score - 3:
            return False
        conn.execute(
            """
            INSERT INTO last_signal (user_id, query, score, sent_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, query) DO UPDATE SET score = excluded.score, sent_at = excluded.sent_at
            """,
            (user_id, query, score, now),
        )
        conn.commit()
        return True


def add_watched_wallet(user_id: int, chain: str, address: str, label: str | None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO watched_wallets (user_id, chain, address, label, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, chain, address, label, int(time.time())),
        )
        conn.commit()
        if cur.lastrowid:
            return int(cur.lastrowid)
        row = conn.execute(
            "SELECT id FROM watched_wallets WHERE user_id = ? AND chain = ? AND address = ?",
            (user_id, chain, address),
        ).fetchone()
        return int(row[0]) if row else 0


def list_watched_wallets(user_id: int | None = None) -> list[dict[str, Any]]:
    with get_conn() as conn:
        if user_id is None:
            rows = conn.execute("SELECT * FROM watched_wallets ORDER BY id").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM watched_wallets WHERE user_id = ? ORDER BY id",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def get_watched_wallet(wallet_id: int, user_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM watched_wallets WHERE id = ? AND user_id = ?",
            (wallet_id, user_id),
        ).fetchone()
        return dict(row) if row else None


def delete_watched_wallet(wallet_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM watched_wallets WHERE id = ? AND user_id = ?",
            (wallet_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def set_wallet_cursor(wallet_id: int, cursor: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE watched_wallets SET cursor = ? WHERE id = ?",
            (cursor, wallet_id),
        )
        conn.commit()


def add_fee(user_id: int, kind: str, notional_usd: float, fee_bps: int, fee_usd: float, note: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO fee_ledger (user_id, created_at, kind, notional_usd, fee_bps, fee_usd, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, int(time.time()), kind, notional_usd, fee_bps, fee_usd, note),
        )
        conn.commit()


def fee_totals(user_id: int | None = None) -> dict[str, float]:
    with get_conn() as conn:
        if user_id is None:
            row = conn.execute(
                "SELECT COALESCE(SUM(fee_usd),0), COALESCE(SUM(notional_usd),0), COUNT(*) FROM fee_ledger"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(fee_usd),0), COALESCE(SUM(notional_usd),0), COUNT(*) FROM fee_ledger WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return {"fee_usd": float(row[0]), "notional_usd": float(row[1]), "count": int(row[2])}


def recent_fees(limit: int = 12, user_id: int | None = None) -> list[dict[str, Any]]:
    with get_conn() as conn:
        if user_id is None:
            rows = conn.execute(
                "SELECT * FROM fee_ledger ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM fee_ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]


def record_equity_mark(user_id: int, equity: float, peak: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO equity_marks (user_id, marked_at, equity, peak) VALUES (?, ?, ?, ?)",
            (user_id, int(time.time()), equity, peak),
        )
        conn.commit()


def last_equity_mark(user_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM equity_marks WHERE user_id = ? ORDER BY marked_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict[str, Any]]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM users").fetchall()]


def add_snipe(
    user_id: int,
    query: str,
    chain: str,
    usd: float,
    min_liq: float,
    min_score: int,
    max_age_h: float | None,
    require_long: int,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO snipes (
                user_id, query, chain, usd, min_liq, min_score, max_age_h,
                require_long, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'armed', ?)
            """,
            (
                user_id,
                query,
                chain,
                usd,
                min_liq,
                min_score,
                max_age_h,
                require_long,
                int(time.time()),
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def active_snipes(user_id: int | None = None) -> list[dict[str, Any]]:
    with get_conn() as conn:
        if user_id is None:
            rows = conn.execute(
                "SELECT * FROM snipes WHERE status = 'armed' ORDER BY id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM snipes WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def finish_snipe(snipe_id: int, status: str, result: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE snipes SET status = ?, result = ?, finished_at = ? WHERE id = ?",
            (status, result[:500], int(time.time()), snipe_id),
        )
        conn.commit()


def cancel_snipe(snipe_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE snipes SET status = 'cancelled', finished_at = ? "
            "WHERE id = ? AND user_id = ? AND status = 'armed'",
            (int(time.time()), snipe_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def add_limit(user_id: int, query: str, side: str, target: float, usd: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO limits (user_id, query, side, target, usd, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?)",
            (user_id, query, side, target, usd, int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)


def open_limits(user_id: int | None = None) -> list[dict[str, Any]]:
    with get_conn() as conn:
        if user_id is None:
            rows = conn.execute("SELECT * FROM limits WHERE status = 'open'").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM limits WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def finish_limit(limit_id: int, status: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE limits SET status = ? WHERE id = ?", (status, limit_id))
        conn.commit()


def cancel_limit(limit_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE limits SET status = 'cancelled' WHERE id = ? AND user_id = ? AND status = 'open'",
            (limit_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_user_wallet(user_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM user_wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def save_user_wallet(user_id: int, sol_pub: str, sol_key: str, evm_pub: str, evm_key: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_wallets (user_id, sol_pub, sol_key, evm_pub, evm_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                sol_pub = excluded.sol_pub,
                sol_key = excluded.sol_key,
                evm_pub = excluded.evm_pub,
                evm_key = excluded.evm_key
            """,
            (user_id, sol_pub, sol_key, evm_pub, evm_key, int(time.time())),
        )
        conn.commit()


def add_live_cost(user_id: int, mint: str, usd: float) -> None:
    mint = (mint or "").strip()
    if not mint:
        return
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO live_basis (user_id, mint, cost_usd, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, mint) DO UPDATE SET
                cost_usd = live_basis.cost_usd + excluded.cost_usd,
                updated_at = excluded.updated_at
            """,
            (user_id, mint, float(usd), int(time.time())),
        )
        conn.commit()


def live_mints(user_id: int) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT mint FROM live_basis WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
        return [str(r["mint"]) for r in rows]


def live_cost(user_id: int, mint: str) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cost_usd FROM live_basis WHERE user_id = ? AND mint = ?",
            (user_id, mint),
        ).fetchone()
        return float(row["cost_usd"]) if row else 0.0


def set_live_exit(user_id: int, mint: str, tp_pct: float | None = None, sl_pct: float | None = None) -> None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT tp_pct, sl_pct FROM live_exits WHERE user_id = ? AND mint = ?",
            (user_id, mint),
        ).fetchone()
        tp = tp_pct if tp_pct is not None else (float(row["tp_pct"]) if row and row["tp_pct"] is not None else None)
        sl = sl_pct if sl_pct is not None else (float(row["sl_pct"]) if row and row["sl_pct"] is not None else None)
        conn.execute(
            """
            INSERT INTO live_exits (user_id, mint, tp_pct, sl_pct)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, mint) DO UPDATE SET tp_pct = excluded.tp_pct, sl_pct = excluded.sl_pct
            """,
            (user_id, mint, tp, sl),
        )
        conn.commit()


def get_live_exit(user_id: int, mint: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM live_exits WHERE user_id = ? AND mint = ?",
            (user_id, mint),
        ).fetchone()
        return dict(row) if row else None


def list_live_exits() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM live_exits").fetchall()]


def clear_live_exit(user_id: int, mint: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM live_exits WHERE user_id = ? AND mint = ?", (user_id, mint))
        conn.commit()


def flag_on(user_id: int, flag: str, default: int = 1) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT onoff FROM user_flags WHERE user_id = ? AND flag = ?",
            (user_id, flag),
        ).fetchone()
        if row is None:
            return bool(default)
        return int(row["onoff"]) == 1


def set_flag(user_id: int, flag: str, on: bool) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO user_flags (user_id, flag, onoff) VALUES (?, ?, ?)
            ON CONFLICT(user_id, flag) DO UPDATE SET onoff = excluded.onoff
            """,
            (user_id, flag, 1 if on else 0),
        )
        conn.commit()


def lp_mark(user_id: int, mint: str) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT liq_usd FROM lp_marks WHERE user_id = ? AND mint = ?",
            (user_id, mint),
        ).fetchone()
        return float(row["liq_usd"]) if row else 0.0


def set_lp_mark(user_id: int, mint: str, liq: float) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO lp_marks (user_id, mint, liq_usd, updated_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, mint) DO UPDATE SET liq_usd = excluded.liq_usd, updated_at = excluded.updated_at
            """,
            (user_id, mint, float(liq), int(time.time())),
        )
        conn.commit()


def clear_live_cost(user_id: int, mint: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM live_basis WHERE user_id = ? AND mint = ?",
            (user_id, mint),
        )
        conn.commit()
