"""Names nobody may launch through Ferzan: the flagship's own name (lookalikes included) and the coins scammers
most often fake. The FERZAN flagship itself is created by the droplet directly and never passes through here."""
from __future__ import annotations

import os
import re

_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "$": "s", "@": "a", "!": "i"})
_BRAND = "ferzan"
# exact tickers that are never a new coin: stablecoins and the majors, wrapped forms included
_TICKERS = {"USD", "USDC", "USDT", "DAI", "PYUSD", "FDUSD", "SOL", "WSOL", "ETH", "WETH", "BTC", "WBTC", "BNB", "WBNB",
            "TRX", "TON", "AVAX", "POL", "MATIC", "XRP", "DOGE", "ADA"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower().translate(_LEET))


def _close(a: str, b: str) -> bool:
    """True when a equals b or differs by one edit (swap, drop, add, change)."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        d = [i for i in range(len(a)) if a[i] != b[i]]
        return len(d) == 1 or (len(d) == 2 and d[1] == d[0] + 1 and a[d[0]] == b[d[1]] and a[d[1]] == b[d[0]])
    s, l = (a, b) if len(a) < len(b) else (b, a)
    return any(l[:i] + l[i + 1:] == s for i in range(len(l)))


def problem(name: str, symbol: str) -> str | None:
    """Why this name/ticker may not be launched, or None."""
    extra = {x.strip().upper() for x in (os.getenv("LAUNCH_RESERVED") or "").split(",") if x.strip()}
    sym_up = (symbol or "").strip().lstrip("$").upper()
    if sym_up in _TICKERS or sym_up in extra:
        return f"The ticker {sym_up} is reserved. Pick another one."
    for field in (name, symbol):
        n = _norm(field)
        if not n:
            continue
        if _BRAND in n or any(_close(n[i:i + 6], _BRAND) or _close(n[i:i + 5], _BRAND) or _close(n[i:i + 7], _BRAND)
                              for i in range(max(1, len(n) - 4))):
            return "That name looks like Ferzan's own token. Only the official FERZAN may use it. Pick another name."
    return None
