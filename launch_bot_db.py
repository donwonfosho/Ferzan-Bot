"""
launch_bot_db.py

Tracks launch requests from creation (in the Telegram bot's conversation)
through completion (after the Mini App gets a signed transaction back
from the user's wallet). This is the shared state between two separate
processes -- the bot and the backend API -- so both read/write the same
SQLite file.

Also stores per-request token metadata (name/symbol/description/image
URL) so the backend can serve it back as a metadata JSON endpoint,
avoiding a dependency on an external IPFS/Arweave pinning service for a
first version -- centralized, but one fewer external integration to get
working before anything launches. Revisit if you want the decentralized-
hosting guarantee later.
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List

DB_PATH = "launch_bot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS launch_requests (
    id TEXT PRIMARY KEY,
    telegram_user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    chain TEXT NOT NULL,               -- 'ethereum' / 'bsc' / 'base' / 'robinhood' / 'solana'
    mode TEXT NOT NULL,                -- 'plain' / 'bonding_curve' / 'pumpfun'
    name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    total_supply TEXT NOT NULL,        -- stored as string -- can exceed sqlite INTEGER range for 18-decimal tokens
    decimals INTEGER NOT NULL DEFAULT 18,
    description TEXT,
    image_url TEXT,
    extra_params TEXT,                 -- JSON blob for mode-specific fields (graduation threshold, dev-buy amount, etc.)
    status TEXT NOT NULL DEFAULT 'pending',  -- pending / built / submitted / confirmed / failed / expired
    wallet_address TEXT,
    tx_hash TEXT,
    result_token_address TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _get_conn() as conn:
        conn.executescript(SCHEMA)


@dataclass
class LaunchRequest:
    id: str
    telegram_user_id: int
    chat_id: int
    chain: str
    mode: str
    name: str
    symbol: str
    total_supply: str
    decimals: int
    description: Optional[str]
    image_url: Optional[str]
    extra_params: dict
    status: str
    wallet_address: Optional[str]
    tx_hash: Optional[str]
    result_token_address: Optional[str]
    error_message: Optional[str]
    created_at: str
    updated_at: str

    @classmethod
    def _from_row(cls, row) -> "LaunchRequest":
        d = dict(row)
        d["extra_params"] = json.loads(d["extra_params"]) if d["extra_params"] else {}
        return cls(**d)


def create_launch_request(
    telegram_user_id: int,
    chat_id: int,
    chain: str,
    mode: str,
    name: str,
    symbol: str,
    total_supply: str,
    decimals: int = 18,
    description: str = "",
    image_url: str = "",
    extra_params: Optional[dict] = None,
) -> LaunchRequest:
    request_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    extra_json = json.dumps(extra_params or {})

    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO launch_requests
               (id, telegram_user_id, chat_id, chain, mode, name, symbol,
                total_supply, decimals, description, image_url, extra_params,
                status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
            (request_id, telegram_user_id, chat_id, chain, mode, name, symbol,
             total_supply, decimals, description, image_url, extra_json, now, now),
        )

    return get_launch_request(request_id)


def get_launch_request(request_id: str) -> Optional[LaunchRequest]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM launch_requests WHERE id = ?", (request_id,)
        ).fetchone()
    return LaunchRequest._from_row(row) if row else None


def update_status(
    request_id: str,
    status: str,
    wallet_address: Optional[str] = None,
    tx_hash: Optional[str] = None,
    result_token_address: Optional[str] = None,
    error_message: Optional[str] = None,
):
    now = datetime.now(timezone.utc).isoformat()
    fields = ["status = ?", "updated_at = ?"]
    values = [status, now]

    for col, val in [
        ("wallet_address", wallet_address),
        ("tx_hash", tx_hash),
        ("result_token_address", result_token_address),
        ("error_message", error_message),
    ]:
        if val is not None:
            fields.append(f"{col} = ?")
            values.append(val)

    values.append(request_id)
    with _get_conn() as conn:
        conn.execute(f"UPDATE launch_requests SET {', '.join(fields)} WHERE id = ?", values)


def get_user_launch_history(telegram_user_id: int, limit: int = 20) -> List[LaunchRequest]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM launch_requests WHERE telegram_user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (telegram_user_id, limit),
        ).fetchall()
    return [LaunchRequest._from_row(r) for r in rows]
