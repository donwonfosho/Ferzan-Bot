"""Hot-wallet helper for THIS droplet only.

Reads SIGNER_MNEMONIC or SIGNER_KEY from .env. Never logs the secret.
Used to confirm the public Solana address before any live send.
"""

from __future__ import annotations

import os


def _phrase() -> str:
    return " ".join((os.getenv("SIGNER_MNEMONIC") or "").split())


def _raw_key() -> str:
    return (os.getenv("SIGNER_KEY") or "").strip()


def configured() -> bool:
    return bool(_phrase() or _raw_key())


def public_sol() -> str:
    try:
        from solders.keypair import Keypair
    except ImportError as exc:
        raise RuntimeError("pip install solders") from exc

    raw = _raw_key()
    phrase = _phrase()
    kp = None
    if raw:
        try:
            kp = Keypair.from_base58_string(raw)
        except Exception:
            kp = None
    if kp is None and phrase:
        try:
            kp = Keypair.from_seed_phrase_and_passphrase(phrase, "")
        except Exception as exc:
            raise RuntimeError("Mnemonic did not derive a Solana key. Check the 12 words.") from exc
    if kp is None:
        raise RuntimeError("Set SIGNER_MNEMONIC or SIGNER_KEY in .env")
    return str(kp.pubkey())


def status_text() -> str:
    if not configured():
        return "Signer empty. Add SIGNER_MNEMONIC on the droplet."
    try:
        addr = public_sol()
    except Exception as exc:
        return f"Signer present but could not derive address.\n{exc}"
    return (
        "Signer loaded on this box.\n"
        f"Solana: {addr}\n"
        "This must match Ferzan Trade Bot in Trust.\n"
        "Live Buy is still off until we wire Jupiter on this process."
    )
