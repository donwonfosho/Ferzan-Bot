"""
Launch a Tron coin from a user's Ferzan Trade Bot wallet (called by the Launch Bot, never by users).

  python tron_launch_exec.py info   '{"uid": 123}'
  python tron_launch_exec.py launch '{"uid": 123, "request_id": "...", "name": "...", "symbol": "...", "supply_raw": "..."}'

Prints one JSON line. The key never leaves this process: it is decrypted here the same way the Trade Bot
does it, used to sign one launchToken() call on the Ferzan Tron factory (TRON_FACTORY), and dropped.

Safety:
  - loads the Trade Bot's own settings (Trade Desk/.env) so it reads the same wallet database and key;
    refuses to run if the wallet master key is missing (it never creates one)
  - uses only an EXISTING wallet (never creates one) and checks the decrypted key matches the stored address
  - checks the TRX balance covers the factory fee + energy before sending (a short balance burns TRX for nothing)
  - one launch per request id: the txid is recorded before broadcasting, so a retry re-checks it instead of paying twice
  - the result is read back from the chain: the factory event, the creator, and the coin's total supply
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_env() -> None:
    from dotenv import dotenv_values

    for k, v in dotenv_values("/opt/ferzan/.env").items():  # shared settings, never overriding
        if v and k not in os.environ:
            os.environ[k] = v
    for k, v in dotenv_values(HERE / ".env").items():  # the Trade Bot's own settings win, exactly like bot.py
        if v:
            os.environ[k] = v


_load_env()
sys.path.insert(0, str(HERE))

MIN_SPARE_SUN = 25_000_000   # 25 TRX on top of the fee: a launch burns about 16 TRX of energy + bandwidth
FEE_LIMIT_SUN = 60_000_000   # hard cap on what one launch may burn
LOG = Path(os.getenv("TRON_LAUNCH_LOG") or "/opt/ferzan/app/tron_launches.json")
TOPIC = "TokenLaunched(address,address,string,string,uint256)"


def out(**kw) -> None:
    print(json.dumps(kw))
    sys.exit(0)


def _wallet(uid: int) -> tuple[str, str]:
    """(tron address, key hex) of the user's ACTIVE Trade Bot wallet."""
    import db
    import tron_signer as ts
    import user_wallets

    if not (os.getenv("FERZAN_MASTER_KEY") or "").strip() and not user_wallets.MASTER_PATH.exists():
        out(ok=False, error="wallet key store not found on this server")
    row = db.get_user_wallet(uid)
    if not row:
        out(ok=False, error="no_wallet")
    key = user_wallets._unlock(row["evm_key"])
    from eth_account import Account

    if Account.from_key("0x" + key.replace("0x", "")).address.lower() != str(row["evm_pub"]).lower():
        out(ok=False, error="wallet key check failed")
    addr, _ = ts.evm_key_to_tron(key)
    return addr, key


def _factory() -> str:
    f = (os.getenv("TRON_FACTORY") or "").strip()
    if not (f.startswith("T") and len(f) == 34):
        out(ok=False, error="TRON_FACTORY not set")
    return f


def _const(ts, contract: str, sig: str, params: str = "") -> str:
    r = ts._post("/wallet/triggerconstantcontract", {
        "owner_address": ts._to_hex(contract), "contract_address": ts._to_hex(contract),
        "function_selector": sig, "parameter": params})
    return (r.get("constant_result") or [""])[0]


def _launch_fee(ts, factory: str) -> int:
    """launchFeeSun() from the factory. An empty answer means the node failed: never guess 0 and build a tx that underpays."""
    raw = _const(ts, factory, "launchFeeSun()")
    if not raw:
        raise RuntimeError("Could not read the launch fee from the factory. Nothing sent.")
    return int(raw, 16)


def _balance(ts, addr: str) -> int:
    return int(ts._post("/wallet/getaccount", {"address": ts._to_hex(addr)}).get("balance") or 0)


def _b58(ts, hex41: str) -> str:
    raw = bytes.fromhex(hex41 if hex41.startswith("41") else "41" + hex41)
    return ts._b58encode(raw + ts._check(raw))


def _log_rw(fn):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(str(LOG) + ".lock", "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        data = json.loads(LOG.read_text()) if LOG.exists() else {}
        res = fn(data)
        tmp = LOG.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, LOG)
        return res


