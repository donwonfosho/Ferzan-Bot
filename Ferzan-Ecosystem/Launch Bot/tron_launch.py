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


def curve_factory() -> str:
    return _setting("TRON_CURVE_FACTORY")


def curve_live() -> bool:
    f = curve_factory()
    return live() and _setting("TRON_CURVE_LIVE") == "1" and f.startswith("T") and len(f) == 34


def curve_min_grad_trx() -> float:
    try:
        return float(_setting("TRON_CURVE_MIN_GRAD_TRX") or 10000)
    except ValueError:
        return 10000.0


REAP_GRACE_S = 600


async def _kill(p) -> None:
    try:
        if p.returncode is None:
            p.kill()
        await asyncio.wait_for(p.wait(), 10)
    except Exception:  # noqa: BLE001
        pass


async def _reap(p, grace: float | None = None) -> None:
    """After run() gave up waiting: let the helper finish for `grace` seconds, then kill and collect it."""
    try:
        await asyncio.wait_for(p.communicate(), grace or REAP_GRACE_S)
    except Exception:  # noqa: BLE001
        await _kill(p)


async def run(cmd: str, args: dict, timeout: int = 150, script: str = "tron_launch_exec.py") -> dict:
    """Runs a Trade Desk launch helper (Tron or TON) with a clean environment; it loads the Trade Bot's
    own settings."""
    exe = EXEC.with_name(script)
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
    env["TRON_FACTORY"] = factory()
    if curve_factory():
        env["TRON_CURVE_FACTORY"] = curve_factory()
    p = None
    try:
        p = await asyncio.create_subprocess_exec(
            sys.executable, "-W", "ignore", str(exe), cmd, json.dumps(args), cwd=str(EXEC.parent), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        so, _ = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        # The helper may already have broadcast, so it is not killed on the spot; it gets a grace period to
        # finish, then is killed and reaped so a stuck helper can never pile up on the droplet.
        asyncio.get_running_loop().create_task(_reap(p))
        return {"ok": False, "pending": True, "error": "still waiting for the network"}
    except Exception as e:  # noqa: BLE001
        if p is not None:
            await _kill(p)
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


def verify_curve_launch(txid: str, creator: str, wait_s: int = 60) -> dict:
    """{'ok': True, 'token', 'curve'} only for a successful launch on the Ferzan Tron curve factory by `creator`."""
    from eth_hash.auto import keccak

    fac = curve_factory()
    if not fac or not txid:
        return {"ok": False, "error": "no curve factory or transaction"}
    info, deadline = {}, time.time() + wait_s
    while time.time() < deadline:
        info = _post("/wallet/gettransactioninfobyid", {"value": txid})
        if info.get("id"):
            break
        time.sleep(3)
    if (info.get("receipt") or {}).get("result") != "SUCCESS":
        return {"ok": False, "error": "transaction not confirmed as successful"}
    topic, fac_hex = keccak(b"CurveLaunched(address,address,address)").hex(), to_hex41(fac)[2:]
    for lg in info.get("log") or []:
        t = lg.get("topics") or []
        if (len(t) >= 4 and t[0].lower() == topic and str(lg.get("address", "")).lower() == fac_hex
                and to_b58(t[3]) == creator):
            return {"ok": True, "curve": to_b58(t[1]), "token": to_b58(t[2])}
    return {"ok": False, "error": "no Ferzan curve launch for this creator"}


# ---------------------------------------------------------------- Tron curves on the website ----
# The website builds nothing itself: these helpers read a Ferzan Tron curve and prepare unsigned calls that
# the visitor's own TronLink signs. Only curves made by our factory are ever touched.
_OURS: dict = {}
_STATE: dict = {}


def _const(contract: str, sig: str, param: str = "") -> str:
    h = to_hex41(contract)
    r = _post("/wallet/triggerconstantcontract", {"owner_address": h, "contract_address": h,
                                                  "function_selector": sig, "parameter": param})
    if any(x.get("ret") == "FAILED" for x in ((r.get("transaction") or {}).get("ret") or [])):
        raise ValueError(f"{sig} reverted")
    res = (r.get("constant_result") or [""])[0]
    if not res:
        raise ValueError(f"{sig}: no answer")
    return res


def _words(res: str) -> list:
    return [int(res[i:i + 64], 16) for i in range(0, len(res) - 63, 64)]


def _abi(types: list, vals: list) -> str:
    from eth_abi import encode

    return encode(types, vals).hex()


def _a20(b58: str) -> str:
    return "0x" + to_hex41(b58)[2:]


def is_our_curve(curve: str) -> bool:
    """True only for a curve whose factory() is the configured Ferzan Tron curve factory."""
    fac = curve_factory()
    if not fac or not curve.startswith("T") or len(curve) != 34:
        return False
    if _OURS.get(curve):
        return True
    try:
        ok = _const(curve, "factory()")[-40:] == to_hex41(fac)[2:] and _words(_const(curve, "token()"))[0] != 0
    except (ValueError, IndexError):
        return False
    if ok:
        _OURS[curve] = True
    return ok


def curve_state(curve: str, wallet: str = "") -> dict:
    """Public curve numbers (cached 5 s) plus the wallet's own balances when a wallet is given. Sun and coin units."""
    now = time.time()
    hit = _STATE.get(curve)
    if hit and now - hit[0] < 5:
        st = dict(hit[1])
    else:
        w = lambda sig: _words(_const(curve, sig))[0]  # noqa: E731
        token = to_b58(hex(w("token()"))[2:].zfill(40))
        st = {"token": token, "grad_target": w("gradTarget()"), "real": w("realEth()"), "sold": w("tokensSold()"),
              "supply": w("curveSupply()"), "start": w("startTime()"), "max_buy": w("maxBuyPerWallet()"),
              "complete": bool(w("complete()")), "graduated": bool(w("graduated()"))}
        _STATE[curve] = (now, st)
        st = dict(st)
    if wallet:
        tok, owner = st["token"], to_hex41(wallet)[2:].zfill(64)
        bal = _words(_const(tok, "balanceOf(address)", owner))[0]
        alw = _words(_const(tok, "allowance(address,address)", owner + to_hex41(curve)[2:].zfill(64)))[0]
        bought = _words(_const(curve, "boughtNative(address)", owner))[0]
        acct = _post("/wallet/getaccount", {"address": to_hex41(wallet)})
        st["mine"] = {"balance": bal, "allowance": alw, "bought": bought, "trx": int(acct.get("balance") or 0)}
    return st


def curve_quote(curve: str, side: str, amount: int) -> dict:
    if side == "buy":
        out, used, refund, fee = _words(_const(curve, "quoteBuy(uint256)", _abi(["uint256"], [amount])))[:4]
        return {"out": out, "used": used, "refund": refund, "fee": fee}
    out, fee = _words(_const(curve, "quoteSell(uint256)", _abi(["uint256"], [amount])))[:2]
    return {"out": out, "fee": fee}


def _build(owner: str, contract: str, sig: str, params: str, value: int, fee_limit: int) -> dict:
    built = _post("/wallet/triggersmartcontract", {
        "owner_address": to_hex41(owner), "contract_address": to_hex41(contract), "function_selector": sig,
        "parameter": params, "call_value": int(value), "fee_limit": int(fee_limit), "visible": False})
    tx = built.get("transaction") or {}
    if not tx.get("txID") or not (built.get("result") or {}).get("result"):
        raise ValueError("Tron could not prepare that: " + str(built.get("result") or built)[:120])
    return tx


def build_curve_trade(owner: str, curve: str, side: str, amount: int, min_out: int, referrer: str = "") -> dict:
    """Unsigned buy / sell / approve for a Ferzan Tron curve. `amount` is sun for a buy, coin units otherwise."""
    if not is_our_curve(curve):
        raise ValueError("That is not a Ferzan curve")
    if not (0 < amount < 2**96) or not (0 <= min_out < 2**96):
        raise ValueError("Amount looks wrong")
    ref = _a20(referrer) if referrer and referrer != owner and referrer.startswith("T") and len(referrer) == 34 else "0x" + "0" * 40
    if side == "buy":
        return _build(owner, curve, "buy(uint256,address)", _abi(["uint256", "address"], [min_out, ref]), amount, 150_000_000)
    if side == "sell":
        return _build(owner, curve, "sell(uint256,uint256,address)", _abi(["uint256", "uint256", "address"], [amount, min_out, ref]), 0, 150_000_000)
    if side == "approve":
        token = curve_state(curve)["token"]
        return _build(owner, token, "approve(address,uint256)", _abi(["address", "uint256"], [_a20(curve), amount]), 0, 50_000_000)
    raise ValueError("Unknown action")


def build_site_curve_launch(owner: str, name: str, symbol: str, supply_raw: int, grad_sun: int, start: int,
                            max_buy_sun: int, dev_sun: int) -> dict:
    """Unsigned launch() on the Ferzan Tron curve factory, for the visitor's TronLink."""
    fac = curve_factory()
    if not curve_live() or not fac:
        raise ValueError("Tron curve launches are not open yet")
    fee = _words(_const(fac, "launchFeeSun()"))[0]
    params = _abi(["string", "string", "uint256", "uint256", "uint256", "uint256"],
                  [name, symbol, int(supply_raw), int(grad_sun), int(start), int(max_buy_sun)])
    tx = _build(owner, fac, "launch(string,string,uint256,uint256,uint256,uint256)", params, fee + dev_sun, 150_000_000)
    return {"transaction": tx, "fee_sun": fee}
