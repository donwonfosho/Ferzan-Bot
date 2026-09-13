"""TON live swap via STON.fi. Needs pytoniq on the droplet for the send."""

from __future__ import annotations

import os

import requests

from evm_signer import live_enabled, max_usd

STON = "https://api.ston.fi"
# Official TON asset id used by STON.fi
TON_ASSET = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"


def _headers() -> dict:
    return {"Accept": "application/json"}


def simulate(ask: str, offer_units: str, slip: str = "0.05") -> dict:
    r = requests.get(
        f"{STON}/v1/swap/simulate",
        params={
            "offer_address": TON_ASSET,
            "ask_address": ask,
            "offer_units": str(offer_units),
            "slippage_tolerance": slip,
        },
        headers=_headers(),
        timeout=20,
    )
    try:
        return r.json()
    except Exception:
        return {"error": r.text[:180], "status": r.status_code}


def buy_ton(jetton: str, usd: float, secret: str | None = None) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live buys OFF."
    jetton = (jetton or "").strip()
    if not jetton.startswith(("EQ", "UQ", "kQ")):
        return False, "Need a TON jetton address (EQ… / UQ…)."
    usd = min(max(1.0, float(usd)), max_usd())
    try:
        from price_fetcher import get_price_usd

        px = float(get_price_usd("the-open-network") or 5)
    except Exception:
        px = 5.0
    nano = max(10**7, int((usd / max(px, 1e-9)) * 10**9))
    sim = simulate(jetton, str(nano))
    if sim.get("error") or sim.get("status", 200) >= 400:
        return False, "STON.fi quote failed: " + str(sim.get("error") or sim)[:180]
    try:
        from pytoniq import LiteBalancer
        from pytoniq_core.crypto.signature import sign_message  # noqa: F401
    except Exception:
        ask = sim.get("ask_units") or sim.get("min_ask_units") or "?"
        return (
            False,
            "STON.fi priced the buy, but TON send needs pytoniq on the droplet.\n"
            "ssh: pip install pytoniq pytoniq-core\n"
            f"Quote ask_units={ask}. Fund the TON wallet, then retry.",
        )
    return False, "pytoniq is installed. Wire WalletV4 send next restart — quote was good."


def sell_ton(jetton: str, secret: str | None = None) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live sells OFF."
    return False, "TON sell ships with the pytoniq send. pip install pytoniq pytoniq-core first."


def status_text() -> str:
    try:
        import pytoniq  # noqa: F401

        extra = "pytoniq ON"
    except Exception:
        extra = "pytoniq missing — pip install pytoniq pytoniq-core"
    return f"TON STON.fi quotes live. Send: {extra}"
