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

            CREATE TABLE IF NOT EXISTS feed_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT,
                added_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sponsored (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                ca TEXT,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS feed_binds (
                chat_id INTEGER NOT NULL,
                chain TEXT NOT NULL,
                title TEXT,
                added_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, chain)
            );

            CREATE TABLE IF NOT EXISTS buy_limits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mint TEXT NOT NULL,
                chain TEXT,
                usd REAL NOT NULL,
                target_px REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'armed'
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
        if "buy_usd" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN buy_usd REAL DEFAULT 25")
        if "buy_slip_pct" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN buy_slip_pct REAL DEFAULT 10")
        if "sell_slip_pct" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN sell_slip_pct REAL DEFAULT 10")
        if "auto_buy_usd" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN auto_buy_usd REAL DEFAULT 0")
        if "referred_by" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
        if "discount_until" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN discount_until INTEGER DEFAULT 0")
        if "trail_pct" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN trail_pct REAL DEFAULT 0")
        if "lp_drop_pct" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN lp_drop_pct REAL DEFAULT 50")
        if "lp_floor_usd" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN lp_floor_usd REAL DEFAULT 500")
        if "stake_units" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN stake_units REAL DEFAULT 0")
        exit_cols = {r[1] for r in conn.execute("PRAGMA table_info(live_exits)").fetchall()}
        if "trail_pct" not in exit_cols:
            conn.execute("ALTER TABLE live_exits ADD COLUMN trail_pct REAL")
        if "peak_pct" not in exit_cols:
            conn.execute("ALTER TABLE live_exits ADD COLUMN peak_pct REAL DEFAULT 0")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS wallet_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                sol_pub TEXT NOT NULL,
                sol_key TEXT NOT NULL,
                evm_pub TEXT NOT NULL,
                evm_key TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wallet_slots_user ON wallet_slots(user_id)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS wallet_active (user_id INTEGER PRIMARY KEY, slot_id INTEGER NOT NULL)"
        )
        # Every pre-multi-wallet user becomes slot "Main" (keys copied as-is,
        # still encrypted). Runs once per user: skipped when they have slots.
        conn.execute(
            """
            INSERT INTO wallet_slots (user_id, label, sol_pub, sol_key, evm_pub, evm_key, created_at)
            SELECT uw.user_id, 'Main', uw.sol_pub, uw.sol_key, uw.evm_pub, uw.evm_key, uw.created_at
            FROM user_wallets uw
            WHERE NOT EXISTS (SELECT 1 FROM wallet_slots ws WHERE ws.user_id = uw.user_id)
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO wallet_active (user_id, slot_id)
            SELECT ws.user_id, MIN(ws.id) FROM wallet_slots ws GROUP BY ws.user_id
            """
        )
        ww_cols = {r[1] for r in conn.execute("PRAGMA table_info(watched_wallets)").fetchall()}
        if "copy_on" not in ww_cols:
            # Every watch — old and new — starts alert-only. Copy is opt-in per
            # wallet: the pre-v2 copy path never actually fired (it couldn't
            # extract a mint), so nobody was "already copying" and silently
            # arming live buys on existing watches would spend real money.
            conn.execute("ALTER TABLE watched_wallets ADD COLUMN copy_on INTEGER DEFAULT 0")
        if "copy_usd" not in ww_cols:
            conn.execute("ALTER TABLE watched_wallets ADD COLUMN copy_usd REAL DEFAULT 0")
        if "copy_sells" not in ww_cols:
            conn.execute("ALTER TABLE watched_wallets ADD COLUMN copy_sells INTEGER DEFAULT 0")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS copy_fills (
                user_id INTEGER NOT NULL,
                wallet_id INTEGER NOT NULL,
                mint TEXT NOT NULL,
                bought_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, wallet_id, mint)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feed_failures (
                chat_id INTEGER PRIMARY KEY,
                fails INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referral_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                from_user INTEGER,
                created_at INTEGER NOT NULL,
                volume_usd REAL NOT NULL,
                share_usd REAL NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chain_trade (
                user_id INTEGER NOT NULL,
                chain TEXT NOT NULL,
                buy_slip REAL NOT NULL DEFAULT 10,
                sell_slip REAL NOT NULL DEFAULT 10,
                gas REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, chain)
            )
            """
        )
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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS native_marks (
                chain TEXT PRIMARY KEY,
                price REAL NOT NULL,
                ts INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS live_tp_rungs (
                user_id INTEGER NOT NULL,
                mint TEXT NOT NULL,
                pct REAL NOT NULL,
                sell_pct REAL NOT NULL,
                hit INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (user_id, mint, pct)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS curated_wallets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chain TEXT NOT NULL,
                address TEXT NOT NULL,
                label TEXT NOT NULL,
                note TEXT,
                added_by INTEGER,
                added_at INTEGER NOT NULL,
                UNIQUE(chain, address)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dca_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                mint TEXT NOT NULL,
                chain TEXT NOT NULL DEFAULT '',
                usd_per_buy REAL NOT NULL,
                interval_seconds INTEGER NOT NULL,
                next_run_at INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL,
                last_run_at INTEGER,
                UNIQUE(user_id, mint)
            )
            """
        )
        _init_v4(conn)
        _init_v5(conn)
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


_COPY_FIELDS = {"copy_on", "copy_usd", "copy_sells"}


