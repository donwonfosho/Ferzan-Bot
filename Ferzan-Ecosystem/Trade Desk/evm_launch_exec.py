"""
Launch an EVM coin (Base, BNB Chain, Ethereum, Robinhood Chain) from a user's Ferzan Trade Bot wallet.
Called by the Launch Bot, never by users.

  python evm_launch_exec.py info   '{"uid": 123, "chain": "base"}'
  python evm_launch_exec.py launch '{"uid": 123, "chain": "base", "request_id": "...", "factory": "0x..", "tx": {...}}'

`tx` is the same unsigned factory call the website / Mini App hands to a connected wallet (built by the Launch Bot API).
Here the Trade Bot's own EVM wallet (the key the Trade Bot trades with) signs and sends it.

Safety (same rules as tron_launch_exec.py / ton_launch_exec.py):
  - the Trade Bot's own settings + an EXISTING wallet only; the decrypted key must match the stored address
  - only the chains in CHAIN_MAP; the tx must be for that chain id, sent FROM this wallet, TO the Ferzan factory it was
    built for, with calldata, and its value can never exceed MAX_NATIVE (EVM_LAUNCH_MAX_NATIVE, default 3 coins)
  - if the tx carries launch_fee_wei + dev_buy_wei, the value must equal exactly their sum
  - the balance must cover value + gas before anything is signed
  - the factory must be one of the Ferzan factories the Launch Bot passes from its own settings (not just whatever the
    build response says), and gas cost is capped
  - one launch per request id: every tx hash AND the nonce are recorded BEFORE broadcasting; a retry re-checks all of them
    and never takes a new nonce while an earlier try might still be in flight. A reply that does not clearly say "rejected"
    is treated as "may have been sent", never as "nothing was sent"
  - the result is read back from the chain (receipt status), never assumed
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import tron_launch_exec as common  # loads the Trade Bot settings exactly like the other launch helpers

out = common.out
CHAIN_MAP = {"base": "base", "bsc": "bsc", "ethereum": "eth", "robinhood": "hood"}  # Launch Bot name -> Trade Bot chain id
MAX_NATIVE_WEI = int(float(os.getenv("EVM_LAUNCH_MAX_NATIVE") or 3) * 10**18)
GAS_PRICE_BUMP = 1.2           # cushion over the current gas price so the launch is not left waiting
MAX_GAS_COST_WEI = int(float(os.getenv("EVM_LAUNCH_MAX_GAS_NATIVE") or 0.5) * 10**18)
RETRY_EXPIRE_S = 120           # a send that never reached the network may be retried after this long
RECEIPT_WAIT_S = 90
LOG = Path(os.getenv("EVM_LAUNCH_LOG") or "/opt/ferzan/app/evm_launches.json")


def _log(fn):
    common.LOG = LOG
    return common._log_rw(fn)


def _wallet(uid: int):
    """(address, key hex, account) of the user's ACTIVE Trade Bot wallet."""
    import db
    import user_wallets

    if not (os.getenv("FERZAN_MASTER_KEY") or "").strip() and not user_wallets.MASTER_PATH.exists():
        out(ok=False, error="wallet key store not found on this server")
    row = db.get_user_wallet(uid)
    if not row:
        out(ok=False, error="no_wallet")
    key = user_wallets._unlock(row["evm_key"])
    from eth_account import Account

    acct = Account.from_key("0x" + key.replace("0x", ""))
    if acct.address.lower() != str(row["evm_pub"]).lower():
        out(ok=False, error="wallet key check failed")
    return acct.address, key, acct


def _meta(chain: str):
    import evm_signer

    cid = CHAIN_MAP.get(str(chain).lower())
    if not cid:
        out(ok=False, error="unsupported_chain")
    meta = evm_signer.CHAINS.get(cid) or {}
    if not meta.get("rpc") or not meta.get("chain_id"):
        out(ok=False, error="unsupported_chain")
    return cid, meta, evm_signer


def _balance_wei(es, meta, addr: str) -> int:
    body = es._rpc(meta["rpc"], "eth_getBalance", [addr, "latest"])
    raw = body.get("result") if isinstance(body, dict) else None
    if raw in (None, ""):
        raise RuntimeError("balance read failed")  # never a guessed 0
    return int(raw, 16) if str(raw).startswith("0x") else int(raw)


def _receipt(es, meta, txhash: str) -> dict:
    body = es._rpc(meta["rpc"], "eth_getTransactionReceipt", [txhash])
    return (body.get("result") if isinstance(body, dict) else None) or {}


