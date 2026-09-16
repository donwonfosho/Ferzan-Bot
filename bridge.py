"""In-bot Relay quotes signed with the user's Ferzan desk wallet."""

from __future__ import annotations

import json
import os

import requests

RELAY = "https://api.relay.link/quote/v2"
NATIVE_EVM = "0x0000000000000000000000000000000000000000"
SOL_NATIVE = "11111111111111111111111111111111"

CHAINS = {
    "sol": {"name": "Solana", "id": 792703809, "unit": "SOL", "kind": "sol", "dec": 9, "currency": "So11111111111111111111111111111111111111112"},
    "eth": {"name": "Ethereum", "id": 1, "unit": "ETH", "kind": "evm", "dec": 18, "currency": NATIVE_EVM},
    "base": {"name": "Base", "id": 8453, "unit": "ETH", "kind": "evm", "dec": 18, "currency": NATIVE_EVM},
    "bsc": {"name": "BNB", "id": 56, "unit": "BNB", "kind": "evm", "dec": 18, "currency": NATIVE_EVM},
    "arb": {"name": "Arbitrum", "id": 42161, "unit": "ETH", "kind": "evm", "dec": 18, "currency": NATIVE_EVM},
    "pol": {"name": "Polygon", "id": 137, "unit": "POL", "kind": "evm", "dec": 18, "currency": NATIVE_EVM},
    "avax": {"name": "Avalanche", "id": 43114, "unit": "AVAX", "kind": "evm", "dec": 18, "currency": NATIVE_EVM},
    "op": {"name": "Optimism", "id": 10, "unit": "ETH", "kind": "evm", "dec": 18, "currency": NATIVE_EVM},
}


def _amount_raw(key: str, amt: str) -> str:
    meta = CHAINS[key]
    val = float(amt)
    if val <= 0 or val > 25:
        raise ValueError("Size must be between 0 and 25 native.")
    return str(int(val * (10 ** meta["dec"])))


def quote(uid: int, src: str, dst: str, amt: str) -> dict:
    import user_wallets

    if src not in CHAINS or dst not in CHAINS:
        raise ValueError("Pick two listed chains.")
    if src == dst:
        raise ValueError("From and to must be different.")
    w = user_wallets.ensure(uid)
    user = w["sol_pub"] if CHAINS[src]["kind"] == "sol" else w["evm_pub"]
    recv = w["sol_pub"] if CHAINS[dst]["kind"] == "sol" else w["evm_pub"]
    if not user or not recv:
        raise ValueError("Open /wallet first so Ferzan can create your desk addresses.")
    body = {
        "user": user,
        "recipient": recv,
        "originChainId": CHAINS[src]["id"],
        "destinationChainId": CHAINS[dst]["id"],
        "originCurrency": CHAINS[src]["currency"],
        "destinationCurrency": CHAINS[dst]["currency"],
        "amount": _amount_raw(src, amt),
        "tradeType": "EXACT_INPUT",
        "includeProtocolData": True,
    }
    headers = {"content-type": "application/json"}
    key = (os.getenv("RELAY_API_KEY") or "").strip()
    if key:
        headers["x-api-key"] = key
    r = requests.post(RELAY, headers=headers, json=body, timeout=25)
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        msg = data.get("message") or data.get("error") or r.text[:240]
        raise RuntimeError(str(msg))
    return {"raw": data, "user": user, "recv": recv, "src": src, "dst": dst, "amt": amt}


def summarize(pack: dict) -> str:
    data = pack["raw"]
    details = data.get("details") or {}
    cin = details.get("currencyIn") or {}
    cout = details.get("currencyOut") or {}
    impact = details.get("totalImpact") or {}
    if isinstance(impact, dict):
        pct = impact.get("percent")
        fee = f"Impact {pct}%" if pct is not None else ""
    else:
        fee = str(impact or "")
    inn = cin.get("amountFormatted") or pack["amt"]
    outn = cout.get("amountFormatted") or "?"
    try:
        outn = f"{float(outn):.6f}"
    except (TypeError, ValueError):
        pass
    return (
        f"From {CHAINS[pack['src']]['name']}   {inn} {CHAINS[pack['src']]['unit']}\n"
        f"To {CHAINS[pack['dst']]['name']}   {outn} {CHAINS[pack['dst']]['unit']}\n"
        f"Send {pack['user'][:12]}…\n"
        f"Receive {pack['recv'][:12]}…\n"
        f"{fee}"
    )


