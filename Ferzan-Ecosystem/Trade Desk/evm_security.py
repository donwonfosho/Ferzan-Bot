"""GoPlus contract-safety line for EVM tokens, shared by the classic bot and the Mini App."""

from __future__ import annotations

import requests


def security_line(chain: str, ca: str) -> str:
    ids = {"eth": "1", "bsc": "56", "base": "8453", "arb": "42161", "avax": "43114", "trx": "tron", "tron": "tron"}
    cid = ids.get((chain or "").lower())
    if not cid or not (ca.startswith("0x") or (cid == "tron" and ca.startswith("T"))):
        return ""
    try:
        r = requests.get(
            f"https://api.gopluslabs.io/api/v1/token_security/{cid}",
            params={"contract_addresses": ca},
            timeout=8,
        )
        res = (r.json() or {}).get("result") or {}
        blob = res.get(ca) or res.get(ca.lower()) or {}
    except Exception:
        return ""
    if not blob:
        return ""
    def _tax(k):  # GoPlus reports taxes as fractions: "0.01" is 1%
        try:
            return f"{float(blob.get(k) or 0) * 100:.1f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return "?"

    buy_t, sell_t = _tax("buy_tax"), _tax("sell_tax")
    flags = []
    if blob.get("is_honeypot") == "1":
        flags.append("🚨 HONEYPOT")
    if blob.get("honeypot_with_same_creator") == "1":
        flags.append("🚨 same creator rugged")
    if blob.get("cannot_sell_all") == "1":
        flags.append("⚠️ cannot sell all")
    if blob.get("is_blacklisted") == "1":
        flags.append("⚠️ blacklist")
    if blob.get("hidden_owner") == "1":
        flags.append("⚠️ hidden owner")
    if blob.get("can_take_back_ownership") == "1":
        flags.append("⚠️ owner reclaim")
    if blob.get("is_mintable") == "1":
        flags.append("⚠️ mintable")
    if blob.get("owner_change_balance") == "1":
        flags.append("⚠️ owner can change balances")
    if blob.get("personal_slippage_modifiable") == "1" or blob.get("slippage_modifiable") == "1":
        flags.append("⚠️ tax can change")
    if blob.get("is_proxy") == "1":
        flags.append("ℹ️ proxy")
    head = "🚨 HONEYPOT RISK" if blob.get("is_honeypot") == "1" else (
        "⚠️ Contract flags" if flags else "✅ No honeypot flag"
    )
    out = f"{head}  ·  buy {buy_t}%  ·  sell {sell_t}%"
    try:
        hs = [h for h in (blob.get("holders") or []) if str(h.get("is_contract", "0")) != "1"][:10]
        top10 = sum(float(h.get("percent") or 0) for h in hs) * 100
        n_hold = int(float(blob.get("holder_count") or 0))
        if hs:
            out += f"\n👥 Top 10 hold {top10:.0f}%" + (f" · {n_hold:,} holders" if n_hold else "")
    except Exception:
        pass
    if flags:
        out += "\n" + " · ".join(flags[:6])
    return out
