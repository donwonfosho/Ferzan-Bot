"""Public, read-only status for the website footer: normal / degraded / down.

It answers three questions and nothing else: are the Ferzan services running, does the Solana network answer,
and is the Launch API (this process) answering. It never returns service names, hosts, errors or keys.
Results are cached for 20 seconds so a busy site cannot hammer systemd or the RPC.

  cd "Launch Bot" && python -m unittest tests.test_status_check
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request

CACHE_SECONDS = 20
RPC_TIMEOUT = 4

# What each public component means. A component is "normal" only if every listed service is active.
GROUPS = [
    ("Trade Bot", ["ferzan-trade", "ferzan-trade-api", "ferzan-webapp"]),
    ("Launch Bot", ["ferzan-launch"]),
    ("Curve indexer", ["ferzan-curve-indexer"]),
]

_lock = threading.Lock()
_cache: dict = {"at": 0.0, "value": None}


def _service_active(name: str) -> bool | None:
    """True / False, or None when systemd cannot be asked (then we say 'unknown', never 'normal')."""
    try:
        r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (r.stdout or "").strip()
    if out == "active":
        return True
    if out in ("inactive", "failed", "activating", "deactivating"):
        return False
    return None


def _solana_answers(rpc_url: str) -> bool | None:
    if not rpc_url:
        return None
    try:
        req = urllib.request.Request(
            rpc_url,
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "getSlot"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as resp:  # noqa: S310 (our own configured RPC)
            body = json.loads(resp.read().decode() or "{}")
        return bool(body.get("result"))
    except Exception:  # noqa: BLE001 - a failed probe IS the answer
        return False


def combine(components: list[dict]) -> str:
    """normal / degraded / down / unknown from the component list."""
    known = [c for c in components if c["status"] != "unknown"]
    if not known:
        return "unknown"
    if all(c["status"] == "normal" for c in known):
        return "normal"
    core = [c for c in known if c["name"] in ("Trade Bot", "Launch Bot")]
    if len(core) == 2 and all(c["status"] == "down" for c in core):
        return "down"
    return "degraded"


def build(active=_service_active, solana=_solana_answers, rpc_url: str = "", now: float | None = None) -> dict:
    comps: list[dict] = [{"name": "Launch API", "status": "normal"}]  # we are answering
    for name, services in GROUPS:
        states = [active(s) for s in services]
        if any(s is None for s in states):
            status = "unknown"
        else:
            status = "normal" if all(states) else "down"
        comps.append({"name": name, "status": status})
    sol = solana(rpc_url)
    comps.append({"name": "Solana network", "status": "unknown" if sol is None else ("normal" if sol else "down")})
    return {"status": combine(comps), "updated": int(now if now is not None else time.time()), "components": comps}


def snapshot(rpc_url: str = "") -> dict:
    with _lock:
        now = time.time()
        if _cache["value"] is not None and now - _cache["at"] < CACHE_SECONDS:
            return _cache["value"]
    value = build(rpc_url=rpc_url, now=now)
    with _lock:
        _cache["value"], _cache["at"] = value, time.time()
    return value
