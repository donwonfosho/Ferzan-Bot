"""
Launch a Solana (Meteora bonding-curve) coin from a user's Ferzan Trade Bot wallet. Called by the Launch Bot, never by users.

  python sol_launch_exec.py info   '{"uid": 123, "need_lamports": 90000000}'
  python sol_launch_exec.py launch '{"uid": 123, "request_id": "...", "tx_hex": "...", "mint": "...", "cap_lamports": 250000000, "dry": false}'

`tx_hex` is the same partly signed launch transaction the Mini App hands to a connected wallet (the Launch Bot API
built it and already signed it with the NEW coin's mint key). Here the Trade Bot's own Solana wallet adds the creator
signature (it is also the fee payer) and sends it.

Safety (same rules as the Tron / TON / EVM helpers):
  - the Trade Bot's own settings + an EXISTING wallet only; the decrypted key must match the stored address
  - the transaction must be a classic (legacy) one that needs exactly two signers: this wallet and the new coin's mint
  - the new coin's mint signature must already be on it
  - the transaction is SIMULATED first: if it would take more SOL out of the wallet than cap_lamports (the Launch Bot sets that
    to dev buy + launch fee + a margin for Solana rent and fees), nothing is signed or sent
  - one launch per request id: the signature is recorded BEFORE sending; a retry only re-reads its status
  - "dry": true does every check, the simulation and the signing, then stops without sending or recording anything
  - errors are one JSON line with no key material and no RPC urls
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import tron_launch_exec as common  # loads the Trade Bot settings exactly like the other launch helpers

out = common.out
HARD_CAP_LAMPORTS = int(float(os.getenv("SOL_LAUNCH_MAX_SOL") or 60) * 1_000_000_000)  # above the dev-buy ceiling plus fees
EXPIRE_S = 150          # a Solana transaction is only valid for about a minute; past this an unseen one can never land
SEND_WAIT_S = 60
LOG = Path(os.getenv("SOL_LAUNCH_LOG") or "/opt/ferzan/app/sol_launches.json")


def _log(fn):
    common.LOG = LOG
    return common._log_rw(fn)


def _clean(msg) -> str:
    m = re.sub(r"https?://\S+", "<url>", str(msg))
    m = re.sub(r"(?i)\burl:?\s*\S+", "url <hidden>", m)
    m = re.sub(r"(?i)host=\S+", "host=<hidden>", m)
    m = re.sub(r"\b[\w-]+(?:\.[\w-]+)+\.[a-z]{2,}\b", "<host>", m)
    return m[:160]


def _wallet(uid: int):
    """(address, Keypair) of the user's ACTIVE Trade Bot wallet."""
    import db
    import signer
    import user_wallets

    if not (os.getenv("FERZAN_MASTER_KEY") or "").strip() and not user_wallets.MASTER_PATH.exists():
        out(ok=False, error="wallet key store not found on this server")
    row = db.get_user_wallet(uid)
    if not row:
        out(ok=False, error="no_wallet")
    kp = signer.keypair_from_secret(user_wallets._unlock(row["sol_key"]))
    if str(kp.pubkey()) != str(row["sol_pub"]):
        out(ok=False, error="wallet key check failed")
    return str(row["sol_pub"]), kp


def _rpc(method: str, params: list, timeout: int = 25) -> dict:
    import signer

    r = signer._rpc_post(json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout)
    body = r.json() if r.content else {}
    return body if isinstance(body, dict) else {}


def _status(sig: str) -> tuple[str, str]:
    """("confirmed" | "failed" | "pending" | "unknown", reason). unknown = the network ANSWERED that it has not seen it.
    A query that itself fails raises, so callers can tell "not seen" from "couldn't ask"."""
    body = _rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
    st = (((body.get("result") or {}).get("value")) or [None])[0]
    if not st:
        return "unknown", ""
    if st.get("err"):
        return "failed", str(st.get("err"))[:120]
    if st.get("confirmationStatus") in ("confirmed", "finalized"):
        return "confirmed", ""
    return "pending", ""


