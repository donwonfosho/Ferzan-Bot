"""
credentials_db.py

Stores per-user exchange API credentials, encrypted at rest with Fernet
(symmetric encryption). These are trading API keys, not private keys/seed
phrases -- but they can still place real orders, so they don't belong in
plaintext SQLite any more than a password would.

Setup:
    pip install cryptography
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    export CREDENTIALS_ENCRYPTION_KEY="<paste the key printed above>"

Losing that key makes all stored credentials permanently undecryptable --
back it up somewhere separate from the database file itself.

Security notes worth acting on, not just reading:
  - Create exchange API keys with TRADE permission only -- explicitly
    disable withdrawal permission. Then even a leaked key can't move
    funds out, only place/cancel orders.
  - If you can, IP-allowlist the key to your droplet's address.
  - This module encrypts the secret at rest; it does not protect against
    someone with shell access to the running bot process (which can read
    decrypted secrets from memory). That's a deeper hardening step
    (secrets manager, HSM, etc.) -- reasonable to defer for testnet use,
    worth revisiting before real funds are involved.
"""

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

DB_PATH = os.environ.get("CREDENTIALS_DB_PATH", "credentials.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    user_id INTEGER PRIMARY KEY,
    exchange_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    api_key_enc BLOB NOT NULL,
    api_secret_enc BLOB NOT NULL
);
"""


def _get_fernet() -> Fernet:
    key = os.environ.get("CREDENTIALS_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError(
            "CREDENTIALS_ENCRYPTION_KEY is not set. Generate one with:\n"
            "  python3 -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"\n"
            "then export it before running the bot."
        )
    return Fernet(key.encode())


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
class Credentials:
    exchange_id: str
    symbol: str
    api_key: str
    api_secret: str


def store_credentials(user_id: int, exchange_id: str, symbol: str, api_key: str, api_secret: str):
    fernet = _get_fernet()
    api_key_enc = fernet.encrypt(api_key.encode())
    api_secret_enc = fernet.encrypt(api_secret.encode())
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO credentials (user_id, exchange_id, symbol, api_key_enc, api_secret_enc)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                 exchange_id = excluded.exchange_id,
                 symbol = excluded.symbol,
                 api_key_enc = excluded.api_key_enc,
                 api_secret_enc = excluded.api_secret_enc""",
            (user_id, exchange_id, symbol, api_key_enc, api_secret_enc),
        )


def get_credentials(user_id: int) -> Optional[Credentials]:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT exchange_id, symbol, api_key_enc, api_secret_enc FROM credentials WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None

    fernet = _get_fernet()
    try:
        api_key = fernet.decrypt(row["api_key_enc"]).decode()
        api_secret = fernet.decrypt(row["api_secret_enc"]).decode()
    except InvalidToken as e:
        raise RuntimeError(
            "Could not decrypt stored credentials -- CREDENTIALS_ENCRYPTION_KEY "
            "may have changed since they were stored."
        ) from e

    return Credentials(
        exchange_id=row["exchange_id"], symbol=row["symbol"],
        api_key=api_key, api_secret=api_secret,
    )


def delete_credentials(user_id: int):
    with _get_conn() as conn:
        conn.execute("DELETE FROM credentials WHERE user_id = ?", (user_id,))
