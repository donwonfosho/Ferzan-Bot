"""TON network relay for the Ferzan website.

The website's TON launch (signing with the Ferzan account wallet) was being refused by the public toncenter
endpoint with 429 "Ratelimit exceed" because it called it without an API key. These two routes let the site
read a wallet's state and broadcast an ALREADY SIGNED message through the server, which uses the Ferzan
toncenter key, retries on 429/5xx, and falls back to tonapi.io. Nothing here signs, holds keys or moves money:
a signed message can only do what its signer already authorised, and sending the same one twice is harmless
(same seqno). Both routes are rate limited per caller.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import threading
import time

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

_ADDR_RE = re.compile(r"^((EQ|UQ|kQ|0Q)[A-Za-z0-9_-]{46}|-?[01]:[0-9a-fA-F]{64})$")
MAX_BOC_B64 = 8192
_rate: dict = {}
_rate_lock = threading.Lock()
_cache: dict = {}
_file_env: dict = {}


def _env(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    path = os.environ.get("FERZAN_ENV_FILE", "/opt/ferzan/.env")
    try:
        mt = os.stat(path).st_mtime
        if _file_env.get("k") != (path, mt):
            vals = {}
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if "=" in line and not line.lstrip().startswith("#"):
                        k, _, val = line.partition("=")
                        vals[k.strip()] = val.strip().strip("'\"")
            _file_env.update(k=(path, mt), v=vals)
        return _file_env.get("v", {}).get(name, "")
    except OSError:
        return ""


def _rate_ok(who: str, bucket: str, limit: int, window: int) -> bool:
    now = time.time()
    key = (bucket, who)
    with _rate_lock:
        hits = [t for t in _rate.get(key, []) if now - t < window]
        if len(hits) >= limit:
            _rate[key] = hits
            return False
        hits.append(now)
        _rate[key] = hits
    return True


def _who(request: Request) -> str:
    fwd = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "?")


def _toncenter(path: str, *, post: bool = False, params=None, body=None):
    key = _env("TONCENTER_API_KEY")
    headers = {"X-API-Key": key} if key else {}
    base = (_env("TONCENTER_URL") or "https://toncenter.com/api/v2").rstrip("/")
    return (requests.post(f"{base}/{path}", json=body, headers=headers, timeout=12) if post
            else requests.get(f"{base}/{path}", params=params, headers=headers, timeout=12))


def _retry(fn, tries: int = 3):
    """fn() -> (ok, value, retryable). Waits 1s, 2s between tries; returns the last (ok, value)."""
    last = (False, "TON network did not answer")
    for i in range(tries):
        try:
            ok, val, retry = fn()
        except Exception as exc:  # network error: worth one more try
            ok, val, retry = False, f"TON network error ({type(exc).__name__})", True
        if ok:
            return True, val
        last = (False, val)
        if not retry or i == tries - 1:
            break
        time.sleep(1 << i)
    return last


@router.get("/api/ton/relay/wallet/{address}")
def wallet_state(address: str, request: Request):
    """{'state': 'uninitialized'|'active'|..., 'seqno': int, 'balance': nanoton} for a wallet address."""
    if not _ADDR_RE.match(address or ""):
        raise HTTPException(400, "That does not look like a TON address")
    if not _rate_ok(_who(request), "wallet", 60, 60):
        raise HTTPException(429, "Slow down a little")
    hit = _cache.get(address)
    if hit and time.time() - hit[0] < 4:
        return hit[1]

    def go():
        r = _toncenter("getWalletInformation", params={"address": address})
        if r.status_code in (429, 500, 502, 503, 504):
            return False, f"TON network busy ({r.status_code})", True
        j = r.json()
        if not j.get("ok"):
            return False, str(j.get("error") or "TON network refused")[:160], False
        return True, j.get("result") or {}, False

    ok, res = _retry(go)
    if not ok:
        raise HTTPException(502, res)
    out = {
        "state": str(res.get("account_state") or "uninitialized"),
        "seqno": int(res.get("seqno") or 0),
        "balance": int(res.get("balance") or 0),
        "wallet_id": res.get("wallet_id"),
    }
    _cache[address] = (time.time(), out)
    return out


class SendBody(BaseModel):
    boc: str


@router.post("/api/ton/relay/send")
def send_boc(body: SendBody, request: Request):
    """Broadcast a signed external message (base64 BOC). Tries toncenter (with key, retried), then tonapi.io."""
    boc = (body.boc or "").strip()
    if not boc or len(boc) > MAX_BOC_B64:
        raise HTTPException(400, "That message is empty or too large")
    try:
        raw = base64.b64decode(boc.replace("-", "+").replace("_", "/") + "=" * (-len(boc) % 4), validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, "That is not a valid signed message")
    if not raw.startswith(b"\xb5\xee\x9c\x72"):  # BOC magic
        raise HTTPException(400, "That is not a valid signed message")
    if not _rate_ok(_who(request), "send", 20, 60):
        raise HTTPException(429, "Slow down a little")
    std = base64.b64encode(raw).decode()

    def via_toncenter():
        r = _toncenter("sendBoc", post=True, body={"boc": std})
        if r.status_code in (429, 500, 502, 503, 504):
            return False, f"TON network busy ({r.status_code})", True
        j = r.json()
        if j.get("ok"):
            return True, "toncenter", False
        return False, str(j.get("error") or j.get("result") or "TON network refused")[:200], False

    ok, via = _retry(via_toncenter)
    if ok:
        return {"ok": True, "via": via}
    first = via

    def via_tonapi():
        key = _env("TONAPI_KEY")
        r = requests.post("https://tonapi.io/v2/blockchain/message", json={"boc": std},
                          headers={"Authorization": f"Bearer {key}"} if key else {}, timeout=12)
        if r.status_code in (200, 201, 202):
            return True, "tonapi", False
        if r.status_code in (429, 500, 502, 503, 504):
            return False, f"TON network busy ({r.status_code})", True
        try:
            msg = str((r.json() or {}).get("error") or r.text)[:200]
        except ValueError:
            msg = r.text[:200]
        return False, msg, False

    ok2, via2 = _retry(via_tonapi, tries=2)
    if ok2:
        return {"ok": True, "via": via2}
    raise HTTPException(502, f"TON did not take the message: {first}; backup: {via2}")