def _blockhash_dead(bh: str) -> bool:
    """True only when the network explicitly says this blockhash can no longer be used (so an unseen tx can never land)."""
    if not bh:
        return False
    try:
        body = _rpc("isBlockhashValid", [bh, {"commitment": "processed"}])
    except Exception:  # noqa: BLE001
        return False
    val = (body.get("result") or {}).get("value")
    return val is False


def _wait(sig: str, wait_s: int) -> tuple[str, str]:
    deadline = time.time() + wait_s
    while True:
        try:
            s, why = _status(sig)
        except Exception:  # noqa: BLE001 - a busy node is not a failed launch, and not proof it never landed
            s, why = "error", ""
        if s in ("confirmed", "failed") or time.time() >= deadline:
            return s, why
        time.sleep(2)


def _verdict(state: str, why: str, sig: str, mint: str, addr: str, **extra) -> dict:
    if state == "confirmed":
        return {"ok": True, "signature": sig, "mint": mint, "address": addr, **extra}
    if state == "failed":
        return {"ok": False, "signature": sig, "address": addr, "error": f"the launch failed on Solana ({why})", **extra}
    return {"ok": False, "pending": True, "signature": sig, "mint": mint, "address": addr, "error": "not confirmed yet", **extra}


def info(args: dict) -> None:
    import signer

    addr, _kp = _wallet(int(args["uid"]))
    bal, need = signer.sol_balance_lamports(addr), int(args.get("need_lamports") or 0)
    out(ok=True, address=addr, balance=bal / 1e9, need=need / 1e9, enough=bal >= need)


def _decode(tx_hex: str, addr: str, mint: str):
    """The transaction object, after the structure checks, or exits with the reason."""
    from solders.signature import Signature
    from solders.transaction import Transaction

    try:
        tx = Transaction.from_bytes(bytes.fromhex(tx_hex))
    except Exception:  # noqa: BLE001
        out(ok=False, error="launch transaction is not a classic Solana transaction")
    msg = tx.message
    need = int(msg.header.num_required_signatures)
    signers = [str(k) for k in list(msg.account_keys)[:need]]
    if need != 2 or signers[0] != addr or set(signers) != {addr, mint}:
        out(ok=False, error="launch transaction needs signers other than this wallet and the new coin")
    if tx.signatures[signers.index(mint)] == Signature.default():
        out(ok=False, error="launch transaction is missing the new coin's signature")
    return tx


def _outflow(tx, addr: str) -> tuple[int, str]:
    """(lamports this transaction would take out of the wallet, "" ) from a simulation; exits if it would fail."""
    pre = _rpc("getBalance", [addr, {"commitment": "processed"}])
    pre_l = ((pre.get("result") or {}).get("value"))
    if pre_l is None:
        out(ok=False, error="couldn't read the wallet balance")
    sim = _rpc("simulateTransaction", [base64.b64encode(bytes(tx)).decode(), {
        "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True, "commitment": "processed",
        "accounts": {"encoding": "base64", "addresses": [addr]}}], timeout=40)
    val = (sim.get("result") or {}).get("value") or {}
    if val.get("err"):
        logs = " ".join(str(x) for x in (val.get("logs") or [])[-3:])
        low = (str(val.get("err")) + logs).lower()
        if "insufficient" in low:
            out(ok=False, error="low_balance", address=addr, balance=int(pre_l) / 1e9)
        out(ok=False, error="the launch would fail on Solana: " + _clean(logs or val.get("err")), address=addr)
    accts = val.get("accounts") or []
    if not accts or not isinstance(accts[0], dict) or accts[0].get("lamports") is None:
        out(ok=False, error="couldn't simulate the launch, so nothing was sent", address=addr)
    return int(pre_l) - int(accts[0]["lamports"]), ""


