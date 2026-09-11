"""Per-Telegram-user trading wallets. Custodial on this box."""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

import db

MASTER_PATH = Path(os.getenv("FERZAN_MASTER_PATH", "/opt/ferzan/app/.master"))


def _fernet():
    from cryptography.fernet import Fernet

    secret = (os.getenv("FERZAN_MASTER_KEY") or "").strip()
    if not secret:
        if MASTER_PATH.exists():
            secret = MASTER_PATH.read_text().strip()
        else:
            secret = base64.urlsafe_b64encode(os.urandom(32)).decode()
            MASTER_PATH.parent.mkdir(parents=True, exist_ok=True)
            MASTER_PATH.write_text(secret)
            MASTER_PATH.chmod(0o600)
    digest = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _lock(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def _unlock(blob: str) -> str:
    return _fernet().decrypt(blob.encode()).decode()


def ensure(user_id: int) -> dict:
    row = db.get_user_wallet(user_id)
    if row:
        return row
    from eth_account import Account
    from solders.keypair import Keypair

    sol = Keypair()
    evm = Account.create()
    try:
        sol_secret = sol.to_base58_string()
    except Exception:
        sol_secret = base64.b64encode(bytes(sol)).decode()
    evm_secret = evm.key.hex()
    db.save_user_wallet(
        user_id,
        str(sol.pubkey()),
        _lock(sol_secret),
        evm.address,
        _lock(evm_secret),
    )
    return db.get_user_wallet(user_id) or {}


def secrets(user_id: int) -> tuple[str, str]:
    row = ensure(user_id)
    return _unlock(row["sol_key"]), _unlock(row["evm_key"])


def card_text(user_id: int) -> str:
    row = ensure(user_id)
    return (
        "Your Ferzan wallets (you deposit, you trade)\n\n"
        f"Solana\n`{row['sol_pub']}`\n\n"
        f"EVM (ETH / Base / BSC)\n`{row['evm_pub']}`\n\n"
        "Send SOL to the Solana line. Send ETH or BNB to the EVM line on that network.\n"
        "Live Buy/Sell spend THESE addresses, not Ferzan treasury.\n"
        "Export is not shown in chat. Ask support if you must leave."
    )