def set_wallet_copy(wallet_id: int, user_id: int, **fields: Any) -> bool:
    fields = {k: v for k, v in fields.items() if k in _COPY_FIELDS}
    if not fields:
        return False
    sets = ", ".join(f"{k} = ?" for k in fields)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE watched_wallets SET {sets} WHERE id = ? AND user_id = ?",
            (*fields.values(), wallet_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0


def record_copy_fill(user_id: int, wallet_id: int, mint: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO copy_fills (user_id, wallet_id, mint, bought_at) VALUES (?, ?, ?, ?)",
            (user_id, wallet_id, mint, int(time.time())),
        )
        conn.commit()


def has_copy_fill(user_id: int, wallet_id: int, mint: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM copy_fills WHERE user_id = ? AND wallet_id = ? AND mint = ?",
            (user_id, wallet_id, mint),
        ).fetchone()
        return row is not None


def clear_copy_fill(user_id: int, wallet_id: int, mint: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM copy_fills WHERE user_id = ? AND wallet_id = ? AND mint = ?",
            (user_id, wallet_id, mint),
        )
        conn.commit()


def clear_copy_fills_for_wallet(user_id: int, wallet_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM copy_fills WHERE user_id = ? AND wallet_id = ?", (user_id, wallet_id))
        conn.commit()


def note_feed_failure(chat_id: int, error: str) -> int:
    """Bump the consecutive-failure count for a feed chat; returns the new count."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO feed_failures (chat_id, fails, last_error, updated_at)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                fails = fails + 1, last_error = excluded.last_error, updated_at = excluded.updated_at
            """,
            (chat_id, (error or "")[:200], int(time.time())),
        )
        conn.commit()
        row = conn.execute("SELECT fails FROM feed_failures WHERE chat_id = ?", (chat_id,)).fetchone()
        return int(row[0]) if row else 1


def clear_feed_failure(chat_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM feed_failures WHERE chat_id = ?", (chat_id,))
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
    """Legacy single-wallet write (first wallet creation). Also records it as
    a slot so multi-wallet sees it; keys already in a slot are never dropped."""
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT id FROM wallet_slots WHERE user_id = ? AND sol_pub = ? AND evm_pub = ?",
            (user_id, sol_pub, evm_pub),
        ).fetchone()
        if not exists:
            n = conn.execute("SELECT COUNT(*) FROM wallet_slots WHERE user_id = ?", (user_id,)).fetchone()[0]
            cur = conn.execute(
                """
                INSERT INTO wallet_slots (user_id, label, sol_pub, sol_key, evm_pub, evm_key, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, "Main" if n == 0 else f"Wallet {n + 1}", sol_pub, sol_key, evm_pub, evm_key, int(time.time())),
            )
            sid = cur.lastrowid
        else:
            sid = exists[0]
        conn.execute(
            "INSERT INTO wallet_active (user_id, slot_id) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET slot_id = excluded.slot_id",
            (user_id, sid),
        )
        conn.commit()
    _mirror_active(user_id, sol_pub, sol_key, evm_pub, evm_key)


MAX_WALLETS = 10


def _mirror_active(user_id: int, sol_pub: str, sol_key: str, evm_pub: str, evm_key: str) -> None:
    """user_wallets always holds the ACTIVE slot, so every existing reader
    (get_user_wallet / user_wallets.secrets) transparently uses it."""
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


def list_wallet_slots(user_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        active = conn.execute("SELECT slot_id FROM wallet_active WHERE user_id = ?", (user_id,)).fetchone()
        rows = conn.execute(
            "SELECT * FROM wallet_slots WHERE user_id = ? ORDER BY id", (user_id,)
        ).fetchall()
    aid = active[0] if active else None
    return [dict(r) | {"active": r["id"] == aid} for r in rows]


def get_wallet_slot(user_id: int, slot_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM wallet_slots WHERE id = ? AND user_id = ?", (slot_id, user_id)
        ).fetchone()
        return dict(row) if row else None


def add_wallet_slot(user_id: int, label: str, sol_pub: str, sol_key: str, evm_pub: str, evm_key: str) -> int:
    with get_conn() as conn:
        n = conn.execute("SELECT COUNT(*) FROM wallet_slots WHERE user_id = ?", (user_id,)).fetchone()[0]
        if n >= MAX_WALLETS:
            raise ValueError(f"Wallet limit reached ({MAX_WALLETS}).")
        cur = conn.execute(
            """
            INSERT INTO wallet_slots (user_id, label, sol_pub, sol_key, evm_pub, evm_key, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, (label or f"Wallet {n + 1}")[:24], sol_pub, sol_key, evm_pub, evm_key, int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)


def set_active_wallet(user_id: int, slot_id: int) -> dict[str, Any] | None:
    slot = get_wallet_slot(user_id, slot_id)
    if not slot:
        return None
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO wallet_active (user_id, slot_id) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET slot_id = excluded.slot_id",
            (user_id, slot_id),
        )
        conn.commit()
    _mirror_active(user_id, slot["sol_pub"], slot["sol_key"], slot["evm_pub"], slot["evm_key"])
    return slot


def rename_wallet_slot(user_id: int, slot_id: int, label: str) -> bool:
    label = (label or "").strip()[:24]
    if not label:
        return False
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE wallet_slots SET label = ? WHERE id = ? AND user_id = ?", (label, slot_id, user_id)
        )
        conn.commit()
        return cur.rowcount > 0


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


def user_volume_usd(user_id: int, days: int = 30) -> float:
    since = int(time.time()) - int(days) * 86400
    with get_conn() as conn:
        row = conn.execute(
            # level 1 only: L2/L3 rows repeat the same trade's volume
            "SELECT COALESCE(SUM(volume_usd),0) FROM referral_ledger "
            "WHERE from_user = ? AND created_at >= ? AND COALESCE(level, 1) = 1",
            (int(user_id), since),
        ).fetchone()
        live = conn.execute(
            "SELECT COALESCE(SUM(cost_usd),0) FROM live_basis WHERE user_id = ?",
            (int(user_id),),
        ).fetchone()
    return float(row[0] or 0) + float(live[0] or 0)


def set_stake_units(user_id: int, units: float) -> None:
    update_user(int(user_id), stake_units=max(0.0, float(units)))


def live_cost(user_id: int, mint: str) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cost_usd FROM live_basis WHERE user_id = ? AND mint = ?",
            (user_id, mint),
        ).fetchone()
        return float(row["cost_usd"]) if row else 0.0


def set_live_exit(
    user_id: int,
    mint: str,
    tp_pct: float | None = None,
    sl_pct: float | None = None,
    trail_pct: float | None = None,
    peak_pct: float | None = None,
    peak_px: float | None = None,
) -> None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT tp_pct, sl_pct, trail_pct, peak_pct, peak_px FROM live_exits WHERE user_id = ? AND mint = ?",
            (user_id, mint),
        ).fetchone()
        # sqlite3.Row has no .get() -- convert first (the old code raised
        # AttributeError here whenever a row existed, e.g. on every peak update).
        cur = dict(row) if row else {}
        tp = tp_pct if tp_pct is not None else cur.get("tp_pct")
        sl = sl_pct if sl_pct is not None else cur.get("sl_pct")
        tr = trail_pct if trail_pct is not None else cur.get("trail_pct")
        # NULL peak_px = trailing stop starts from the next price read.
        pk = peak_pct if peak_pct is not None else cur.get("peak_pct")
        ppx = peak_px if peak_px is not None else cur.get("peak_px")
        conn.execute(
            """
            INSERT INTO live_exits (user_id, mint, tp_pct, sl_pct, trail_pct, peak_pct, peak_px)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, mint) DO UPDATE SET
                tp_pct = excluded.tp_pct,
                sl_pct = excluded.sl_pct,
                trail_pct = excluded.trail_pct,
                peak_pct = excluded.peak_pct,
                peak_px = excluded.peak_px
            """,
            (user_id, mint, tp, sl, tr, pk, ppx),
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


def set_tp_ladder(user_id: int, mint: str, rungs: list[tuple[float, float]]) -> None:
    """Replaces this user+mint's whole ladder. rungs is [(pct_gain, sell_pct), ...]."""
    with get_conn() as conn:
        conn.execute("DELETE FROM live_tp_rungs WHERE user_id = ? AND mint = ?", (user_id, mint))
        now = int(time.time())
        for pct, sell_pct in rungs:
            conn.execute(
                """
                INSERT INTO live_tp_rungs (user_id, mint, pct, sell_pct, hit, created_at)
                VALUES (?, ?, ?, ?, 0, ?)
                """,
                (user_id, mint, float(pct), float(sell_pct), now),
            )
        conn.commit()


def list_tp_rungs(user_id: int, mint: str) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM live_tp_rungs WHERE user_id = ? AND mint = ? ORDER BY pct",
            (user_id, mint),
        ).fetchall()
        return [dict(r) for r in rows]