def launch(args: dict) -> None:
    uid, rid, mint = int(args["uid"]), str(args["request_id"]), str(args["mint"])
    cap = int(args.get("cap_lamports") or 0)
    if not (0 < cap <= HARD_CAP_LAMPORTS):
        out(ok=False, error="launch spending limit is missing or above the safety cap")
    addr, kp = _wallet(uid)

    prev = _log(lambda d: d.get(rid))
    if prev and prev.get("signature"):  # already tried once: report that, never send a second launch
        sig = prev["signature"]
        if prev.get("expired"):  # already proven dead earlier: report it again, never send under this request id
            out(ok=False, expired=True, signature=sig, address=addr, error="the launch expired before Solana took it (nothing happened)")
        state, why = _wait(sig, 15)
        age = time.time() - int(prev.get("at") or 0)
        # expired only when Solana ANSWERED "never seen it" AND said the blockhash is dead; a failed query proves nothing
        if state == "unknown" and age > EXPIRE_S and _blockhash_dead(prev.get("blockhash") or ""):
            _log(lambda d: d[rid].update(expired=True))
            out(ok=False, expired=True, signature=sig, address=addr, error="the launch expired before Solana took it (nothing happened)")
        out(**_verdict("pending" if state in ("unknown", "error") else state, why, sig, prev.get("mint") or mint, addr, repeat=True))

    tx = _decode(str(args.get("tx_hex") or ""), addr, mint)
    tx.partial_sign([kp], tx.message.recent_blockhash)
    from solders.signature import Signature as _Sig
    if any(sg == _Sig.default() for sg in tx.signatures):  # both signatures (this wallet + the new coin) must now be present
        out(ok=False, error="couldn't sign the launch transaction, so nothing was sent", address=addr)
    spend, _ = _outflow(tx, addr)
    if spend > cap:
        out(ok=False, error=f"the launch would spend {spend / 1e9:.4f} SOL, more than the {cap / 1e9:.4f} SOL it should, so nothing was sent", address=addr)
    sig = str(tx.signatures[0])
    if args.get("dry"):
        out(ok=True, dry=True, address=addr, mint=mint, would_spend=spend / 1e9, cap=cap / 1e9, signature=sig)

    _log(lambda d: d.__setitem__(rid, {"signature": sig, "mint": mint, "uid": uid, "address": addr, "at": int(time.time()), "broadcast": False,
                                       "blockhash": str(tx.message.recent_blockhash)}))
    wire = base64.b64encode(bytes(tx)).decode()
    try:
        body = _rpc("sendTransaction", [wire, {"encoding": "base64", "skipPreflight": False, "maxRetries": 5}], timeout=30)
    except Exception as e:  # noqa: BLE001 - the node may have taken it before the reply was lost
        state, why = _wait(sig, 20)
        if state in ("confirmed", "failed", "pending"):
            _log(lambda d: d[rid].update(broadcast=True))
            out(**_verdict(state, why, sig, mint, addr))
        out(ok=False, maybe_sent=True, signature=sig, address=addr, error="not sure it was sent: " + _clean(f"{type(e).__name__}: {e}"))
    err = body.get("error")
    if err:
        msg = _clean(err.get("message") if isinstance(err, dict) else err)
        state, why = _wait(sig, 8)
        if state in ("confirmed", "failed", "pending"):  # it landed anyway
            _log(lambda d: d[rid].update(broadcast=True))
            out(**_verdict(state, why, sig, mint, addr))
        code = err.get("code") if isinstance(err, dict) else None
        low = msg.lower()
        if code in (-32002, -32003) or "blockhash not found" in low or "signature verification" in low:
            _log(lambda d: d.pop(rid, None))  # refused before it could enter a block: a retry may send again
            out(ok=False, error="Solana rejected the launch: " + msg, address=addr)
        # any other error (internal error, node behind, ...) may have come after the node took it: keep the record
        out(ok=False, maybe_sent=True, signature=sig, address=addr, error="not sure it was sent: " + msg)
    _log(lambda d: d[rid].update(broadcast=True))
    state, why = _wait(sig, SEND_WAIT_S)
    out(**_verdict("pending" if state == "unknown" else state, why, sig, mint, addr))


if __name__ == "__main__":
    cmds = {"info": info, "launch": launch}
    if len(sys.argv) != 3 or sys.argv[1] not in cmds:
        out(ok=False, error="usage: sol_launch_exec.py info|launch '<json>'")
    try:
        cmds[sys.argv[1]](json.loads(sys.argv[2]))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 - always answer with one JSON line, never a traceback with secrets
        out(ok=False, error=f"{type(e).__name__}: {_clean(e)}")
