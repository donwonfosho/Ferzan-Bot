"""Remembers sends whose outcome is UNKNOWN ("sent but not confirmed", "MAY have gone out").

The signers return (False, text) for those, same as a clean failure, so a job that retries on False
could buy the same token twice. This module spots the unclear ones by their wording and keeps a short
memory per (user, token, side); automated jobs (snipes, DCA, limits, feed auto-buys) check it and wait.
A person tapping Buy is never blocked: they can read the message and decide."""
from __future__ import annotations

import re
import threading
import time

_UNCLEAR = re.compile(
    r"before retrying|\bMAY (?:still )?(?:land|have)\b|may have (?:gone|been)|sent but not confirmed|"
    r"not confirmed (?:yet|within)|sent, not confirmed",
    re.IGNORECASE,
)
HOLD_S = 900  # automated retries of the same token wait this long after an unclear send
_LOCK = threading.Lock()
_SEEN: dict[tuple[int, str, str], float] = {}


def is_unclear(msg: str) -> bool:
    return bool(msg) and bool(_UNCLEAR.search(str(msg)))


def mark(uid: int, mint: str, side: str = "buy") -> None:
    with _LOCK:
        _SEEN[(int(uid), str(mint).lower(), side)] = time.time()


def held(uid: int, mint: str, side: str = "buy") -> bool:
    k = (int(uid), str(mint).lower(), side)
    with _LOCK:
        t = _SEEN.get(k)
        if t is None:
            return False
        if time.time() - t > HOLD_S:
            _SEEN.pop(k, None)
            return False
        return True


def clear(uid: int, mint: str, side: str = "buy") -> None:
    with _LOCK:
        _SEEN.pop((int(uid), str(mint).lower(), side), None)