def _wait_receipt(es, meta, txhash: str, wait_s: int) -> dict:
    deadline = time.time() + wait_s
    while True:
        try:
            r = _receipt(es, meta, txhash)
        except Exception:  # noqa: BLE001 - a busy node is not a failed launch
            r = {}
        if r or time.time() >= deadline:
            return r
        time.sleep(3)


def _wait_any(es, meta, hashes: list, wait_s: int):
    """(hash, receipt) of the first of our tries that mined, or ("", {}). Same nonce => at most one can."""
    deadline = time.time() + wait_s
    while True:
        for h in hashes:
            try:
                r = _receipt(es, meta, h)
            except Exception:  # noqa: BLE001
                r = {}
            if r:
                return h, r
        if time.time() >= deadline:
            return "", {}
        time.sleep(3)


def _verdict(r: dict, txhash: str, addr: str, **extra) -> dict:
    if not r:
        return {"ok": False, "pending": True, "txhash": txhash, "address": addr, "error": "not confirmed yet", **extra}
    if int(r.get("status", "0x0"), 16) != 1:
        return {"ok": False, "txhash": txhash, "address": addr, "error": "the launch reverted on-chain (nothing was created)", **extra}
    return {"ok": True, "txhash": txhash, "address": addr, **extra}


def _check_tx(tx: dict, meta: dict, addr: str, factory: str, allowed=None) -> tuple[int, int, str]:
    """(value, gas, data) after every safety check, or exits with the reason."""
    try:
        value, gas = int(tx.get("value") or 0), int(tx["gas"])
        chain_id = int(tx["chainId"])
    except (KeyError, TypeError, ValueError):
        out(ok=False, error="bad launch transaction")
    data = str(tx.get("data") or "")
    if chain_id != int(meta["chain_id"]):
        out(ok=False, error="launch transaction is for another chain")
    pinned = {str(a).lower() for a in (allowed or [])}
    if not pinned or str(factory).lower() not in pinned:
        out(ok=False, error="launch factory is not one of the Ferzan factories")
    if not (str(factory).startswith("0x") and len(str(factory)) == 42) or str(tx.get("to") or "").lower() != str(factory).lower():
        out(ok=False, error="launch transaction is not for the Ferzan factory")
    if str(tx.get("from") or "").lower() != addr.lower():
        out(ok=False, error="launch transaction was built for another wallet")
    if not data.startswith("0x") or len(data) < 10:
        out(ok=False, error="launch transaction has no call data")
    if "launch_fee_wei" in tx or "dev_buy_wei" in tx:
        if value != int(tx.get("launch_fee_wei") or 0) + int(tx.get("dev_buy_wei") or 0):
            out(ok=False, error="launch value does not match fee + dev buy")
    if value < 0 or value > MAX_NATIVE_WEI:
        out(ok=False, error="launch value is above the safety cap")
    if not (21000 <= gas <= 15_000_000):
        out(ok=False, error="launch gas limit looks wrong")
    return value, gas, data


def info(args: dict) -> None:
    cid, meta, es = _meta(args["chain"])
    addr, _key, _acct = _wallet(int(args["uid"]))
    bal = _balance_wei(es, meta, addr)
    need = int(args.get("need_wei") or 0)
    out(ok=True, address=addr, balance=bal / 1e18, need=need / 1e18, enough=bal >= need, symbol=meta.get("native") or "ETH",
        explorer=meta.get("explorer") or "")


_CLEAR_REJECT = ("insufficient funds", "insufficient balance", "invalid sender", "intrinsic gas", "exceeds block gas limit",
                 "gas limit reached", "invalid opcode", "invalid chain id", "unsupported")
_MAYBE_SENT = ("already known", "known transaction", "already imported", "nonce too low", "replacement transaction",
               "already in mempool", "tx already exists")


def _clean(msg: str) -> str:
    import re

    m = re.sub(r"https?://\S+", "<url>", str(msg))
    m = re.sub(r"(?i)\burl:?\s*\S+", "url <hidden>", m)  # connection errors print "url: /v2/<key>"
    return m[:160]  # a paid RPC url may carry a key


