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


def _fresh_keys() -> tuple[str, str, str, str]:
    """(sol_pub, locked sol secret, evm address, locked evm secret)."""
    from eth_account import Account
    from solders.keypair import Keypair

    sol = Keypair()
    evm = Account.create()
    try:
        sol_secret = sol.to_base58_string()
    except Exception:
        sol_secret = base64.b64encode(bytes(sol)).decode()
    return str(sol.pubkey()), _lock(sol_secret), evm.address, _lock(evm.key.hex())


def ensure(user_id: int) -> dict:
    """The user's ACTIVE wallet, creating their first one if needed."""
    row = db.get_user_wallet(user_id)
    if row:
        return row
    db.save_user_wallet(user_id, *_fresh_keys())
    return db.get_user_wallet(user_id) or {}


def new_wallet(user_id: int, label: str = "") -> dict:
    """Generate another wallet and make it active. Existing wallets are kept."""
    ensure(user_id)  # first wallet always exists before a second one
    sid = db.add_wallet_slot(user_id, label, *_fresh_keys())
    return db.set_active_wallet(user_id, sid) or {}


def switch_wallet(user_id: int, slot_id: int) -> dict | None:
    return db.set_active_wallet(user_id, slot_id)


def all_secrets(user_id: int) -> list[tuple[int, str, str, str]]:
    """[(slot_id, label, sol_secret, evm_secret)], ACTIVE wallet first.
    Used by sells to find whichever wallet actually holds a token."""
    ensure(user_id)
    slots = db.list_wallet_slots(user_id)
    slots.sort(key=lambda r: (not r["active"], r["id"]))
    return [(int(r["id"]), r["label"], _unlock(r["sol_key"]), _unlock(r["evm_key"])) for r in slots]


def active_label(user_id: int) -> str:
    for r in db.list_wallet_slots(user_id):
        if r["active"]:
            return r["label"]
    return "Main"


def import_keys(user_id: int, sol_secret: str = "", evm_secret: str = "") -> dict:
    """Import into a NEW wallet slot and make it active. Never overwrites an
    existing key (the old behaviour replaced the active key, which could
    strand funds sitting in the bot-generated wallet). The chain you didn't
    import gets a fresh generated key in the new slot."""
    sol_secret = (sol_secret or "").strip()
    evm_secret = (evm_secret or "").strip()
    if not sol_secret and not evm_secret:
        raise ValueError("Need a Solana key or an EVM hex key.")
    ensure(user_id)
    sol_pub, sol_store, evm_pub, evm_store = _fresh_keys()
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
    for r in db.list_wallet_slots(user_id):
        # Same key imported again: just switch to that wallet.
        if (sol_secret and r["sol_pub"] == sol_pub) or (evm_secret and r["evm_pub"].lower() == evm_pub.lower()):
            return db.set_active_wallet(user_id, int(r["id"])) or {}
    sid = db.add_wallet_slot(user_id, "Imported", sol_pub, sol_store, evm_pub, evm_store)
    return db.set_active_wallet(user_id, sid) or {}


def secrets(user_id: int) -> tuple[str, str]:
    row = ensure(user_id)
    return _unlock(row["sol_key"]), _unlock(row["evm_key"])


def export_text(user_id: int) -> str:
    sol, evm = secrets(user_id)
    row = ensure(user_id)
    return (
        "⚠️ SAVE OFFLINE. Delete this Telegram message after you copy it.\n"
        "Anyone with these keys owns the bag.\n\n"
        f"Wallet: {active_label(user_id)} (switch in /wallet → 👛 to export another)\n\n"
        f"Solana address\n`{row['sol_pub']}`\n"
        f"Solana private key\n`{sol}`\n\n"
        f"EVM address\n`{row['evm_pub']}`\n"
        f"EVM private key\n`{evm}`\n\n"
        "Phantom → import Solana private key.\n"
        "MetaMask / Trust → import EVM private key.\n"
        "Bot-generated wallets have no 12-word phrase — only these keys."
    )


def card_text(user_id: int) -> str:
    row = ensure(user_id)
    bal = ""
    try:
        import signer

        lamports = signer.sol_balance_lamports(row["sol_pub"])
        bal = f"Balance {lamports / 1_000_000_000:.6f} SOL\n"
    except Exception:
        bal = ""
    body = (
        "Your Ferzan wallets\n\n"
        f"Solana\n`{row['sol_pub']}`\n{bal}\n"
        f"EVM (ETH / Base / BNB / ARB…)\n`{row['evm_pub']}`\n"
    )
    try:
        import tron_signer

        _, evm_secret = secrets(user_id)
        tron_addr, _ = tron_signer.evm_key_to_tron(evm_secret)
        body += f"\nTRON (same key)\n`{tron_addr}`\nFund TRX + energy here.\n"
    except Exception:
        body += "\nTRON uses the EVM key.\n"
    body += "\nFund those addresses. Buy spends this wallet."
    return body
