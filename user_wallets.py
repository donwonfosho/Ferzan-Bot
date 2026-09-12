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


def import_keys(user_id: int, sol_secret: str = "", evm_secret: str = "") -> dict:
    sol_secret = (sol_secret or "").strip()
    evm_secret = (evm_secret or "").strip()
    if not sol_secret and not evm_secret:
        raise ValueError("Need a Solana key or an EVM hex key.")
    row = db.get_user_wallet(user_id) or ensure(user_id)
    sol_pub, evm_pub = row["sol_pub"], row["evm_pub"]
    sol_store, evm_store = row["sol_key"], row["evm_key"]
    if sol_secret:
        from solders.keypair import Keypair

        try:
            kp = Keypair.from_base58_string(sol_secret)
        except Exception:
            kp = Keypair.from_bytes(base64.b64decode(sol_secret))
        sol_pub = str(kp.pubkey())
        try:
            packed = kp.to_base58_string()
        except Exception:
            packed = base64.b64encode(bytes(kp)).decode()
        sol_store = _lock(packed)
    if evm_secret:
        from eth_account import Account

        raw = evm_secret[2:] if evm_secret.startswith("0x") else evm_secret
        acct = Account.from_key("0x" + raw)
        evm_pub = acct.address
        evm_store = _lock(acct.key.hex())
    db.save_user_wallet(user_id, sol_pub, sol_store, evm_pub, evm_store)
    return db.get_user_wallet(user_id) or {}


def secrets(user_id: int) -> tuple[str, str]:
    row = ensure(user_id)
    return _unlock(row["sol_key"]), _unlock(row["evm_key"])


def card_text(user_id: int) -> str:
    row = ensure(user_id)
    bal = ""
    try:
        import signer

        lamports = signer.sol_balance_lamports(row["sol_pub"])
        bal = f"Balance {lamports / 1_000_000_000:.6f} SOL\n"
    except Exception:
        bal = ""
    return (
        "Your Ferzan wallets\n\n"
        f"Solana\n`{row['sol_pub']}`\n{bal}\n"
        f"EVM (ETH / Base / BSC)\n`{row['evm_pub']}`\n\n"
        "Fund those addresses. /bag lists tokens. Buy spends this wallet."
    )
