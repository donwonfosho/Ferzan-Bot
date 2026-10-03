"""Safety line for TON jettons (tonapi) so TON buy cards get the same verdict Solana/EVM/Tron cards do.

It only reports what the API actually returned. If the lookup fails it returns "" (no line), never a green check.
"""

from __future__ import annotations

import os

import requests


def _headers() -> dict:
    h = {"Accept": "application/json"}
    key = (os.environ.get("TONAPI_KEY") or "").strip()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def ton_line(jetton: str) -> str:
    try:
        r = requests.get(f"https://tonapi.io/v2/jettons/{jetton}", headers=_headers(), timeout=6)
        if r.status_code != 200:
            return ""
        d = r.json() or {}
    except Exception:  # noqa: BLE001
        return ""
    ver = str(d.get("verification") or "").lower()
    admin = d.get("admin")
    holders = int(d.get("holders_count") or 0)
    flags = []
    if ver == "blacklist":
        flags.append("🚨 flagged as scam by TON registries")
    if d.get("mintable") and admin:
        flags.append("⚠️ admin can still mint more")
    if ver == "whitelist":
        flags.append("✅ verified jetton")
    elif ver != "blacklist":
        flags.append("ℹ️ not on the TON verified list")
    if ver == "blacklist":
        head = "🚨 SCAM FLAG"
    elif any(f.startswith("⚠️") for f in flags):
        head = "⚠️ Contract flags"
    else:
        head = "✅ No scam flag"
    out = head + (f"  ·  {holders:,} holders" if holders else "")
    return out + "\n" + " · ".join(flags)
