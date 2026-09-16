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


DLN = "https://dln.debridge.finance/v1.0/dln/order/create-tx"
DLN_CHAIN = {"sol": 7565164, "eth": 1, "base": 8453, "bsc": 56, "arb": 42161, "pol": 137, "avax": 43114, "op": 10}
DLN_TOKEN = {
    "sol": SOL_NATIVE,
    "eth": NATIVE_EVM,
    "base": NATIVE_EVM,
    "bsc": NATIVE_EVM,
    "arb": NATIVE_EVM,
    "pol": NATIVE_EVM,
    "avax": NATIVE_EVM,
    "op": NATIVE_EVM,
}


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
    if "sol" in {src, dst}:
        return _quote_dln(user, recv, src, dst, amt)
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
    return {"raw": data, "user": user, "recv": recv, "src": src, "dst": dst, "amt": amt, "via": "relay"}


def _quote_dln(user: str, recv: str, src: str, dst: str, amt: str) -> dict:
    if src not in DLN_CHAIN or dst not in DLN_CHAIN:
        raise ValueError("That pair is not on deBridge yet.")
    r = requests.get(
        DLN,
        params={
            "srcChainId": DLN_CHAIN[src],
            "srcChainTokenIn": DLN_TOKEN[src],
            "srcChainTokenInAmount": _amount_raw(src, amt),
            "dstChainId": DLN_CHAIN[dst],
            "dstChainTokenOut": DLN_TOKEN[dst],
            "dstChainTokenOutAmount": "auto",
            "dstChainTokenOutRecipient": recv,
            "srcChainOrderAuthorityAddress": user,
            "dstChainOrderAuthorityAddress": recv,
        },
        timeout=25,
    )
    data = r.json() if r.content else {}
    if r.status_code >= 400 or data.get("error"):
        raise RuntimeError(str(data.get("error") or data.get("message") or r.text[:240]))
    if not ((data.get("tx") or {}).get("data") or (data.get("tx") or {}).get("to")):
        raise RuntimeError(str(data.get("errorMessage") or "deBridge returned no tx."))
    return {"raw": data, "user": user, "recv": recv, "src": src, "dst": dst, "amt": amt, "via": "dln"}


def summarize(pack: dict) -> str:
    data = pack["raw"]
    if pack.get("via") == "dln":
        est = data.get("estimation") or {}
        inn = ((est.get("srcChainTokenIn") or {}).get("amount") or pack["amt"])
        outn = (est.get("dstChainTokenOut") or {}).get("amount") or "?"
        try:
            if str(inn).isdigit() and pack["src"] == "sol":
                inn = f"{int(inn) / 1e9:.4f}"
            if str(outn).isdigit():
                outn = f"{int(outn) / (10 ** CHAINS[pack['dst']]['dec']):.6f}"
        except (TypeError, ValueError):
            pass
        return (
            f"From {CHAINS[pack['src']]['name']}   {inn} {CHAINS[pack['src']]['unit']}\n"
            f"To {CHAINS[pack['dst']]['name']}   {outn} {CHAINS[pack['dst']]['unit']}\n"
            f"Send {pack['user'][:12]}…\n"
            f"Receive {pack['recv'][:12]}…\n"
            "via deBridge · signed on this desk"
        )
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


def widget_links(pack: dict) -> tuple[str, str]:
    jumper_id = {
        "sol": "1151111081099710",
        "eth": "1",
        "base": "8453",
        "bsc": "56",
        "arb": "42161",
    }
    src, dst = pack["src"], pack["dst"]
    jumper = (
        "https://jumper.exchange/"
        f"?fromChain={jumper_id.get(src, '1')}"
        f"&toChain={jumper_id.get(dst, '8453')}"
        f"&fromAmount={pack.get('amt') or ''}"
    )
    relay = (
        "https://relay.link/bridge"
        f"?fromChainId={CHAINS[src]['id']}"
        f"&toChainId={CHAINS[dst]['id']}"
    )
    return jumper, relay


