"""Explain a failed Tron transaction in plain words.   python tron_diag.py <txid>

Prints the result, the energy used against the fee limit, the TRX burned, and the revert message if the contract gave one.
Reads the public chain only. It never prints a key or a setting value.
"""

from __future__ import annotations

import sys

import tron_signer as ts


def decode_revert(hex_data: str) -> str:
    """Solidity Error(string) payload: 08c379a0 + offset + length + text."""
    h = (hex_data or "").lower().removeprefix("0x")
    if h.startswith("08c379a0") and len(h) >= 8 + 128:
        try:
            ln = int(h[8 + 64:8 + 128], 16)
            return bytes.fromhex(h[8 + 128:8 + 128 + ln * 2]).decode(errors="replace")
        except ValueError:
            return ""
    return ""


def explain(txid: str) -> str:
    info = ts._post("/wallet/gettransactioninfobyid", {"value": txid})
    tx = ts._post("/wallet/gettransactionbyid", {"value": txid})
    if not info.get("id"):
        return "Not found or not confirmed yet."
    rc = info.get("receipt") or {}
    result = rc.get("result") or "SUCCESS"
    raw = ((tx.get("raw_data") or {}).get("contract") or [{}])[0].get("parameter", {}).get("value", {})
    limit = (tx.get("raw_data") or {}).get("fee_limit") or 0
    burned = (info.get("fee") or 0) / 1e6
    energy = rc.get("energy_usage_total") or 0
    lines = [f"Result: {result}",
             f"Energy used: {energy:,}  ·  TRX burned: {burned:,.2f}  ·  fee limit: {limit / 1e6:,.0f} TRX",
             f"Sent with: {(raw.get('call_value') or 0) / 1e6:,.2f} TRX"]
    msg = decode_revert(((info.get("contractResult") or [""])[0]))
    if not msg and info.get("resMessage"):
        try:
            msg = bytes.fromhex(info["resMessage"]).decode(errors="replace")
        except ValueError:
            msg = ""
    if msg:
        lines.append(f"Contract said: {msg}")
    if result in {"OUT_OF_ENERGY", "OUT_OF_TIME"} or (limit and burned * 1e6 >= limit * 0.98):
        lines.append("Likely cause: the fee limit ran out. Raise TRON_CURVE_FEE_LIMIT_TRX and keep the wallet funded for it.")
    elif result == "REVERT":
        lines.append("Likely cause: the contract rejected the call (see 'Contract said'). Check graduation target, start time and max buy.")
    return "\n".join(lines)


if __name__ == "__main__":
    print(explain(sys.argv[1]) if len(sys.argv) > 1 else "usage: python tron_diag.py <txid>")