def list_all_tp_rungs() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM live_tp_rungs WHERE hit = 0 ORDER BY user_id, mint, pct"
        ).fetchall()
        return [dict(r) for r in rows]


def mark_tp_rung_hit(user_id: int, mint: str, pct: float) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE live_tp_rungs SET hit = 1 WHERE user_id = ? AND mint = ? AND pct = ?",
            (user_id, mint, float(pct)),
        )
        conn.commit()


def clear_tp_ladder(user_id: int, mint: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM live_tp_rungs WHERE user_id = ? AND mint = ?", (user_id, mint))
        conn.commit()


def add_curated_wallet(chain: str, address: str, label: str, note: str, added_by: int) -> bool:
    with get_conn() as conn:
        try:
            conn.execute(
                """
                INSERT INTO curated_wallets (chain, address, label, note, added_by, added_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chain, address, label, note, added_by, int(time.time())),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False


def list_curated_wallets(chain: str | None = None) -> list[dict[str, Any]]:
    with get_conn() as conn:
        if chain:
            rows = conn.execute(
                "SELECT * FROM curated_wallets WHERE chain = ? ORDER BY id", (chain,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM curated_wallets ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def remove_curated_wallet(wallet_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM curated_wallets WHERE id = ?", (wallet_id,))
        conn.commit()
        return cur.rowcount > 0


def set_dca_plan(user_id: int, mint: str, chain: str, usd_per_buy: float, interval_seconds: int) -> None:
    """Create or replace this user's DCA plan for a mint. Reactivates a
    previously-cancelled plan for the same mint instead of duplicating it."""
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO dca_plans
                (user_id, mint, chain, usd_per_buy, interval_seconds, next_run_at, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(user_id, mint) DO UPDATE SET
                chain = excluded.chain,
                usd_per_buy = excluded.usd_per_buy,
                interval_seconds = excluded.interval_seconds,
                next_run_at = excluded.next_run_at,
                active = 1
            """,
            (user_id, mint, chain, float(usd_per_buy), int(interval_seconds), now + int(interval_seconds), now),
        )
        conn.commit()


def clear_dca_plan(user_id: int, mint: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE dca_plans SET active = 0 WHERE user_id = ? AND mint = ? AND active = 1",
            (user_id, mint),
        )
        conn.commit()
        return cur.rowcount > 0


def list_dca_plans(user_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM dca_plans WHERE user_id = ? AND active = 1 ORDER BY id",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_due_dca_plans(now_ts: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM dca_plans WHERE active = 1 AND next_run_at <= ? ORDER BY id",
            (now_ts,),
        ).fetchall()
        return [dict(r) for r in rows]


def advance_dca_plan(plan_id: int, next_run_at: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE dca_plans SET next_run_at = ?, last_run_at = ? WHERE id = ?",
            (int(next_run_at), int(time.time()), plan_id),
        )
        conn.commit()


def add_feed_chat(chat_id: int, title: str = "", chain: str = "*") -> None:
    chain = (chain or "*").lower()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO feed_binds (chat_id, chain, title, added_at)
            VALUES (?, ?, ?, ?)
            """,
            (int(chat_id), chain, title or "", int(time.time())),
        )
        # Re-binding a chat is an explicit "this works now" — un-mute it.
        conn.execute("DELETE FROM feed_failures WHERE chat_id = ?", (int(chat_id),))
        conn.commit()


def feed_fail_count(chat_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT fails FROM feed_failures WHERE chat_id = ?", (int(chat_id),)).fetchone()
        return int(row[0]) if row else 0


def feed_fail_info(chat_id: int) -> tuple[int, int]:
    """(consecutive failures, unix time of the last one)."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT fails, updated_at FROM feed_failures WHERE chat_id = ?", (int(chat_id),)
        ).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)


def migrate_feed_chat(old_chat_id: int, new_chat_id: int) -> None:
    """A group was upgraded to a supergroup: carry its feed binds to the new id."""
    with get_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO feed_binds (chat_id, chain, title, added_at)
            SELECT ?, chain, title, added_at FROM feed_binds WHERE chat_id = ?
            """,
            (int(new_chat_id), int(old_chat_id)),
        )
        conn.execute("DELETE FROM feed_binds WHERE chat_id = ?", (int(old_chat_id),))
        conn.execute("DELETE FROM feed_failures WHERE chat_id IN (?, ?)", (int(old_chat_id), int(new_chat_id)))
        conn.commit()


def drop_feed_chat(chat_id: int, chain: str | None = None) -> None:
    with get_conn() as conn:
        if chain:
            conn.execute(
                "DELETE FROM feed_binds WHERE chat_id = ? AND chain = ?",
                (int(chat_id), chain.lower()),
            )
        else:
            conn.execute("DELETE FROM feed_binds WHERE chat_id = ?", (int(chat_id),))
        conn.commit()


def get_chain_trade(user_id: int, chain: str) -> dict:
    cid = (chain or "sol").lower()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT buy_slip, sell_slip, gas FROM chain_trade WHERE user_id = ? AND chain = ?",
            (int(user_id), cid),
        ).fetchone()
    if not row:
        return {"buy_slip": 10.0, "sell_slip": 10.0, "gas": 0.0}
    return {
        "buy_slip": float(row["buy_slip"] or 10),
        "sell_slip": float(row["sell_slip"] or 10),
        "gas": float(row["gas"] or 0),
    }


def set_chain_trade(user_id: int, chain: str, **fields: float) -> dict:
    cur = get_chain_trade(user_id, chain)
    cur.update({k: float(v) for k, v in fields.items() if k in {"buy_slip", "sell_slip", "gas"}})
    cid = (chain or "sol").lower()
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO chain_trade (user_id, chain, buy_slip, sell_slip, gas)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, chain) DO UPDATE SET
                buy_slip = excluded.buy_slip,
                sell_slip = excluded.sell_slip,
                gas = excluded.gas
            """,
            (int(user_id), cid, cur["buy_slip"], cur["sell_slip"], cur["gas"]),
        )
        conn.commit()
    return cur


