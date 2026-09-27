"""
Add SunSwap V2 liquidity for a Tron coin from a user's Ferzan Trade Bot wallet (called by the Launch Bot).

  python tron_liquidity_exec.py info    '{"uid": 1, "token": "T..."}'
  python tron_liquidity_exec.py prepare '{"uid": 1, "token": "T...", "trx_sun": 100000000, "token_raw": "..."}'
  python tron_liquidity_exec.py add     '{"uid": 1, "token": "T...", "trx_sun": ..., "token_raw": "...", "burn": true, "rid": "..."}'

prepare: approves the router for the tokens if needed (a small, separate transaction), then SIMULATES the pool
         creation on Tron to get its exact energy, and reports the cost. Nothing else is sent.
add:     sends addLiquidityETH with minimums, then (burn=true) sends the LP tokens to Tron's black-hole address
         so the liquidity can never be pulled. One add per rid.
Same wallet/key rules as tron_launch_exec.py (Trade Bot settings, existing wallet only, key must match).
"""
from __future__ import annotations

import json
import os
import sys
import time

import tron_launch_exec as common  # loads the Trade Bot settings; wallet + log helpers

out = common.out
BLACK_HOLE = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"  # Tron's zero address: tokens sent here are gone forever
MAX_FEE_LIMIT_SUN = 2_000_000_000                  # never let one pool creation burn more than 2,000 TRX
SPARE_SUN = 10_000_000                             # 10 TRX left over for the LP burn + rounding


def _ts():
    import tron_signer as ts

    return ts


def _hex(a: str) -> str:
    return _ts()._to_hex(a)


def _addr_word(words: list[int]) -> str:
    return "41" + f"{words[0]:040x}"[-40:] if words else ""


def _router_info(owner_hex: str) -> tuple[str, str, str]:
    ts = _ts()
    r = _hex(ts.ROUTER)
    factory = _addr_word(ts._const(r, owner_hex, "factory()", ""))
    wtrx = _addr_word(ts._const(r, owner_hex, "WETH()", "")) or _hex(ts.WTRX)
    return r, factory, wtrx


def _pair(factory: str, token_hex: str, wtrx: str, owner_hex: str) -> str:
    ts = _ts()
    words = ts._const(factory, owner_hex, "getPair(address,address)", ts._w(token_hex) + ts._w(wtrx))
    p = _addr_word(words)
    return "" if not p or int(p[2:], 16) == 0 else p


def _energy_fee() -> int:
    for p in (_ts()._post("/wallet/getchainparameters", {}).get("chainParameter") or []):
        if p.get("key") == "getEnergyFee":
            return int(p.get("value") or 100)
    return 210  # conservative if the node does not say


