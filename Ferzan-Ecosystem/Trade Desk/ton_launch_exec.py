"""
Launch a TON coin from a user's Ferzan Trade Bot wallet (called by the Launch Bot, never by users).

  python ton_launch_exec.py info   '{"uid": 123, "need_nano": 600000000}'
  python ton_launch_exec.py launch '{"uid": 123, "request_id": "...", "messages": [...]}'

`messages` are the same three launch messages the Mini App hands to TonConnect wallets (built by the Launch
Bot API from the pinned jetton code): deploy+mint, drop admin, Ferzan fee. Here the Trade Bot's own TON
wallet (WalletV4R2, same key the Trade Bot trades with) signs all three in one external message.

Safety (same rules as tron_launch_exec.py):
  - the Trade Bot's own settings + existing wallet only; the decrypted key must match the stored Solana address
  - the launch messages are checked: at most 3, the first must deploy exactly the address it is sent to,
    and the total can never exceed MAX_TOTAL_NANO
  - balance must cover the messages + wallet gas before anything is signed
  - one launch per request id, and never a second launch once the coin contract exists
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

import tron_launch_exec as common  # loads the Trade Bot settings exactly like the Tron helper

out = common.out
MAX_TOTAL_NANO = 2_000_000_000   # 2 TON: a launch sends about 0.6
GAS_SPARE_NANO = 100_000_000     # 0.1 TON left for the wallet's own fees (and its first-use deploy)
LAUNCH_TTL_S = 120               # the signed launch is valid this long; we wait past it, so "not confirmed" = can't land
LOG = Path(os.getenv("TON_LAUNCH_LOG") or "/opt/ferzan/app/ton_launches.json")


def _secret(uid: int) -> str:
    import db
    import user_wallets

    if not (os.getenv("FERZAN_MASTER_KEY") or "").strip() and not user_wallets.MASTER_PATH.exists():
        out(ok=False, error="wallet key store not found on this server")
    row = db.get_user_wallet(uid)
    if not row:
        out(ok=False, error="no_wallet")
    secret = user_wallets._unlock(row["sol_key"])
    import ton_signer

    from solders.keypair import Keypair

    if str(Keypair.from_bytes(ton_signer._ton_keypair_bytes(secret)).pubkey()) != str(row["sol_pub"]):
        out(ok=False, error="wallet key check failed")
    return secret


def _log(fn):
    common.LOG = LOG
    return common._log_rw(fn)


def _parse(messages):
    from pytoniq_core import Address, Cell, StateInit

    if not isinstance(messages, list) or not 1 <= len(messages) <= 3:
        out(ok=False, error="bad launch messages")
    parsed, total = [], 0
    for i, m in enumerate(messages):
        to = Address(str(m["address"]))
        value = int(m["amount"])
        body = Cell.one_from_boc(base64.b64decode(m["payload"])) if m.get("payload") else None
        si = None
        if m.get("stateInit"):
            c = Cell.one_from_boc(base64.b64decode(m["stateInit"]))
            if len(c.refs) != 2:
                out(ok=False, error="unexpected coin contract layout")
            si = StateInit(code=c.refs[0], data=c.refs[1])
            if si.serialize().hash != c.hash:
                si = StateInit.deserialize(c.begin_parse())
            if si.serialize().hash != c.hash or c.hash != to.hash_part:
                out(ok=False, error="coin contract does not match its address")
        elif i == 0:
            out(ok=False, error="first message must deploy the coin")
        total += value
        parsed.append((to, value, body, si))
    if total > MAX_TOTAL_NANO:
        out(ok=False, error="launch costs more than the safety limit")
    return parsed, total


async def _state(provider, addr):
    st = await provider.get_account_state(addr)
    kind = str(getattr(getattr(st, "state", None), "type_", "") or "").lower()
    if not kind:
        text = repr(st).lower()
        kind = "active" if ("active" in text and "uninit" not in text and "nonexist" not in text) else "unknown"
    return kind == "active", int(getattr(st, "balance", 0) or 0)


async def _info(seed64: bytes):
    from pytoniq import LiteBalancer, WalletV4R2

    provider = LiteBalancer.from_mainnet_config(trust_level=2)
    await provider.start_up()
    try:
        w = await WalletV4R2.from_private_key(provider, seed64)
        _, bal = await _state(provider, w.address)
        return w.address.to_str(is_user_friendly=True, is_bounceable=False), bal
    finally:
        await provider.close_all()


def info(args: dict) -> None:
    import ton_signer

    seed64 = ton_signer._ton_keypair_bytes(_secret(int(args["uid"])))
    addr, bal = ton_signer._run_async(_info(seed64))
    need = int(args.get("need_nano") or 600_000_000) + GAS_SPARE_NANO
    out(ok=True, address=addr, balance_ton=bal / 1e9, need_ton=need / 1e9, enough=bal >= need)


async def _launch(seed64: bytes, rid: str, parsed, total: int):
    from pytoniq import LiteBalancer, WalletV4R2

    import ton_signer as tsg

    provider = LiteBalancer.from_mainnet_config(trust_level=2)
    await provider.start_up()
    try:
        w = await WalletV4R2.from_private_key(provider, seed64)
        addr = w.address.to_str(is_user_friendly=True, is_bounceable=False)
        minter = parsed[0][0]
        deployed, _ = await _state(provider, minter)
        if deployed:  # the coin already exists: report it, never send a second launch
            return {"ok": True, "already": True, "address": addr, "minter": minter.to_str()}
        _, bal = await _state(provider, w.address)
        if bal < total + GAS_SPARE_NANO:
            return {"ok": False, "error": "low_balance", "address": addr, "balance_ton": bal / 1e9,
                    "need_ton": (total + GAS_SPARE_NANO) / 1e9}
        def _msg(to, value, body, si):
            try:
                return w.create_wallet_internal_message(destination=to, value=value, body=body, state_init=si,
                                                        bounce=bool(to.is_bounceable))
            except TypeError:  # older pytoniq: bounce follows its own default
                return w.create_wallet_internal_message(destination=to, value=value, body=body, state_init=si)
        msgs = [_msg(*p) for p in parsed]
        seqno = await tsg._seqno_for_send(provider, w)
        signed = w.raw_create_transfer_msg(private_key=w.private_key, seqno=seqno, wallet_id=w.wallet_id,
                                           messages=msgs, valid_until=int(time.time()) + LAUNCH_TTL_S)
        ext = w.create_external_msg(dest=w.address, state_init=w.state_init if seqno == 0 else None, body=signed)
        cell = ext.serialize()
        h = cell.hash.hex()

        def _mark(d):
            d[rid] = {"hash": h, "seqno": seqno, "address": addr, "minter": minter.to_str(), "at": int(time.time())}
        _log(_mark)
        ok, why = await tsg.broadcast(provider, cell.to_boc())
        if not ok:
            _log(lambda d: d.pop(rid, None))  # nobody took it: a retry may send again
            return {"ok": False, "address": addr, "error": "no TON relay accepted the launch: " + why[:150]}
        landed = await tsg._await_seqno(w, seqno, timeout_s=LAUNCH_TTL_S + 30)
        res = {"address": addr, "txid": h, "minter": minter.to_str()}
        return dict(res, ok=True) if landed else dict(res, ok=False, pending=True, error="not confirmed yet")
    finally:
        await provider.close_all()


def launch(args: dict) -> None:
    import ton_signer

    rid = str(args["request_id"])
    parsed, total = _parse(args.get("messages"))
    seed64 = ton_signer._ton_keypair_bytes(_secret(int(args["uid"])))
    prev = _log(lambda d: d.get(rid))
    if prev and time.time() - int(prev.get("at") or 0) < LAUNCH_TTL_S + 60:  # still in flight: never sign a second
        out(ok=False, pending=True, txid=prev.get("hash", ""), address=prev.get("address", ""), error="already sending")
    out(**ton_signer._run_async(_launch(seed64, rid, parsed, total)))


def check(args: dict) -> None:
    """Offline self-test: checks a message set the way launch() would, sends nothing."""
    parsed, total = _parse(args.get("messages"))
    out(ok=True, messages=len(parsed), total_ton=total / 1e9, deploys=parsed[0][3] is not None)


if __name__ == "__main__":
    cmds = {"info": info, "launch": launch, "check": check}
    if len(sys.argv) != 3 or sys.argv[1] not in cmds:
        out(ok=False, error="usage: ton_launch_exec.py info|launch|check '<json>'")
    try:
        cmds[sys.argv[1]](json.loads(sys.argv[2]))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - one JSON line, never a traceback with secrets
        out(ok=False, error=f"{type(e).__name__}: {str(e)[:160]}")