def launch(args: dict) -> None:
    cid, meta, es = _meta(args["chain"])
    uid, rid = int(args["uid"]), str(args["request_id"])
    addr, _key, acct = _wallet(uid)
    tx = args.get("tx") or {}
    value, gas, data = _check_tx(tx, meta, addr, str(args.get("factory") or ""), args.get("allowed_factories"))

    prev = _log(lambda d: d.get(rid))
    nonce = None
    if prev and (prev.get("hashes") or prev.get("txhash")):  # already tried once: report that, never start a second launch
        hashes = list(prev.get("hashes") or [prev["txhash"]])
        h, r = _wait_any(es, meta, hashes, 15)
        if r:
            out(**_verdict(r, h, addr, repeat=True))
        if prev.get("nonce") is None or prev.get("broadcast") or time.time() - int(prev.get("at") or 0) <= RETRY_EXPIRE_S:
            out(**_verdict({}, hashes[-1], addr, repeat=True))
        # never reached the network and has expired: retry, but ONLY on the same nonce (so two launches can never both mine)
        if es._nonce(meta["rpc"], addr) > int(prev["nonce"]):
            out(**_verdict({}, hashes[-1], addr, repeat=True))  # the nonce moved: something of ours may be in flight
        nonce = int(prev["nonce"])

    price = int(max(int(tx.get("gasPrice") or 0), es._gas_price(meta["rpc"])) * GAS_PRICE_BUMP)
    if gas * price * 2 > MAX_GAS_COST_WEI:
        out(ok=False, error="gas cost is above the safety cap", address=addr)
    bal = _balance_wei(es, meta, addr)
    need = value + gas * price
    if bal < need:
        out(ok=False, error="low_balance", address=addr, balance=bal / 1e18, need=need / 1e18, symbol=meta.get("native") or "ETH")

    if nonce is None:
        nonce = es._nonce_guarded(meta, addr)
    hashes: list = list((prev or {}).get("hashes") or [])
    txhash, last_err, maybe_sent = "", "the network accepted nothing", False
    for bump in (1.0, 1.4, 2.0):  # only repriced when the network says the fee was too low
        raw_tx = {"to": es._addr(tx["to"]), "data": data, "value": value, "chainId": int(meta["chain_id"]),
                  "gas": gas, "gasPrice": int(price * bump), "nonce": nonce}
        signed = acct.sign_transaction(raw_tx)
        raw_hex = signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
        raw_hex = raw_hex if raw_hex.startswith("0x") else "0x" + raw_hex
        h = signed.hash.hex()
        h = h if h.startswith("0x") else "0x" + h
        hashes.append(h)
        _log(lambda d, hs=list(hashes): d.__setitem__(rid, {"txhash": hs[-1], "hashes": hs, "nonce": nonce, "uid": uid,
                                                           "address": addr, "chain": cid, "at": int(time.time()),
                                                           "broadcast": False}))
        try:
            body = es._send_raw(meta, raw_hex, addr, nonce)
        except Exception as e:  # noqa: BLE001 - the reply may have been lost after the node took it
            last_err, maybe_sent = _clean(f"{type(e).__name__}: {e}"), True
            break
        err = body.get("error") if isinstance(body, dict) else None
        if err:
            last_err = _clean(err.get("message") if isinstance(err, dict) else err)
            low = last_err.lower()
            if any(k in low for k in _MAYBE_SENT):
                maybe_sent = True
                break
            if bump < 2.0 and any(k in low for k in es._UNDERPRICED_HINTS):
                continue
            if not any(k in low for k in _CLEAR_REJECT):
                maybe_sent = True  # unknown answer: do not claim "nothing was sent"
            break
        if not (isinstance(body, dict) and body.get("result")):
            maybe_sent = True
            break
        txhash = str(body["result"])
        break
    if not txhash:
        if maybe_sent:  # keep the record: a retry will look at every hash and the nonce before doing anything
            h, r = _wait_any(es, meta, hashes, 20)
            if r:
                out(**_verdict(r, h, addr))
            out(ok=False, maybe_sent=True, txhash=hashes[-1], address=addr, error="not sure it was sent: " + last_err)
        _log(lambda d: d.pop(rid, None))  # clearly rejected before the network took it: a retry may send again
        out(ok=False, error="broadcast failed: " + last_err, address=addr)
    _log(lambda d: d[rid].update(txhash=txhash, broadcast=True))
    h, r = _wait_any(es, meta, hashes, RECEIPT_WAIT_S)
    out(**_verdict(r, h or txhash, addr))


if __name__ == "__main__":
    cmds = {"info": info, "launch": launch}
    if len(sys.argv) != 3 or sys.argv[1] not in cmds:
        out(ok=False, error="usage: evm_launch_exec.py info|launch '<json>'")
    try:
        cmds[sys.argv[1]](json.loads(sys.argv[2]))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - always answer with one JSON line, never a traceback with secrets
        out(ok=False, error=f"{type(e).__name__}: {_clean(e)}")
