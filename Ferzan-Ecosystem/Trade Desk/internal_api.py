"""
internal_api.py

Small internal-only HTTP API so other Ferzan bots (Liq Bot's Earn
screen, for now) can read a user's real referral numbers without
touching Trade Desk's sqlite file directly. Mirrors the auth pattern
Launch Bot's api.py already uses for its own /internal/* routes:
shared INTERNAL_API_TOKEN header, loopback-only fallback when unset.

This process is separate from bot.py's Telegram polling loop -- run it
as its own systemd unit (see ferzan-trade-api.service) alongside it.

Run with: uvicorn internal_api:app --host 127.0.0.1 --port 8011
(loopback only -- nothing here is meant to be reachable off-box; other
bots on the same droplet call it over 127.0.0.1)
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException, Request

import db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("trade_internal_api")

app = FastAPI(title="Ferzan Trade Desk Internal API")


def _internal_ok(request: Request) -> bool:
    expected = (os.environ.get("INTERNAL_API_TOKEN") or "").strip()
    got = (request.headers.get("x-ferzan-internal") or "").strip()
    if expected and got == expected:
        return True
    if not expected:
        # Local droplet default: allow loopback only.
        client = (request.client.host if request.client else "") or ""
        return client in {"127.0.0.1", "::1"}
    return False


@app.get("/internal/referral-stats/{user_id}")
def internal_referral_stats(user_id: int, request: Request):
    if not _internal_ok(request):
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        stats = db.referral_stats(int(user_id))
    except Exception as exc:
        logger.error("referral_stats lookup failed for user=%s: %s", user_id, exc)
        raise HTTPException(status_code=500, detail="lookup failed") from exc
    return stats