def _evm_items(data: dict) -> list[dict]:
    items = []
    for step in data.get("steps") or []:
        for item in step.get("items") or []:
            d = item.get("data") or {}
            if isinstance(d, dict) and d.get("to"):
                items.append(d)
    return items


def execute(uid: int, pack: dict) -> str:
    import user_wallets

    src = pack["src"]
    data = pack["raw"]
    if CHAINS[src]["kind"] == "evm":
        return _exec_evm(uid, pack, data)
    return _exec_sol(uid, pack, data)


def _exec_evm(uid: int, pack: dict, data: dict) -> str:
    import evm_signer
    import user_wallets
    from eth_account import Account
    from chains import CHAINS as DESK

    _sol, evm_key = user_wallets.secrets(uid)
    items = _evm_items(data)
    if not items:
        raise RuntimeError("Relay sent no EVM tx to sign. Try another pair or size.")
    key = pack["src"]
    desk_key = {"eth": "eth", "base": "base", "bsc": "bsc", "arb": "arb", "pol": "pol", "avax": "avax", "op": "op"}.get(key, "eth")
    meta = DESK.get(desk_key) or DESK["eth"]
    raw = evm_key.replace("0x", "").replace("0X", "")
    acct = Account.from_key("0x" + raw)
    links = []
    for i, item in enumerate(items):
        ok, msg = evm_signer._broadcast(
            acct,
            meta,
            item.get("to"),
            item.get("data") or "0x",
            int(str(item.get("value") or "0"), 0) if str(item.get("value") or "0").startswith("0x") else int(item.get("value") or 0),
        )
        if not ok:
            raise RuntimeError(msg if i == 0 else f"Step {i + 1} failed: {msg}")
        links.append(msg)
    return "Bridge submitted.\n" + "\n".join(links) + "\nDestination credit can take 30–90s."


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _exec_sol(uid: int, pack: dict, data: dict) -> str:
    import base64
    import signer as sol_signer
    import user_wallets
    from solders.keypair import Keypair
    from solders.transaction import VersionedTransaction

    sol_key, _evm = user_wallets.secrets(uid)
    try:
        kp = Keypair.from_base58_string(sol_key)
    except Exception:
        kp = Keypair.from_bytes(base64.b64decode(sol_key))

    blob = None
    deposit_to = ""
    deposit_amt = 0
    for node in _walk(data):
        for key in ("transaction", "tx", "serializedTransaction", "serializedTx"):
            val = node.get(key)
            if isinstance(val, str) and len(val) > 80:
                blob = val
        raw = node.get("data")
        if isinstance(raw, str) and len(raw) > 80 and not raw.startswith("0x"):
            blob = raw
        dest = str(node.get("to") or node.get("depositAddress") or "")
        if dest and not dest.startswith("0x") and 32 <= len(dest) <= 48:
            deposit_to = dest
            raw_amt = node.get("value") or node.get("amount") or node.get("lamports") or 0
            try:
                deposit_amt = int(str(raw_amt), 0)
            except (TypeError, ValueError):
                deposit_amt = 0

    if blob:
        try:
            raw = base64.b64decode(blob)
        except Exception:
            raw = bytes.fromhex(blob)
        tx = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(tx.message, [kp])
        rpc = sol_signer._rpc()
        body = requests.post(
            rpc,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendTransaction",
                "params": [
                    base64.b64encode(bytes(signed)).decode(),
                    {"encoding": "base64", "skipPreflight": True},
                ],
            },
            timeout=25,
        ).json()
        if body.get("error"):
            raise RuntimeError(str(body["error"]))
        sig = body.get("result") or ""
        if not sig:
            raise RuntimeError("Solana RPC accepted nothing.")
        return f"Bridge submitted.\nhttps://solscan.io/tx/{sig}\nDestination credit can take 30–90s."

    if deposit_to:
        if deposit_amt <= 0:
            deposit_amt = int(float(pack["amt"]) * 1_000_000_000)
        ok, msg = sol_signer.send_sol(deposit_to, sol_key, deposit_amt)
        if not ok:
            raise RuntimeError(msg)
        return f"Bridge deposit sent.\n{msg}\nDestination credit can take 30–90s."

    kinds = [str(s.get("kind") or s.get("id") or "") for s in (data.get("steps") or [])]
    raise RuntimeError(
        "Relay did not return a signable Solana tx. "
        f"Steps: {kinds or 'none'}. Try Base → ETH first."
    )
