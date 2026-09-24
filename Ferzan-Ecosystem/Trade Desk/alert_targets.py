"""Parse a token-alert target the way traders type it: 2m, 250k, +50%,
-30%, price 0.0012. Shared by the bot and the Mini App so both accept
exactly the same input. Pure functions, no I/O."""

from __future__ import annotations


_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}


def _parse_money(raw: str) -> float | None:
    t = raw.strip().lower().replace("$", "").replace(",", "")
    mult = 1.0
    if t and t[-1] in _SUFFIX:
        mult, t = _SUFFIX[t[-1]], t[:-1]
    try:
        v = float(t) * mult
    except ValueError:
        return None
    return v if v > 0 else None


def parse_alert_target(text: str, px: float, mc: float) -> tuple[str, str, float, str] | str:
    """-> (kind, direction, target, note) or an error string.
    kind: 'mc' | 'price'."""
    t = " ".join((text or "").strip().lower().split())
    if not t:
        return "Send a target like 2m, +50% or price 0.0012."
    if t.endswith("%"):
        try:
            pct = float(t[:-1].replace("+", ""))
        except ValueError:
            return "Percent looks off. Try +50% or -30%."
        if pct == 0 or pct <= -100:
            return "Pick a move between -99% and anything up."
        if px <= 0:
            return "No live price for this token right now — use a market cap target like 2m."
        target = px * (1 + pct / 100)
        return "price", ("above" if pct > 0 else "below"), target, f"{pct:+g}%"
    if t.startswith("price") or t.startswith("px"):
        v = _parse_money(t.split(" ", 1)[1] if " " in t else "")
        if v is None:
            return "Price looks off. Example: price 0.0012"
        if px <= 0:
            return "No live price for this token right now."
        return "price", ("above" if v > px else "below"), v, f"${v:.6g}"
    if t.startswith("mc"):
        t = t[2:].strip()
    v = _parse_money(t)
    if v is None:
        return "Couldn't read that. Try 2m, 250k, +50% or price 0.0012."
    if v < 1000:
        return "That's too small for a market cap. For a price use: price 0.0012"
    if mc <= 0:
        return "No live market cap for this token right now — try a % move instead."
    return "mc", ("above" if v > mc else "below"), v, f"${v:,.0f} MC"
