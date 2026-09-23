"""Per-user trade locks shared by every module that signs transactions.

One wallet must never have two sends in flight at once (EVM nonce clashes,
Solana blockhash races, TON seqno reuse). bot.py's _off() and sniper.py both
take the same lock from here, so a snipe can't race a manual trade.
"""

from __future__ import annotations

import threading

_LOCKS: dict[int, threading.Lock] = {}


def user_lock(uid: int) -> threading.Lock:
    # dict.setdefault is atomic under the GIL, so two threads can't create
    # two different locks for the same user.
    return _LOCKS.setdefault(int(uid), threading.Lock())