def _result(ts, txid: str, creator: str, factory: str, supply: int, wait_s: int = 90) -> dict:
    from eth_hash.auto import keccak

    deadline = time.time() + wait_s
    info = {}
    while time.time() < deadline:
        info = ts._post("/wallet/gettransactioninfobyid", {"value": txid})
        if info.get("id"):
            break
        time.sleep(3)
    if not info.get("id"):
        return {"ok": False, "pending": True, "txid": txid, "error": "not confirmed yet"}
    receipt = info.get("receipt") or {}
    burned = (info.get("fee") or 0) / 1e6
    if receipt.get("result") != "SUCCESS":
        why = receipt.get("result")
        try:  # say why, in words: the contract's own message if it gave one, and the energy spent against the cap
            import tron_diag

            said = tron_diag.decode_revert((info.get("contractResult") or [""])[0])
        except Exception:
            said = ""
        if said:
            why = f"{why}: {said[:80]}"
        elif why in {"OUT_OF_ENERGY", "OUT_OF_TIME"}:
            why = f"{why}: ran out of the {CURVE_FEE_LIMIT_SUN / 1e6:g} TRX fee limit after {burned:,.0f} TRX"
        return {"ok": False, "txid": txid, "burned_trx": burned, "error": f"failed on-chain ({why})"}
    topic = keccak(TOPIC.encode()).hex()
    fac_hex = ts._to_hex(factory)[2:]
    token = ""
    for lg in info.get("log") or []:
        t = lg.get("topics") or []
        if len(t) >= 3 and t[0].lower() == topic and str(lg.get("address", "")).lower() == fac_hex:
            if _b58(ts, "41" + t[2][-40:]) == creator:
                token = _b58(ts, "41" + t[1][-40:])
    if not token:
        return {"ok": False, "txid": txid, "error": "no launch event from the Ferzan factory"}
    total = int(_const(ts, token, "totalSupply()") or "0", 16)
    if total != supply:
        return {"ok": False, "txid": txid, "token": token, "error": f"supply mismatch ({total})"}
    return {"ok": True, "txid": txid, "token": token, "creator": creator, "burned_trx": burned}


def info(args: dict) -> None:
    import tron_signer as ts

    addr, _ = _wallet(int(args["uid"]))
    curve = bool(args.get("curve"))
    factory = _curve_factory() if curve else _factory()
    fee = _launch_fee(ts, factory)
    spare = CURVE_SPARE_SUN if curve else MIN_SPARE_SUN
    need = fee + spare + int(args.get("dev_buy_sun") or 0)
    bal = _balance(ts, addr)
    out(ok=True, address=addr, balance_trx=bal / 1e6, fee_trx=fee / 1e6, need_trx=need / 1e6, enough=bal >= need,
        energy_trx=(50 if curve else 16))


# ------------------------------------------------------------------ bonding curve launch --
CURVE_SPARE_SUN = 65_000_000       # a curve launch burns about 50 TRX of energy (measured on Nile)
CURVE_FEE_LIMIT_SUN = int(float(os.getenv("TRON_CURVE_FEE_LIMIT_TRX") or 120) * 1_000_000)  # a cap, only what is used is burned
CURVE_TOPIC = "CurveLaunched(address,address,address)"


def _curve_factory() -> str:
    f = (os.getenv("TRON_CURVE_FACTORY") or "").strip()
    if not (f.startswith("T") and len(f) == 34):
        out(ok=False, error="TRON_CURVE_FACTORY not set")
    return f


def _curve_result(ts, txid: str, creator: str, factory: str, wait_s: int = 90) -> dict:
    from eth_hash.auto import keccak

    deadline, info = time.time() + wait_s, {}
    while time.time() < deadline:
        info = ts._post("/wallet/gettransactioninfobyid", {"value": txid})
        if info.get("id"):
            break
        time.sleep(3)
    if not info.get("id"):
        return {"ok": False, "pending": True, "txid": txid, "error": "not confirmed yet"}
    receipt, burned = info.get("receipt") or {}, (info.get("fee") or 0) / 1e6
    if receipt.get("result") != "SUCCESS":
        return {"ok": False, "txid": txid, "burned_trx": burned, "error": f"failed on-chain ({receipt.get('result')})"}
    topic, fac_hex = keccak(CURVE_TOPIC.encode()).hex(), ts._to_hex(factory)[2:]
    for lg in info.get("log") or []:
        t = lg.get("topics") or []
        if (len(t) >= 4 and t[0].lower() == topic and str(lg.get("address", "")).lower() == fac_hex
                and _b58(ts, "41" + t[3][-40:]) == creator):
            curve, token = _b58(ts, "41" + t[1][-40:]), _b58(ts, "41" + t[2][-40:])
            if _b58(ts, "41" + _const(ts, curve, "token()")[-40:]) != token:
                return {"ok": False, "txid": txid, "error": "curve does not match its coin"}
            return {"ok": True, "txid": txid, "curve": curve, "token": token, "creator": creator, "burned_trx": burned}
    return {"ok": False, "txid": txid, "error": "no launch event from the Ferzan curve factory"}