def _revert_reason(res: dict) -> str:
    raw = ((res.get("constant_result") or [""])[0] or "")
    if raw.startswith("08c379a0") and len(raw) >= 136:
        try:
            n = int(raw[72:136], 16)
            return bytes.fromhex(raw[136:136 + 2 * n]).decode(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    msg = (res.get("result") or {}).get("message") or ""
    try:
        return bytes.fromhex(msg).decode(errors="replace")
    except Exception:  # noqa: BLE001
        return str(msg)[:120]


def _sim_ok(sim: dict) -> bool:
    """A simulated call succeeded: the node ran it AND the contract did not revert."""
    ret = ((sim.get("transaction") or {}).get("ret") or [{}])[0].get("ret")
    return bool((sim.get("result") or {}).get("result")) and ret != "FAILED" and int(sim.get("energy_used") or 0) > 0


def _balances(owner_hex: str, token_hex: str) -> tuple[int, int, int]:
    ts = _ts()
    trx = ts._trx_balance(owner_hex)
    bal = (ts._const(token_hex, owner_hex, "balanceOf(address)", ts._w(owner_hex)) or [0])[0]
    dec = (ts._const(token_hex, owner_hex, "decimals()", "") or [6])[0]
    return trx, bal, dec


def _add_params(token_hex: str, token_raw: int, trx_sun: int, owner_hex: str, exists: bool) -> str:
    ts = _ts()
    # new pool: our amounts ARE the price, so the minimums are the amounts; existing pool: allow 2% drift
    tmin = token_raw if not exists else token_raw * 98 // 100
    emin = trx_sun if not exists else trx_sun * 98 // 100
    return (ts._w(token_hex) + f"{token_raw:064x}" + f"{tmin:064x}" + f"{emin:064x}" + ts._w(owner_hex)
            + f"{int(time.time()) + 600:064x}")


def _args(a: dict):
    addr, key = common._wallet(int(a["uid"]))
    token = str(a["token"]).strip()
    if not (token.startswith("T") and len(token) == 34):
        out(ok=False, error="not a Tron token address")
    return addr, key, _hex(addr), _hex(token)


def info(a: dict) -> None:
    addr, _key, owner, token = _args(a)
    trx, bal, dec = _balances(owner, token)
    router, factory, wtrx = _router_info(owner)
    pair = _pair(factory, token, wtrx, owner) if factory else ""
    out(ok=True, address=addr, trx=trx / 1e6, token_balance=str(bal), decimals=dec,
        pair=common._b58(_ts(), pair) if pair else "", energy_fee_sun=_energy_fee())


def prepare(a: dict) -> None:
    ts = _ts()
    addr, key, owner, token = _args(a)
    trx_sun, token_raw = int(a["trx_sun"]), int(a["token_raw"])
    if trx_sun < 1_000_000 or token_raw <= 0:
        out(ok=False, error="amounts too small")
    trx, bal, dec = _balances(owner, token)
    if bal < token_raw:
        out(ok=False, error="not enough tokens", token_balance=str(bal))
    router, factory, wtrx = _router_info(owner)
    if not factory:
        out(ok=False, error="could not read the SunSwap factory")
    exists = bool(_pair(factory, token, wtrx, owner))
    approved_tx = ""
    allow = (ts._const(token, owner, "allowance(address,address)", ts._w(owner) + ts._w(router)) or [0])[0]
    if allow < token_raw:
        if trx < 30_000_000:
            out(ok=False, error="low_balance", address=addr, trx=trx / 1e6, need_trx=30.0)
        built = ts._post("/wallet/triggersmartcontract", {
            "owner_address": owner, "contract_address": token, "function_selector": "approve(address,uint256)",
            "parameter": ts._w(router) + "f" * 64, "fee_limit": 100_000_000, "call_value": 0, "visible": False})
        res, link, _b = ts._send_and_wait(built, key)
        if res != "SUCCESS":
            out(ok=False, error=f"approve did not confirm ({res})", link=link)
        approved_tx = link
        trx = ts._trx_balance(owner)
    sim = ts._post("/wallet/triggerconstantcontract", {
        "owner_address": owner, "contract_address": router,
        "function_selector": "addLiquidityETH(address,uint256,uint256,uint256,address,uint256)",
        "parameter": _add_params(token, token_raw, trx_sun, owner, exists), "call_value": trx_sun, "visible": False})
    if not _sim_ok(sim):
        out(ok=False, error="SunSwap would reject this pool: " + _revert_reason(sim), approved=approved_tx)
    energy = int(sim.get("energy_used") or 0) + int(sim.get("energy_penalty") or 0)
    fee = _energy_fee()
    burn_sun = energy * fee
    need = trx_sun + int(burn_sun * 1.2) + SPARE_SUN
    out(ok=True, address=addr, exists=exists, energy=energy, energy_cost_trx=burn_sun / 1e6, trx=trx / 1e6,
        need_trx=need / 1e6, enough=trx >= need, approved=approved_tx, decimals=dec)


def add(a: dict) -> None:
    ts = _ts()
    addr, key, owner, token = _args(a)
    trx_sun, token_raw, rid = int(a["trx_sun"]), int(a["token_raw"]), str(a["rid"])
    common.LOG = common.Path(os.getenv("TRON_LP_LOG") or "/opt/ferzan/app/tron_liquidity.json")
    prev = common._log_rw(lambda d: d.get(rid))
    if prev:
        out(ok=False, error="this liquidity request was already sent", link=prev.get("link", ""))
    router, factory, wtrx = _router_info(owner)
    exists = bool(_pair(factory, token, wtrx, owner))
    params = _add_params(token, token_raw, trx_sun, owner, exists)
    sim = ts._post("/wallet/triggerconstantcontract", {
        "owner_address": owner, "contract_address": router,
        "function_selector": "addLiquidityETH(address,uint256,uint256,uint256,address,uint256)",
        "parameter": params, "call_value": trx_sun, "visible": False})
    if not _sim_ok(sim):
        out(ok=False, error="SunSwap would reject this pool: " + _revert_reason(sim))
    energy = int(sim.get("energy_used") or 0) + int(sim.get("energy_penalty") or 0)
    cost = energy * _energy_fee()
    fee_limit = min(MAX_FEE_LIMIT_SUN, max(100_000_000, int(cost * 1.5)))
    trx = ts._trx_balance(owner)
    if trx < trx_sun + int(cost * 1.2) + SPARE_SUN:
        out(ok=False, error="low_balance", address=addr, trx=trx / 1e6)
    built = ts._post("/wallet/triggersmartcontract", {
        "owner_address": owner, "contract_address": router,
        "function_selector": "addLiquidityETH(address,uint256,uint256,uint256,address,uint256)",
        "parameter": params, "fee_limit": fee_limit, "call_value": trx_sun, "visible": False})
    txid = (built.get("transaction") or {}).get("txID") or ""
    common._log_rw(lambda d: d.__setitem__(rid, {"txid": txid, "uid": int(a["uid"]), "token": a["token"],
                                               "at": int(time.time()), "link": f"https://tronscan.org/#/transaction/{txid}"}))
    res, link, burned = ts._send_and_wait(built, key, timeout_s=90)
    if res != "SUCCESS":
        if res.startswith("not sent"):
            common._log_rw(lambda d: d.pop(rid, None))
        out(ok=False, error=f"pool not created ({res})", link=link, burned_trx=burned)
    pair = _pair(factory, token, wtrx, owner)
    result = {"ok": True, "link": link, "burned_trx": burned, "pair": common._b58(ts, pair) if pair else "",
              "address": addr, "lp_burned": False}
    if a.get("burn") and pair:
        lp = (ts._const(pair, owner, "balanceOf(address)", ts._w(owner)) or [0])[0]
        if lp > 0:
            b = ts._post("/wallet/triggersmartcontract", {
                "owner_address": owner, "contract_address": pair, "function_selector": "transfer(address,uint256)",
                "parameter": ts._w(_hex(BLACK_HOLE)) + f"{lp:064x}", "fee_limit": 100_000_000, "call_value": 0,
                "visible": False})
            r2, link2, _b2 = ts._send_and_wait(b, key)
            result.update(lp_burned=(r2 == "SUCCESS"), burn_link=link2, burn_result=r2)
    out(**result)


if __name__ == "__main__":
    cmds = {"info": info, "prepare": prepare, "add": add}
    if len(sys.argv) != 3 or sys.argv[1] not in cmds:
        out(ok=False, error="usage: tron_liquidity_exec.py info|prepare|add '<json>'")
    try:
        cmds[sys.argv[1]](json.loads(sys.argv[2]))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - one JSON line, never a traceback with secrets
        out(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}")