def get_native_mark(chain: str) -> tuple[float, int] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT price, ts FROM native_marks WHERE chain = ?",
            (chain.lower(),),
        ).fetchone()
    if not row:
        return None
    return float(row["price"]), int(row["ts"])


def set_native_mark(chain: str, price: float) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO native_marks (chain, price, ts)
            VALUES (?, ?, ?)
            ON CONFLICT(chain) DO UPDATE SET price = excluded.price, ts = excluded.ts
            """,
            (chain.lower(), float(price), int(time.time())),
        )
        conn.commit()


def list_feed_binds() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    extra = os.getenv("FERZAN_FEED_CHAT", "").strip()
    if extra:
        try:
            out.append((int(extra), "*"))
        except ValueError:
            pass
    with get_conn() as conn:
        rows = conn.execute("SELECT chat_id, chain FROM feed_binds").fetchall()
        out.extend((int(r["chat_id"]), str(r["chain"] or "*")) for r in rows)
    return out


def add_sponsored(chain: str, kind: str, title: str, url: str, hours: float, ca: str = "") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO sponsored (chain, kind, title, url, ca, expires_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chain.lower(), kind, title, url, ca, int(time.time() + hours * 3600)),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_sponsored(chain: str, kind: str) -> list[dict]:
    now = int(time.time())
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM sponsored
            WHERE (chain = ? OR chain = '*') AND kind = ? AND expires_at > ?
            ORDER BY id DESC
            """,
            (chain.lower(), kind, now),
        ).fetchall()
        return [dict(r) for r in rows]


def list_feed_chats() -> list[int]:
    return list(dict.fromkeys(cid for cid, _ch in list_feed_binds()))