def execute(uid: int, pack: dict) -> str:
    src = pack["src"]
    data = pack["raw"]
    if pack.get("via") == "dln" and CHAINS[src]["kind"] == "sol":
        return _exec_dln_sol(uid, pack, data)
    if CHAINS[src]["kind"] == "evm":
        return _exec_evm(uid, pack, data)
    if pack.get("via") == "dln":
        return _exec_evm(uid, pack, {"steps": [{"items": [{"data": data.get("tx") or {}}]}]})
    return _exec_sol(uid, pack, data)


def _exec_dln_sol(uid: int, pack: dict, data: dict) -> str:
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
    blob = ((data.get("tx") or {}).get("data") or "")
    if not blob:
        raise RuntimeError("deBridge sent no Solana tx.")
    if blob.startswith("0x"):
        raw = bytes.fromhex(blob[2:])
    else:
        try:
            raw = bytes.fromhex(blob)
        except ValueError:
            raw = base64.b64decode(blob)
    tx = VersionedTransaction.from_bytes(raw)
    rpc = sol_signer._rpc()
    bh = requests.post(
        rpc,
        json={"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "confirmed"}]},
        timeout=12,
    ).json()
    blockhash = ((bh.get("result") or {}).get("value") or {}).get("blockhash")
    if not blockhash:
        raise RuntimeError("No Solana blockhash from RPC.")
    from solders.hash import Hash
    from solders.message import MessageV0

    fresh = Hash.from_string(blockhash)
    msg = tx.message
    try:
        rebuilt = MessageV0(
            header=msg.header,
            account_keys=msg.account_keys,
            recent_blockhash=fresh,
            instructions=msg.instructions,
            address_table_lookups=getattr(msg, "address_table_lookups", []),
        )
    except Exception:
        rebuilt = msg
    signed = VersionedTransaction(rebuilt, [kp])
    body = requests.post(
        rpc,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(bytes(signed)).decode(),
                {"encoding": "base64", "skipPreflight": False},
            ],
        },
        timeout=20,
    ).json()
    if body.get("error"):
        err = body["error"]
        if isinstance(err, dict):
            raise RuntimeError(err.get("message") or str(err))
        raise RuntimeError(str(err))
    sig = body.get("result") or ""
    if not sig:
        raise RuntimeError("Solana RPC accepted nothing.")
    return f"Bridge submitted.\nhttps://solscan.io/tx/{sig}\nWatch https://app.debridge.finance/orders\nCredit on destination usually 1–3 min."


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


def _ix_bytes(raw) -> bytes:
    import base64
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, list):
        return bytes(int(x) & 255 for x in raw)
    if not isinstance(raw, str):
        return b""
    if raw.startswith("0x"):
        return bytes.fromhex(raw[2:])
    try:
        return base64.b64decode(raw)
    except Exception:
        return bytes.fromhex(raw)


def _relay_ix(row: dict):
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    pid = row.get("programId") or row.get("program_id")
    accs = row.get("keys") or row.get("accounts") or []
    metas = []
    for acc in accs:
        if isinstance(acc, str):
            metas.append(AccountMeta(Pubkey.from_string(acc), False, True))
            continue
        pk = acc.get("pubkey") or acc.get("pubKey") or acc.get("address")
        metas.append(
            AccountMeta(
                Pubkey.from_string(pk),
                bool(acc.get("isSigner") or acc.get("is_signer")),
                bool(acc.get("isWritable") or acc.get("is_writable")),
            )
        )
    return Instruction(Pubkey.from_string(pid), _ix_bytes(row.get("data")), metas)


