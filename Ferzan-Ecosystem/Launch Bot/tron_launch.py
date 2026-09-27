"""
Tron coin launches. Creators launch from their Ferzan Trade Bot wallet, confirmed in the chat:
there is no Tron wallet-connect in the Mini App.

  launch_bot.py -> run("info"/"launch", ...)  runs Trade Desk/tron_launch_exec.py, which holds the wallet
                                               key, signs launchToken() on the Ferzan factory and reads it back
  api.py        -> verify_launch(...)          checks the launch again on-chain, independently, before the
                                               coin is recorded and posted

Factory: TRON_FACTORY (FerzanTronFactory: clones a fixed-supply TRC-20, fee fixed at deploy time).
Shown in the bot only when TRON_LAUNCH_LIVE=1 and TRON_FACTORY is set.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import requests

EXEC = Path(__file__).resolve().parent.parent / "Trade Desk" / "tron_launch_exec.py"
TOPIC = "TokenLaunched(address,address,string,string,uint256)"
_ALPH = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _setting(name: str) -> str:
    """From the service environment, else /opt/ferzan/.env, else Launch Bot/.env (api.py loads neither)."""
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    from dotenv import dotenv_values

    for f in ("/opt/ferzan/.env", str(Path(__file__).resolve().with_name(".env"))):
        v = (dotenv_values(f).get(name) or "").strip()
        if v:
            return v
    return ""


def factory() -> str:
    return _setting("TRON_FACTORY")


def live() -> bool:
    f = factory()
    return _setting("TRON_LAUNCH_LIVE") == "1" and f.startswith("T") and len(f) == 34


async def run(cmd: str, args: dict, timeout: int = 150, script: str = "tron_launch_exec.py") -> dict:
    """Runs a Trade Desk launch helper (Tron or TON) with a clean environment; it loads the Trade Bot's
    own settings."""
    exe = EXEC.with_name(script)
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    env["TRON_FACTORY"] = factory()
    try:
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-W", "ignore", str(exe), cmd, json.dumps(args), cwd=str(EXEC.parent), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        so, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "pending": True, "error": "still waiting for the network"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"could not start the wallet helper ({type(e).__name__})"}
    for line in reversed((so or b"").decode(errors="replace").splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {"ok": False, "error": "no answer from the wallet helper"}


# ---- independent on-chain check (api.py) ----
def _grid() -> str:
    return (_setting("TRONGRID_URL") or "https://api.trongrid.io").rstrip("/")


def _post(path: str, body: dict) -> dict:
    h = {"Content-Type": "application/json"}
    key = _setting("TRONGRID_API_KEY")
    if key:
        h["TRON-PRO-API-KEY"] = key
    try:
        return requests.post(_grid() + path, json=body, headers=h, timeout=20).json()
    except Exception:  # noqa: BLE001
        return {}


def _chk(raw: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]


def to_b58(hex20: str) -> str:
    raw = bytes.fromhex("41" + hex20[-40:])
    n = int.from_bytes(raw + _chk(raw), "big")
    s = ""
    while n:
        n, r = divmod(n, 58)
        s = _ALPH[r] + s
    return s


def to_hex41(addr: str) -> str:
    n = 0
    for ch in addr:
        n = n * 58 + _ALPH.index(ch)
    raw = n.to_bytes(25, "big")
    if raw[0] != 0x41 or _chk(raw[:21]) != raw[21:]:
        raise ValueError("not a Tron address")
    return raw[:21].hex()


def verify_launch(txid: str, creator: str, supply_raw: int, wait_s: int = 60) -> dict:
    """{'ok': True, 'token': T...} only if this txid is a successful Ferzan-factory launch by `creator`
    and the new coin's total supply is exactly supply_raw."""
    from eth_hash.auto import keccak

    fac = factory()
    if not fac or not txid:
        return {"ok": False, "error": "no factory or transaction"}
    info, deadline = {}, time.time() + wait_s
    while time.time() < deadline:
        info = _post("/wallet/gettransactioninfobyid", {"value": txid})
        if info.get("id"):
            break
        time.sleep(3)
    if (info.get("receipt") or {}).get("result") != "SUCCESS":
        return {"ok": False, "error": "transaction not confirmed as successful"}
    topic, fac_hex = keccak(TOPIC.encode()).hex(), to_hex41(fac)[2:]
    token = ""
    for lg in info.get("log") or []:
        t = lg.get("topics") or []
        if (len(t) >= 3 and t[0].lower() == topic and str(lg.get("address", "")).lower() == fac_hex
                and to_b58(t[2]) == creator):
            token = to_b58(t[1])
    if not token:
        return {"ok": False, "error": "no Ferzan launch event for this creator"}
    r = _post("/wallet/triggerconstantcontract", {"owner_address": to_hex41(token), "contract_address": to_hex41(token),
                                                  "function_selector": "totalSupply()", "parameter": ""})
    total = int(((r.get("constant_result") or ["0"])[0]) or "0", 16)
    if total != int(supply_raw):
        return {"ok": False, "error": f"supply mismatch ({total})"}
    return {"ok": True, "token": token}


def build_site_launch(owner: str, name: str, symbol: str, supply_raw) -> dict:
    """Unsigned launchToken() call for a website visitor's TronLink wallet (the wallet signs and sends it)."""
    from eth_abi import encode

    fac = factory()
    if not live() or not fac:
        raise ValueError("Tron launches are not open yet")
    fee_hex = ((_post("/wallet/triggerconstantcontract", {"owner_address": to_hex41(fac), "contract_address": to_hex41(fac),
                "function_selector": "launchFeeSun()", "parameter": ""}).get("constant_result") or ["0"])[0]) or "0"
    fee = int(fee_hex, 16)
    params = encode(["string", "string", "uint256"], [name, symbol, int(supply_raw)]).hex()
    built = _post("/wallet/triggersmartcontract", {
        "owner_address": to_hex41(owner), "contract_address": to_hex41(fac),
        "function_selector": "launchToken(string,string,uint256)", "parameter": params,
        "call_value": fee, "fee_limit": 60_000_000, "visible": False})
    tx = built.get("transaction") or {}
    if not tx.get("txID") or not (built.get("result") or {}).get("result"):
        raise ValueError("Tron could not prepare the launch: " + str(built.get("result") or built)[:120])
    return {"transaction": tx, "fee_sun": fee}