def curve(args: dict) -> None:
    import tron_signer as ts
    from eth_abi import encode

    uid, rid = int(args["uid"]), str(args["request_id"])
    name, symbol, supply = str(args["name"]).strip(), str(args["symbol"]).strip(), int(args["supply_raw"])
    grad, start = int(args["grad_sun"]), int(args.get("start_time") or 0)
    max_buy, dev = int(args.get("max_buy_sun") or 0), int(args.get("dev_buy_sun") or 0)
    if not (0 < len(name.encode()) <= 64 and 0 < len(symbol.encode()) <= 16 and 10**6 <= supply <= 25 * 10**29):
        out(ok=False, error="bad name, ticker or supply")
    addr, key = _wallet(uid)
    factory = _curve_factory()
    if grad < int(_const(ts, factory, "minGradTarget()") or "0", 16):
        out(ok=False, error="graduation target is below the minimum")
    prev = _log_rw(lambda d: d.get(rid))
    if prev and prev.get("txid"):  # never a second launch for the same request
        res = _curve_result(ts, prev["txid"], addr, factory, wait_s=15)
        expired = not prev.get("broadcast") and res.get("pending") and time.time() - int(prev.get("at") or 0) > 120
        if not expired:
            out(**res, address=addr, repeat=True)
    fee = _launch_fee(ts, factory)
    bal = _balance(ts, addr)
    if bal < fee + dev + CURVE_SPARE_SUN:
        out(ok=False, error="low_balance", address=addr, balance_trx=bal / 1e6, need_trx=(fee + dev + CURVE_SPARE_SUN) / 1e6)
    params = encode(["string", "string", "uint256", "uint256", "uint256", "uint256"],
                    [name, symbol, supply, grad, start, max_buy]).hex()
    built = ts._post("/wallet/triggersmartcontract", {
        "owner_address": ts._to_hex(addr), "contract_address": ts._to_hex(factory),
        "function_selector": "launch(string,string,uint256,uint256,uint256,uint256)", "parameter": params,
        "call_value": fee + dev, "fee_limit": CURVE_FEE_LIMIT_SUN})
    tx = built.get("transaction") or {}
    if not tx.get("txID") or not (built.get("result") or {}).get("result"):
        out(ok=False, error="TronGrid could not build the launch: " + str(built.get("result") or built)[:160])
    txid = tx["txID"]
    _log_rw(lambda d: d.__setitem__(rid, {"txid": txid, "uid": uid, "address": addr, "at": int(time.time()), "broadcast": False}))
    ok, msg = ts._broadcast(tx, key)
    if not ok:
        _log_rw(lambda d: d.pop(rid, None))
        out(ok=False, error="broadcast failed: " + msg[:160])
    _log_rw(lambda d: d[rid].__setitem__("broadcast", True))
    out(**_curve_result(ts, txid, addr, factory), address=addr, fee_trx=fee / 1e6)


def launch(args: dict) -> None:
    import tron_signer as ts
    from eth_abi import encode

    uid, rid = int(args["uid"]), str(args["request_id"])
    name, symbol, supply = str(args["name"]).strip(), str(args["symbol"]).strip(), int(args["supply_raw"])
    if not (0 < len(name.encode()) <= 64 and 0 < len(symbol.encode()) <= 16 and 0 < supply <= 10**30):
        out(ok=False, error="bad name, ticker or supply")
    addr, key = _wallet(uid)
    factory = _factory()

    prev = _log_rw(lambda d: d.get(rid))
    if prev and prev.get("txid"):  # already sent once: report that launch, never send a second one
        res = _result(ts, prev["txid"], addr, factory, supply, wait_s=15)
        # only a send that never went out (crash before broadcast) and has since expired may be retried
        expired = not prev.get("broadcast") and res.get("pending") and time.time() - int(prev.get("at") or 0) > 120
        if not expired:
            out(**res, address=addr, repeat=True)

    fee = _launch_fee(ts, factory)
    bal = _balance(ts, addr)
    if bal < fee + MIN_SPARE_SUN:
        out(ok=False, error="low_balance", address=addr, balance_trx=bal / 1e6, need_trx=(fee + MIN_SPARE_SUN) / 1e6)

    params = encode(["string", "string", "uint256"], [name, symbol, supply]).hex()
    built = ts._post("/wallet/triggersmartcontract", {
        "owner_address": ts._to_hex(addr), "contract_address": ts._to_hex(factory),
        "function_selector": "launchToken(string,string,uint256)", "parameter": params,
        "call_value": fee, "fee_limit": FEE_LIMIT_SUN})
    tx = built.get("transaction") or {}
    if not tx.get("txID") or not (built.get("result") or {}).get("result"):
        out(ok=False, error="TronGrid could not build the launch: " + str(built.get("result") or built)[:160])
    txid = tx["txID"]

    def _mark(d):
        d[rid] = {"txid": txid, "uid": uid, "address": addr, "at": int(time.time()), "broadcast": False}
    _log_rw(_mark)
    ok, msg = ts._broadcast(tx, key)
    if not ok:
        _log_rw(lambda d: d.pop(rid, None))  # never reached the chain: a retry may send again
        out(ok=False, error="broadcast failed: " + msg[:160])

    def _sent(d):
        d[rid]["broadcast"] = True
    _log_rw(_sent)
    out(**_result(ts, txid, addr, factory, supply), address=addr, fee_trx=fee / 1e6)


if __name__ == "__main__":
    cmds = {"info": info, "launch": launch, "curve": curve}
    if len(sys.argv) != 3 or sys.argv[1] not in cmds:
        out(ok=False, error="usage: tron_launch_exec.py info|launch|curve '<json>'")
    try:
        cmds[sys.argv[1]](json.loads(sys.argv[2]))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - always answer with one JSON line, never a traceback with secrets
        out(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}")