def add_buy_limit(user_id: int, mint: str, chain: str, usd: float, target_px: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO buy_limits (user_id, mint, chain, usd, target_px, status) VALUES (?,?,?,?,?,'armed')",
            (user_id, mint, chain, float(usd), float(target_px)),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_buy_limits(user_id: int | None = None) -> list[dict]:
    with get_conn() as conn:
        if user_id is None:
            rows = conn.execute("SELECT * FROM buy_limits WHERE status = 'armed'").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM buy_limits WHERE user_id = ? ORDER BY id DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def fill_buy_limit(lid: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE buy_limits SET status = 'filled' WHERE id = ?", (lid,))
        conn.commit()


def cancel_buy_limit(user_id: int, lid: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE buy_limits SET status = 'cancelled' WHERE id = ? AND user_id = ?",
            (lid, user_id),
        )
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


def reduce_live_cost_pct(user_id: int, mint: str, sold_pct: float) -> None:
    """Shrinks recorded cost basis by sold_pct after a partial sell, so the
    remaining position's PnL% still reflects only what's still held."""
    sold_pct = max(0.0, min(100.0, float(sold_pct)))
    if sold_pct <= 0:
        return
    with get_conn() as conn:
        row = conn.execute(
            "SELECT cost_usd FROM live_basis WHERE user_id = ? AND mint = ?",
            (user_id, mint),
        ).fetchone()
        if not row:
            return
        cost = float(row["cost_usd"] or 0)
        delta = -(cost * sold_pct / 100.0)
        conn.execute(
            "UPDATE live_basis SET cost_usd = MAX(0, cost_usd + ?), updated_at = ? WHERE user_id = ? AND mint = ?",
            (delta, int(time.time()), user_id, mint),
        )
        conn.commit()


def credit_desk_share(trader_id: int, volume_usd: float) -> str:
    """Pay the referrer from Ferzan's cut. Invitee keeps an Ape Pass window."""
    vol = max(0.0, float(volume_usd))
    if vol <= 0:
        return ""
    trader = get_user(int(trader_id)) or {}
    parent = trader.get("referred_by")
    if not parent:
        try:
            import requests
            base = (os.getenv("LAUNCH_API_URL") or "http://127.0.0.1:8000").rstrip("/")
            token = (os.getenv("INTERNAL_API_TOKEN") or "").strip()
            headers = {"X-Ferzan-Internal": token} if token else {}
            r = requests.get(f"{base}/internal/referrer-wallet/{int(trader_id)}", headers=headers, timeout=8)
            if r.status_code >= 400:
                raise RuntimeError(r.text[:180])
            data = r.json() if r.content else {}
            # This endpoint returns payout wallet; referrer id lives on launch referrals table.
            # Keep referred_by if Desk already has it. Wallet-only lookup is enough for curve buys.
            if data.get("referrer_id"):
                parent = int(data["referrer_id"])
                update_user(int(trader_id), referred_by=parent)
        except Exception as exc:
            import logging
            logging.getLogger("db").warning("launch referrer lookup failed user=%s: %s", trader_id, exc)
            parent = None
    now = int(time.time())
    if not trader.get("discount_until"):
        update_user(int(trader_id), discount_until=now + 30 * 86400)
    if not parent:
        return ""
    parent = int(parent)
    if parent == int(trader_id):
        return ""
    with get_conn() as conn:
        tot = conn.execute(
            "SELECT COALESCE(SUM(volume_usd),0) FROM referral_ledger "
            "WHERE user_id = ? AND kind = 'share' AND COALESCE(level, 1) = 1",
            (parent,),
        ).fetchone()[0]
    tot = float(tot or 0) + vol
    pct, tier = _tier_for_volume(tot)
    import fees as _fees

    cut = vol * (_fees.current_bps() / 10_000.0)
    share = cut * pct
    # Level 2 / 3: the referrer's own referrer, and theirs. Walk the chain,
    # never paying the trader or anyone twice (a corrupted loop can't pay).
    payees = [(parent, 1, share)]
    seen = {int(trader_id), parent}
    up = parent
    for level, lvl_pct in ((2, REF_L2_PCT), (3, REF_L3_PCT)):
        nxt = (get_user(up) or {}).get("referred_by")
        if not nxt or int(nxt) in seen or lvl_pct <= 0:
            break
        up = int(nxt)
        seen.add(up)
        payees.append((up, level, cut * lvl_pct))
    with get_conn() as conn:
        for who, level, amt in payees:
            conn.execute(
                """
                INSERT INTO referral_ledger
                (user_id, from_user, created_at, volume_usd, share_usd, kind, status, level)
                VALUES (?, ?, ?, ?, ?, 'share', 'open', ?)
                """,
                (who, int(trader_id), now, vol, amt, level),
            )
        conn.commit()
    return f"Desk Share {tier} +${share:.4f} → {parent}"


REF_L2_PCT = float(os.getenv("REF_L2_PCT", "0.10"))
REF_L3_PCT = float(os.getenv("REF_L3_PCT", "0.05"))


def _tier_for_volume(vol: float) -> tuple[float, str]:
    if vol >= 250_000:
        return 0.40, "Desk"
    if vol >= 50_000:
        return 0.35, "Captain"
    return 0.30, "Scout"


def would_create_ref_loop(user_id: int, referrer_id: int, depth: int = 12) -> bool:
    """True if making referrer_id the referrer of user_id closes a loop
    (user_id already sits somewhere above referrer_id)."""
    cur = int(referrer_id)
    for _ in range(depth):
        if cur == int(user_id):
            return True
        nxt = (get_user(cur) or {}).get("referred_by")
        if not nxt:
            return False
        cur = int(nxt)
    return True  # absurdly deep chain: refuse rather than guess


def referral_stats(user_id: int) -> dict:
    uid = int(user_id)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(level, 1) AS lvl,
              COALESCE(SUM(volume_usd),0) AS vol,
              COALESCE(SUM(share_usd),0) AS earned,
              COALESCE(SUM(CASE WHEN status='open' THEN share_usd ELSE 0 END),0) AS open_usd,
              COALESCE(SUM(CASE WHEN status='claimed' THEN share_usd ELSE 0 END),0) AS pending,
              COUNT(*) AS n
            FROM referral_ledger WHERE user_id = ? AND kind = 'share'
            GROUP BY COALESCE(level, 1)
            """,
            (uid,),
        ).fetchall()
        kids = conn.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (uid,)).fetchone()[0]
        l2 = conn.execute(
            "SELECT COUNT(*) FROM users WHERE referred_by IN (SELECT user_id FROM users WHERE referred_by = ?)",
            (uid,),
        ).fetchone()[0]
        l3 = conn.execute(
            """
            SELECT COUNT(*) FROM users WHERE referred_by IN (
              SELECT user_id FROM users WHERE referred_by IN (
                SELECT user_id FROM users WHERE referred_by = ?))
            """,
            (uid,),
        ).fetchone()[0]
    by_level = {1: 0.0, 2: 0.0, 3: 0.0}
    vol = earned = open_usd = pending = 0.0
    n = 0
    for r in rows:
        lvl = int(r["lvl"])
        by_level[lvl] = by_level.get(lvl, 0.0) + float(r["earned"] or 0)
        if lvl == 1:
            vol += float(r["vol"] or 0)
        earned += float(r["earned"] or 0)
        open_usd += float(r["open_usd"] or 0)
        pending += float(r["pending"] or 0)
        n += int(r["n"] or 0)
    if vol >= 250_000:
        tier = "Desk"
    elif vol >= 50_000:
        tier = "Captain"
    elif vol > 0 or kids:
        tier = "Scout"
    else:
        tier = "Rookie"
    return {
        "volume": vol,
        "earned": earned,
        "open": open_usd,
        "pending": pending,
        "fills": n,
        "invites": int(kids or 0),
        "invites_l2": int(l2 or 0),
        "invites_l3": int(l3 or 0),
        "by_level": by_level,
        "tier": tier,
    }


def request_referral_claim(user_id: int) -> float:
    stats = referral_stats(user_id)
    if stats["open"] <= 0:
        return 0.0
    with get_conn() as conn:
        conn.execute(
            "UPDATE referral_ledger SET status = 'claimed' WHERE user_id = ? AND status = 'open'",
            (int(user_id),),
        )
        conn.commit()
    return stats["open"]


def max_claimed_ref_id(user_id: int) -> int:
    with get_conn() as conn:
        return int(conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM referral_ledger WHERE user_id = ? AND status = 'claimed'",
            (int(user_id),),
        ).fetchone()[0])


def mark_referral_paid(user_id: int, max_id: int | None = None) -> float:
    """Admin paid a claim out of the treasury: claimed -> paid. One write
    transaction, so a claim landing mid-way can't be marked paid unpaid."""
    with get_conn() as conn:
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT id, share_usd FROM referral_ledger WHERE user_id = ? AND status = 'claimed' AND id <= ?",
                (int(user_id), int(max_id) if max_id is not None else 2**62),
            ).fetchall()
            ids = [r[0] for r in rows]
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                conn.execute(
                    f"UPDATE referral_ledger SET status = 'paid' WHERE id IN ({','.join('?' * len(chunk))})", chunk
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return float(sum(r[1] for r in rows))


# =========================================================== batch 4 ====
# Mini App orders, token alerts, migration sniper, address book, trade log,
# presets, daily recap. Tables are created by init_db() -> _init_v4().

def _init_v4(conn) -> None:
    ex_cols = {r[1] for r in conn.execute("PRAGMA table_info(live_exits)").fetchall()}
    if ex_cols and "peak_px" not in ex_cols:
        conn.execute("ALTER TABLE live_exits ADD COLUMN peak_px REAL")
    rl_cols = {r[1] for r in conn.execute("PRAGMA table_info(referral_ledger)").fetchall()}
    if "level" not in rl_cols:
        conn.execute("ALTER TABLE referral_ledger ADD COLUMN level INTEGER DEFAULT 1")
    ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "sell_presets" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN sell_presets TEXT")
    ct_cols = {r[1] for r in conn.execute("PRAGMA table_info(chain_trade)").fetchall()}
    if "presets" not in ct_cols:
        conn.execute("ALTER TABLE chain_trade ADD COLUMN presets TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS webapp_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('buy', 'sell')),
            mint TEXT NOT NULL,
            chain TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL,
            unit TEXT NOT NULL CHECK(unit IN ('native', 'usd', 'pct')),
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_webapp_orders_status ON webapp_orders(status);
        CREATE INDEX IF NOT EXISTS idx_webapp_orders_user ON webapp_orders(user_id, created_at);

        CREATE TABLE IF NOT EXISTS token_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            mint TEXT NOT NULL,
            symbol TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL CHECK(kind IN ('mc', 'price')),
            direction TEXT NOT NULL CHECK(direction IN ('above', 'below')),
            target REAL NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            active INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL,
            fired_at INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_token_alerts_active ON token_alerts(active);

        CREATE TABLE IF NOT EXISTS mig_config (
            user_id INTEGER PRIMARY KEY,
            mode TEXT NOT NULL DEFAULT 'off',
            usd REAL NOT NULL DEFAULT 10,
            max_top10 REAL NOT NULL DEFAULT 30,
            min_liq REAL NOT NULL DEFAULT 5000,
            max_per_day INTEGER NOT NULL DEFAULT 3,
            tp_pct REAL NOT NULL DEFAULT 0,
            sl_pct REAL NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mig_seen (
            pool TEXT PRIMARY KEY,
            mint TEXT NOT NULL,
            seen_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mig_fills (
            user_id INTEGER NOT NULL,
            mint TEXT NOT NULL,
            ts INTEGER NOT NULL,
            PRIMARY KEY (user_id, mint)
        );

        CREATE TABLE IF NOT EXISTS address_book (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            family TEXT NOT NULL,
            label TEXT NOT NULL,
            address TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(user_id, family, address)
        );

        CREATE TABLE IF NOT EXISTS live_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            side TEXT NOT NULL,
            mint TEXT NOT NULL,
            chain TEXT NOT NULL DEFAULT '',
            usd REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_live_trades_user_ts ON live_trades(user_id, ts);

        CREATE TABLE IF NOT EXISTS recap_marks (
            user_id INTEGER PRIMARY KEY,
            day TEXT NOT NULL,
            desk_usd REAL NOT NULL,
            sent_at INTEGER NOT NULL
        );
        """
    )


# ---------------------------------------------------------------- presets --
DEFAULT_PRESETS = {
    "sol": [0.05, 0.1, 0.25, 0.5, 1, 2],
    "eth": [0.005, 0.01, 0.025, 0.05, 0.1, 0.25],
    "base": [0.005, 0.01, 0.025, 0.05, 0.1, 0.25],
    "arb": [0.005, 0.01, 0.025, 0.05, 0.1, 0.25],
    "hood": [0.005, 0.01, 0.025, 0.05, 0.1, 0.25],
    "bsc": [0.02, 0.05, 0.1, 0.25, 0.5, 1],
    "avax": [0.5, 1, 2.5, 5, 10, 25],
    "ton": [1, 2, 5, 10, 25, 50],
}
DEFAULT_SELL_PRESETS = [25, 50, 100]


def _parse_nums(raw: str | None) -> list[float]:
    out = []
    for part in str(raw or "").replace(" ", ",").split(","):
        try:
            v = float(part)
        except ValueError:
            continue
        if v > 0:
            out.append(v)
    return out


def buy_presets(user_id: int | None, chain: str) -> list[float]:
    cid = (chain or "sol").lower()
    if user_id:
        with get_conn() as conn:
            row = conn.execute(
                "SELECT presets FROM chain_trade WHERE user_id = ? AND chain = ?", (int(user_id), cid)
            ).fetchone()
        got = _parse_nums(row["presets"] if row else "")
        if got:
            return got[:6]
    return list(DEFAULT_PRESETS.get(cid, DEFAULT_PRESETS["eth"]))


def set_buy_presets(user_id: int, chain: str, values: list[float]) -> None:
    cid = (chain or "sol").lower()
    set_chain_trade(user_id, cid)  # make sure the row exists
    raw = ",".join(f"{v:g}" for v in values[:6]) if values else None
    with get_conn() as conn:
        conn.execute(
            "UPDATE chain_trade SET presets = ? WHERE user_id = ? AND chain = ?", (raw, int(user_id), cid)
        )
        conn.commit()


def sell_presets(user_id: int | None) -> list[int]:
    if user_id:
        got = [int(v) for v in _parse_nums((get_user(int(user_id)) or {}).get("sell_presets")) if 1 <= v <= 100]
        if got:
            return got[:4]
    return list(DEFAULT_SELL_PRESETS)


def set_sell_presets(user_id: int, values: list[int]) -> None:
    raw = ",".join(str(int(v)) for v in values[:4]) if values else None
    update_user(int(user_id), sell_presets=raw)


# ---------------------------------------------------------- webapp orders --
WEBAPP_ORDER_TTL_S = 45  # a queued order older than this never executes


def add_webapp_order(user_id: int, side: str, mint: str, chain: str, amount: float, unit: str,
                     multi: bool = False) -> int:
    """Returns the new id, or 0 if this user already has an order in flight
    (checked in the same statement, so two fast taps can't both queue)."""
    now = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO webapp_orders (user_id, side, mint, chain, amount, unit, multi, status, created_at, updated_at)
            SELECT ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?
            WHERE NOT EXISTS (
                SELECT 1 FROM webapp_orders WHERE user_id = ? AND status IN ('pending', 'running')
            )
            """,
            (int(user_id), side, mint, chain or "", float(amount), unit, 1 if multi else 0, now, now, int(user_id)),
        )
        conn.commit()
        return int(cur.lastrowid) if cur.rowcount == 1 else 0


def open_webapp_orders(user_id: int) -> int:
    with get_conn() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM webapp_orders WHERE user_id = ? AND status IN ('pending', 'running')",
                (int(user_id),),
            ).fetchone()[0]
        )


def recent_webapp_orders(user_id: int, seconds: int = 60) -> int:
    with get_conn() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM webapp_orders WHERE user_id = ? AND created_at >= ?",
                (int(user_id), int(time.time()) - int(seconds)),
            ).fetchone()[0]
        )


def get_webapp_order(order_id: int, user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM webapp_orders WHERE id = ? AND user_id = ?", (int(order_id), int(user_id))
        ).fetchone()
        return dict(row) if row else None


def claim_webapp_orders(limit: int = 20) -> list[dict]:
    """Expire stale pending orders, then atomically claim fresh ones
    (pending -> running). A row is claimed by exactly one caller."""
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            "UPDATE webapp_orders SET status = 'expired', result = ?, updated_at = ? "
            "WHERE status = 'pending' AND created_at < ?",
            ("Expired before the bot picked it up — nothing was sent.", now, now - WEBAPP_ORDER_TTL_S),
        )
        rows = conn.execute(
            "SELECT * FROM webapp_orders WHERE status = 'pending' ORDER BY id LIMIT ?", (int(limit),)
        ).fetchall()
        claimed = []
        for r in rows:
            cur = conn.execute(
                "UPDATE webapp_orders SET status = 'running', updated_at = ? WHERE id = ? AND status = 'pending'",
                (now, r["id"]),
            )
            if cur.rowcount == 1:
                claimed.append(dict(r))
        conn.commit()
    return claimed


def finish_webapp_order(order_id: int, ok: bool, result: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE webapp_orders SET status = ?, result = ?, updated_at = ? WHERE id = ?",
            ("done" if ok else "failed", (result or "")[:1500], int(time.time()), int(order_id)),
        )
        conn.commit()


def fail_stuck_webapp_orders(max_running_s: int = 600) -> int:
    """After a crash/restart, 'running' rows never finish. Mark them unknown
    (the trade may or may not have landed — the user checks their bag)."""
    now = int(time.time())
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE webapp_orders SET status = 'failed', updated_at = ?, "
            "result = 'The bot restarted mid-trade — check your bag before retrying.' "
            "WHERE status = 'running' AND updated_at < ?",
            (now, now - int(max_running_s)),
        )
        conn.commit()
        return cur.rowcount


# ----------------------------------------------------------- token alerts --
MAX_TOKEN_ALERTS = 25


def add_token_alert(
    user_id: int, mint: str, symbol: str, kind: str, direction: str, target: float, note: str = ""
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO token_alerts (user_id, mint, symbol, kind, direction, target, note, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (int(user_id), mint, symbol or "", kind, direction, float(target), note or "", int(time.time())),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_token_alerts(user_id: int | None = None) -> list[dict]:
    with get_conn() as conn:
        if user_id is None:
            rows = conn.execute("SELECT * FROM token_alerts WHERE active = 1").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM token_alerts WHERE active = 1 AND user_id = ? ORDER BY id", (int(user_id),)
            ).fetchall()
        return [dict(r) for r in rows]


def fire_token_alert(alert_id: int) -> bool:
    """active -> fired. True only for the caller that flipped it."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE token_alerts SET active = 0, fired_at = ? WHERE id = ? AND active = 1",
            (int(time.time()), int(alert_id)),
        )
        conn.commit()
        return cur.rowcount == 1


def delete_token_alert(alert_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE token_alerts SET active = 0 WHERE id = ? AND user_id = ? AND active = 1",
            (int(alert_id), int(user_id)),
        )
        conn.commit()
        return cur.rowcount == 1


# ------------------------------------------------------- migration sniper --
def get_mig_config(user_id: int) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM mig_config WHERE user_id = ?", (int(user_id),)).fetchone()
    if row:
        return dict(row)
    return {
        "user_id": int(user_id), "mode": "off", "usd": 10.0, "max_top10": 30.0, "min_liq": 5000.0,
        "max_per_day": 3, "tp_pct": 0.0, "sl_pct": 0.0, "updated_at": 0,
    }


def set_mig_config(user_id: int, **fields) -> dict:
    cfg = get_mig_config(user_id)
    for k, v in fields.items():
        if k in {"mode", "usd", "max_top10", "min_liq", "max_per_day", "tp_pct", "sl_pct"}:
            cfg[k] = v
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO mig_config (user_id, mode, usd, max_top10, min_liq, max_per_day, tp_pct, sl_pct, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              mode = excluded.mode, usd = excluded.usd, max_top10 = excluded.max_top10,
              min_liq = excluded.min_liq, max_per_day = excluded.max_per_day,
              tp_pct = excluded.tp_pct, sl_pct = excluded.sl_pct, updated_at = excluded.updated_at
            """,
            (
                int(user_id), cfg["mode"], float(cfg["usd"]), float(cfg["max_top10"]), float(cfg["min_liq"]),
                int(cfg["max_per_day"]), float(cfg["tp_pct"]), float(cfg["sl_pct"]), int(time.time()),
            ),
        )
        conn.commit()
    return cfg


def list_mig_users() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM mig_config WHERE mode IN ('watch', 'buy')").fetchall()]


def mig_mark_seen(pool: str, mint: str) -> bool:
    """True if this pool is new (first time we see it)."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO mig_seen (pool, mint, seen_at) VALUES (?, ?, ?)",
            (pool, mint, int(time.time())),
        )
        conn.execute("DELETE FROM mig_seen WHERE seen_at < ?", (int(time.time()) - 7 * 86400,))
        conn.commit()
        return cur.rowcount == 1


def mig_fill(user_id: int, mint: str) -> bool:
    """Reserve (user, mint) before buying. False = already bought/attempted."""
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO mig_fills (user_id, mint, ts) VALUES (?, ?, ?)",
            (int(user_id), mint, int(time.time())),
        )
        conn.commit()
        return cur.rowcount == 1


def mig_unfill(user_id: int, mint: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM mig_fills WHERE user_id = ? AND mint = ?", (int(user_id), mint))
        conn.commit()


def mig_fills_today(user_id: int) -> int:
    with get_conn() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM mig_fills WHERE user_id = ? AND ts >= ?",
                (int(user_id), int(time.time()) - 86400),
            ).fetchone()[0]
        )


# ----------------------------------------------------------- address book --
def list_addresses(user_id: int, family: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if family:
            rows = conn.execute(
                "SELECT * FROM address_book WHERE user_id = ? AND family = ? ORDER BY id", (int(user_id), family)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM address_book WHERE user_id = ? ORDER BY family, id", (int(user_id),)
            ).fetchall()
        return [dict(r) for r in rows]


def get_address(addr_id: int, user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM address_book WHERE id = ? AND user_id = ?", (int(addr_id), int(user_id))
        ).fetchone()
        return dict(row) if row else None


def save_address(user_id: int, family: str, label: str, address: str) -> int:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO address_book (user_id, family, label, address, created_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, family, address) DO UPDATE SET label = excluded.label
            """,
            (int(user_id), family, (label or "Saved")[:24], address, int(time.time())),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id FROM address_book WHERE user_id = ? AND family = ? AND address = ?",
            (int(user_id), family, address),
        ).fetchone()
        return int(row["id"])


def delete_address(addr_id: int, user_id: int) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM address_book WHERE id = ? AND user_id = ?", (int(addr_id), int(user_id)))
        conn.commit()
        return cur.rowcount == 1


# -------------------------------------------------------------- trade log --
def log_trade(user_id: int, side: str, mint: str, chain: str = "", usd: float = 0.0, source: str = "") -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO live_trades (user_id, ts, side, mint, chain, usd, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (int(user_id), int(time.time()), side, mint or "", chain or "", float(usd or 0), source or ""),
        )
        conn.commit()


def trades_since(user_id: int, since_ts: int) -> list[dict]:
    with get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM live_trades WHERE user_id = ? AND ts >= ? ORDER BY ts", (int(user_id), int(since_ts))
            ).fetchall()
        ]


def users_with_wallets() -> list[int]:
    with get_conn() as conn:
        return [int(r[0]) for r in conn.execute("SELECT user_id FROM user_wallets").fetchall()]


def get_recap_mark(user_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM recap_marks WHERE user_id = ?", (int(user_id),)).fetchone()
        return dict(row) if row else None


def set_recap_mark(user_id: int, day: str, desk_usd: float) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO recap_marks (user_id, day, desk_usd, sent_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET day = excluded.day, desk_usd = excluded.desk_usd,
              sent_at = excluded.sent_at
            """,
            (int(user_id), day, float(desk_usd), int(time.time())),
        )
        conn.commit()


def reset_live_peak(user_id: int, mint: str) -> None:
    """Trailing stop (re)armed: NULL peak price = start from the next price."""
    with get_conn() as conn:
        conn.execute("UPDATE live_exits SET peak_px = NULL WHERE user_id = ? AND mint = ?", (int(user_id), mint))
        conn.commit()


def count_watched_wallets(user_id: int) -> int:
    with get_conn() as conn:
        return int(
            conn.execute("SELECT COUNT(*) FROM watched_wallets WHERE user_id = ?", (int(user_id),)).fetchone()[0]
        )


# ------------------------------------------------------------------ v5 --
# Mini App: exit rules / DCA / limit buys / wallet switch / PnL card.


def _init_v5(conn) -> None:
    ucols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "multi_wallets" not in ucols:
        conn.execute("ALTER TABLE users ADD COLUMN multi_wallets TEXT")
    ocols = {r[1] for r in conn.execute("PRAGMA table_info(webapp_orders)").fetchall()}
    if "multi" not in ocols:
        conn.execute("ALTER TABLE webapp_orders ADD COLUMN multi INTEGER NOT NULL DEFAULT 0")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS card_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            mint TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_card_requests_status ON card_requests(status);
        """
    )


def replace_live_exit(user_id: int, mint: str, tp_pct: float | None, sl_pct: float | None,
                      trail_pct: float | None) -> None:
    """The app's "Exit rules" save: sets all three exactly (None clears
    that one; all None removes the rules). A new or changed trailing stop
    starts from the next price read, same as the bot's 📉 button."""
    user_id, mint = int(user_id), (mint or "").strip()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT trail_pct FROM live_exits WHERE user_id = ? AND mint = ?", (user_id, mint)
        ).fetchone()
        if tp_pct is None and sl_pct is None and trail_pct is None:
            conn.execute("DELETE FROM live_exits WHERE user_id = ? AND mint = ?", (user_id, mint))
            conn.commit()
            return
        old_trail = row["trail_pct"] if row else None
        if row:
            conn.execute(
                "UPDATE live_exits SET tp_pct = ?, sl_pct = ?, trail_pct = ? WHERE user_id = ? AND mint = ?",
                (tp_pct, sl_pct, trail_pct, user_id, mint),
            )
        else:
            conn.execute(
                "INSERT INTO live_exits (user_id, mint, tp_pct, sl_pct, trail_pct) VALUES (?, ?, ?, ?, ?)",
                (user_id, mint, tp_pct, sl_pct, trail_pct),
            )
        if trail_pct is not None and (old_trail is None or float(old_trail) != float(trail_pct)):
            conn.execute(
                "UPDATE live_exits SET peak_px = NULL WHERE user_id = ? AND mint = ?", (user_id, mint)
            )
        conn.commit()


def get_dca_plan(user_id: int, mint: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM dca_plans WHERE user_id = ? AND mint = ? AND active = 1",
            (int(user_id), (mint or "").strip()),
        ).fetchone()
        return dict(row) if row else None


def armed_buy_limits(user_id: int, mint: str | None = None) -> list[dict]:
    with get_conn() as conn:
        if mint:
            rows = conn.execute(
                "SELECT * FROM buy_limits WHERE user_id = ? AND mint = ? AND status = 'armed' ORDER BY id",
                (int(user_id), mint.strip()),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM buy_limits WHERE user_id = ? AND status = 'armed' ORDER BY id",
                (int(user_id),),
            ).fetchall()
        return [dict(r) for r in rows]


MAX_BUY_LIMITS = 10


def add_card_request(user_id: int, mint: str, per_min: int = 3) -> int:
    """Queue a PnL card for the bot to render + send. 0 = refused (one
    already pending, or too many this minute)."""
    now = int(time.time())
    with get_conn() as conn:
        pending = conn.execute(
            "SELECT COUNT(*) FROM card_requests WHERE user_id = ? AND status = 'pending'", (int(user_id),)
        ).fetchone()[0]
        recent = conn.execute(
            "SELECT COUNT(*) FROM card_requests WHERE user_id = ? AND created_at > ?", (int(user_id), now - 60)
        ).fetchone()[0]
        if pending or recent >= per_min:
            return 0
        cur = conn.execute(
            "INSERT INTO card_requests (user_id, mint, created_at) VALUES (?, ?, ?)",
            (int(user_id), (mint or "").strip(), now),
        )
        conn.commit()
        return int(cur.lastrowid)


def claim_card_requests(limit: int = 20) -> list[dict]:
    """Pending -> sent, atomically; stale (>2 min) requests are dropped."""
    now = int(time.time())
    with get_conn() as conn:
        conn.execute(
            "UPDATE card_requests SET status = 'expired' WHERE status = 'pending' AND created_at < ?",
            (now - 120,),
        )
        rows = conn.execute(
            "SELECT * FROM card_requests WHERE status = 'pending' ORDER BY id LIMIT ?", (int(limit),)
        ).fetchall()
        out = []
        for r in rows:
            cur = conn.execute(
                "UPDATE card_requests SET status = 'sent' WHERE id = ? AND status = 'pending'", (r["id"],)
            )
            if cur.rowcount == 1:
                out.append(dict(r))
        conn.commit()
        conn.execute("DELETE FROM card_requests WHERE created_at < ?", (now - 7 * 86400,))
        conn.commit()
        return out


MAX_MULTI_WALLETS = 5


def multi_wallets(user_id: int) -> list[int]:
    """Wallet slot ids ticked for multi-buy that still exist (active wallet
    NOT implied here; callers always add it). Order = slot order."""
    row = get_user(int(user_id)) or {}
    raw = str(row.get("multi_wallets") or "")
    want = {int(x) for x in raw.split(",") if x.strip().isdigit()}
    return [s["id"] for s in list_wallet_slots(int(user_id)) if s["id"] in want]


def set_multi_wallets(user_id: int, slot_ids: list[int]) -> list[int]:
    valid = [s["id"] for s in list_wallet_slots(int(user_id)) if s["id"] in set(slot_ids)][:MAX_MULTI_WALLETS]
    if not get_user(int(user_id)):
        ensure_user(int(user_id), None)
    update_user(int(user_id), multi_wallets=",".join(str(i) for i in valid))
    return valid


def multi_buy_slots(user_id: int) -> list[int]:
    """Slots a multi-buy would use: the active wallet first, then ticked
    ones, capped at MAX_MULTI_WALLETS. [] when multi-buy is off or <2."""
    if not flag_on(int(user_id), "multi_buy", 0):
        return []
    slots = list_wallet_slots(int(user_id))
    active = [s["id"] for s in slots if s["active"]]
    ids = active + [i for i in multi_wallets(user_id) if i not in active]
    ids = ids[:MAX_MULTI_WALLETS]
    return ids if len(ids) >= 2 else []