def _fetch_alts(rpc: str, addrs: list):
    from solders.address_lookup_table_account import AddressLookupTableAccount
    from solders.pubkey import Pubkey

    if not addrs:
        return []
    body = requests.post(
        rpc,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getMultipleAccounts",
            "params": [list(addrs), {"encoding": "jsonParsed"}],
        },
        timeout=10,
    ).json()
    values = (body.get("result") or {}).get("value") or []
    out = []
    for addr, val in zip(addrs, values):
        if not val:
            continue
        info = ((val.get("data") or {}).get("parsed") or {}).get("info") or {}
        names = info.get("addresses") or []
        if names:
            out.append(
                AddressLookupTableAccount(
                    Pubkey.from_string(addr),
                    [Pubkey.from_string(x) for x in names],
                )
            )
    return out


def _exec_sol_instructions(kp, payload: dict, rpc: str) -> str:
    import base64
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction

    raw_ix = payload.get("instructions") or []
    alts = _fetch_alts(rpc, payload.get("addressLookupTableAddresses") or [])
    ixs = [_relay_ix(x) for x in raw_ix if isinstance(x, dict)]
    if not ixs:
        raise RuntimeError("Relay instructions were empty.")
    bh = requests.post(
        rpc,
        json={"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]},
        timeout=15,
    ).json()
    blockhash = ((bh.get("result") or {}).get("value") or {}).get("blockhash")
    if not blockhash:
        raise RuntimeError("No Solana blockhash.")
    msg = MessageV0.try_compile(kp.pubkey(), ixs, alts, Hash.from_string(blockhash))
    signed = VersionedTransaction(msg, [kp])
    body = requests.post(
        rpc,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "sendTransaction",
            "params": [
                base64.b64encode(bytes(signed)).decode(),
                {"encoding": "base64", "skipPreflight": False},
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

    rpc = sol_signer._rpc()
    for step in data.get("steps") or []:
        for item in step.get("items") or []:
            d = item.get("data") or {}
            if isinstance(d, dict) and d.get("instructions"):
                return _exec_sol_instructions(kp, d, rpc)

    blob = None
    deposit_to = ""
    deposit_amt = 0
    for step in data.get("steps") or []:
        for item in step.get("items") or []:
            d = item.get("data")
            if isinstance(d, str) and len(d) > 80 and not d.startswith("0x"):
                blob = d
            elif isinstance(d, dict):
                for key in ("transaction", "tx", "serializedTransaction", "serializedTx", "data"):
                    val = d.get(key)
                    if isinstance(val, str) and len(val) > 80:
                        blob = val
                dest = str(d.get("to") or d.get("depositAddress") or "")
                if dest and not dest.startswith("0x") and 32 <= len(dest) <= 48:
                    deposit_to = dest
                    raw_amt = d.get("value") or d.get("amount") or d.get("lamports") or 0
                    try:
                        deposit_amt = int(str(raw_amt), 0)
                    except (TypeError, ValueError):
                        deposit_amt = 0

    if blob:
        try:
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
                        {"encoding": "base64", "skipPreflight": False},
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
        except Exception as exc:
            if not deposit_to:
                raise RuntimeError(
                    f"{exc}\nSOL→EVM from Relay needs a compiled deposit. Use Base → ETH for now."
                ) from exc

    if deposit_to:
        if deposit_amt <= 0:
            deposit_amt = int(float(pack["amt"]) * 1_000_000_000)
        ok, msg = sol_signer.send_sol(deposit_to, sol_key, deposit_amt)
        if not ok:
            raise RuntimeError(msg)
        return f"Bridge deposit sent.\n{msg}\nDestination credit can take 30–90s."

    kinds = [str(s.get("kind") or s.get("id") or "") for s in (data.get("steps") or [])]
    preview = ""
    try:
        item = ((data.get("steps") or [{}])[0].get("items") or [{}])[0]
        d = item.get("data")
        if isinstance(d, dict):
            preview = "data keys: " + ",".join(list(d.keys())[:24])
        else:
            preview = f"data type={type(d).__name__}"
    except Exception:
        preview = "no item.data"
    raise RuntimeError(
        "Relay Solana step is not a raw tx we can sign yet.\n"
        f"Steps: {kinds or 'none'}. {preview}\n"
        "Use Base → ETH until that payload is wired."
    )
