"""In-bot Relay quotes signed with the user's Ferzan desk wallet."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

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
    "hood": {"name": "Robinhood Chain", "id": 4663, "unit": "ETH", "kind": "evm", "dec": 18, "currency": NATIVE_EVM},
    "arc": {"name": "Arc", "id": 0, "unit": "USDC", "kind": "evm", "dec": 18, "currency": NATIVE_EVM},
    "trx": {"name": "Tron", "id": 0, "unit": "TRX", "kind": "tron", "dec": 6, "currency": NATIVE_EVM},
}


# Checked live on the droplet: neither Relay nor deBridge lists these, so they stay out of the picker.
_NO_ROUTE = {"pulse", "stable"}


def _extend_from_desk() -> None:
    """Every EVM chain the desk trades gets a bridge entry (id from the desk table; 0 = resolved from its RPC)."""
    try:
        from chains import CHAINS as DESK
    except Exception:
        return
    for k, m in DESK.items():
        if k in CHAINS or m.get("kind") != "evm" or k in _NO_ROUTE:
            continue
        CHAINS[k] = {"name": m.get("label") or k.upper(), "id": int(m.get("chain_id") or 0),
                     "unit": m.get("native") or "ETH", "kind": "evm", "dec": 18, "currency": NATIVE_EVM}


_extend_from_desk()
MAX_NATIVE = {"TRX": 20000.0, "USDC": 5000.0}  # per-bridge size cap in native units (default 25)


def chain_id(key: str) -> int:
    """EVM chain id. Arc's comes from the desk's chain table (set from its RPC)."""
    if int(CHAINS[key]["id"] or 0):
        return int(CHAINS[key]["id"])
    from chains import CHAINS as DESK

    return int((DESK.get(key) or {}).get("chain_id") or 0)


def _amount_raw(key: str, amt: str) -> str:
    meta = CHAINS[key]
    val = float(amt)
    cap = MAX_NATIVE.get(meta["unit"], 25.0)
    if val <= 0 or val > cap:
        raise ValueError(f"Size must be between 0 and {cap:g} {meta['unit']}.")
    return str(int(val * (10 ** meta["dec"])))


DLN = "https://dln.debridge.finance/v1.0/dln/order/create-tx"

# ---- Ferzan bridge fee (same as the website): a small cut of what is bridged, paid to the treasury by the provider.
# FERZAN holders pay less (tiers live in the Launch Bot's ferzan_perks; the Trade Bot only asks its local API).
_FEE_EVM = "0x4d5955afb9ABF5943729CB74A0196498483e4622"  # public treasury addresses, same as the website
_FEE_SOL = "6yxsKcSeqAcoLXgyKDtVVW7Hb2d4uLYVT8X9zGa64HRp"
_TIER_CACHE: dict = {}


def fee_bps(uid: int) -> int:
    """Bridge fee in basis points for this user (0 = none). Never raises: any problem means the normal fee."""
    base = int(os.getenv("BRIDGE_FEE_BPS", "25") or 25)
    base = max(0, min(100, base))
    if base == 0:
        return 0
    try:
        import feecollect

        if feecollect.exempt(int(uid)):  # team members listed in FEE_EXEMPT_USER_IDS pay no bridge fee
            return 0
    except Exception:  # noqa: BLE001
        pass
    try:
        sol = wallet_addr(uid, "sol")
        if not sol:
            return base
        import time as _t
        hit = _TIER_CACHE.get(sol)
        if hit and _t.time() - hit[0] < 300:
            return hit[1]
        api = os.getenv("LAUNCH_API_LOCAL", "http://127.0.0.1:8000").rstrip("/")
        r = requests.get(f"{api}/api/ferzan-perks/{sol}", timeout=2.5).json()
        bps = int(r.get("bridge_fee_bps")) if r.get("active") else base
        bps = max(0, min(base, bps))
        _TIER_CACHE[sol] = (_t.time(), bps)
        return bps
    except Exception:
        return base


def _fee_wallet(kind: str) -> str:
    if kind == "sol":
        return (os.getenv("BRIDGE_FEE_WALLET_SOL") or _FEE_SOL).strip()
    return (os.getenv("BRIDGE_FEE_WALLET_EVM") or _FEE_EVM).strip()
DLN_CHAIN = {"sol": 7565164, "eth": 1, "base": 8453, "bsc": 56, "arb": 42161, "pol": 137, "avax": 43114, "op": 10,
             "hood": 4663, "arc": None, "trx": 100000026}


def dln_chain(key: str):
    """deBridge's id for a desk chain. DLN_ID_<KEY> in the env overrides (for ids that differ from the EVM one)."""
    raw = (os.getenv(f"DLN_ID_{key.upper()}") or "").strip()
    if raw.isdigit():
        return int(raw)
    if key in DLN_CHAIN and DLN_CHAIN[key]:
        return DLN_CHAIN[key]
    if key in CHAINS and CHAINS[key]["kind"] == "evm":
        cid = chain_id(key)
        return _dln_live().get(cid) if cid else None
    return None


_DLN_LIVE: dict = {"t": 0.0, "v": {}}


def _dln_live() -> dict:
    """{original EVM chain id: deBridge chain id} from deBridge's own list, cached 1h. {} if unreachable."""
    import time as _t

    if _DLN_LIVE["v"] and _t.time() - _DLN_LIVE["t"] < 3600:
        return _DLN_LIVE["v"]
    try:
        r = requests.get("https://dln.debridge.finance/v1.0/supported-chains-info", timeout=8).json()
        out = {}
        for c in r.get("chains") or []:
            o, i = c.get("originalChainId"), c.get("chainId")
            if o and i:
                out[int(o)] = int(i)
        if out:
            _DLN_LIVE.update(t=_t.time(), v=out)
        return out
    except Exception:
        return _DLN_LIVE["v"]
DLN_TOKEN = {
    "sol": SOL_NATIVE,
    "eth": NATIVE_EVM,
    "base": NATIVE_EVM,
    "bsc": NATIVE_EVM,
    "arb": NATIVE_EVM,
    "pol": NATIVE_EVM,
    "avax": NATIVE_EVM,
    "op": NATIVE_EVM,
    "hood": NATIVE_EVM,
    "arc": NATIVE_EVM,
    "trx": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",  # deBridge's id for native TRX (base58, not the 0x zero address)
}


def dln_token(key: str) -> str:
    return (os.getenv(f"DLN_NATIVE_{key.upper()}") or "").strip() or DLN_TOKEN.get(key) or NATIVE_EVM


def wallet_addr(uid: int, key: str) -> str:
    """The desk address for a chain: SOL wallet, EVM wallet, or the Tron address of the EVM key."""
    import user_wallets

    w = user_wallets.ensure(uid)
    kind = CHAINS[key]["kind"]
    if kind == "sol":
        return w["sol_pub"]
    if kind == "tron":
        import tron_signer

        _sol, evm = user_wallets.secrets(uid)
        return tron_signer.evm_key_to_tron(evm.replace("0x", "").replace("0X", ""))[0]
    return w["evm_pub"]


def quote(uid: int, src: str, dst: str, amt: str) -> dict:
    import user_wallets

    if src not in CHAINS or dst not in CHAINS:
        raise ValueError("Pick two listed chains.")
    if src == dst:
        raise ValueError("From and to must be different.")
    if CHAINS[src]["kind"] == "tron":
        raise ValueError("Bridging out of Tron isn't enabled yet. Bridge INTO Tron, or send TRX manually.")
    user_wallets_ok = user_wallets_ensure(uid)
    user, recv = wallet_addr(uid, src), wallet_addr(uid, dst)
    if not user_wallets_ok or not user or not recv:
        raise ValueError("Open /wallet first so Ferzan can create your desk addresses.")
    bps = fee_bps(uid)
    if "sol" in {src, dst} or "trx" in {src, dst}:
        return _quote_dln(user, recv, src, dst, amt, bps)
    try:
        return _quote_relay(user, recv, src, dst, amt, bps)
    except Exception as relay_err:
        # Relay doesn't list every chain (Robinhood, Arc): deBridge is the fallback.
        try:
            return _quote_dln(user, recv, src, dst, amt, bps)
        except Exception as dln_err:
            raise RuntimeError(f"No route: Relay said {str(relay_err)[:90]}; deBridge said {str(dln_err)[:90]}")


def user_wallets_ensure(uid: int) -> bool:
    import user_wallets

    try:
        return bool(user_wallets.ensure(uid))
    except Exception:
        return False


def _quote_relay(user: str, recv: str, src: str, dst: str, amt: str, bps: int = 0) -> dict:
    if not chain_id(src) or not chain_id(dst):
        raise ValueError("chain id unknown")
    body = {
        "user": user,
        "recipient": recv,
        "originChainId": chain_id(src),
        "destinationChainId": chain_id(dst),
        "originCurrency": CHAINS[src]["currency"],
        "destinationCurrency": CHAINS[dst]["currency"],
        "amount": _amount_raw(src, amt),
        "tradeType": "EXACT_INPUT",
        "includeProtocolData": True,
    }
    if bps > 0:
        body["appFees"] = [{"recipient": _fee_wallet("evm"), "fee": str(bps)}]
    headers = {"content-type": "application/json"}
    key = (os.getenv("RELAY_API_KEY") or "").strip()
    if key:
        headers["x-api-key"] = key
    r = requests.post(RELAY, headers=headers, json=body, timeout=25)
    data = r.json() if r.content else {}
    if r.status_code >= 400 and bps > 0:
        # If the provider rejects the fee request, still offer the bridge, without our fee, and say so in the log.
        import logging
        logging.getLogger("bridge").warning("relay rejected appFees (%s): %s", r.status_code, r.text[:160])
        body.pop("appFees", None)
        bps = 0
        r = requests.post(RELAY, headers=headers, json=body, timeout=25)
        data = r.json() if r.content else {}
    if r.status_code >= 400:
        msg = data.get("message") or data.get("error") or r.text[:240]
        raise RuntimeError(str(msg))
    return {"raw": data, "user": user, "recv": recv, "src": src, "dst": dst, "amt": amt, "via": "relay", "fee_bps": bps}


def _quote_dln(user: str, recv: str, src: str, dst: str, amt: str, bps: int = 0) -> dict:
    sid, did = dln_chain(src), dln_chain(dst)
    if not sid or not did:
        raise ValueError("That pair is not on deBridge yet.")
    params = {
            "srcChainId": sid,
            "srcChainTokenIn": dln_token(src),
            "srcChainTokenInAmount": _amount_raw(src, amt),
            "dstChainId": did,
            "dstChainTokenOut": dln_token(dst),
            "dstChainTokenOutAmount": "auto",
            "dstChainTokenOutRecipient": recv,
            "srcChainOrderAuthorityAddress": user,
            "dstChainOrderAuthorityAddress": recv,
    }
    if bps > 0:
        params["affiliateFeePercent"] = f"{bps / 100:g}"
        params["affiliateFeeRecipient"] = _fee_wallet("sol" if CHAINS[src]["kind"] == "sol" else "evm")
    r = requests.get(DLN, params=params, timeout=25)
    data = r.json() if r.content else {}
    if (r.status_code >= 400 or data.get("error")) and bps > 0:
        import logging
        logging.getLogger("bridge").warning("deBridge rejected the affiliate fee (%s): %s", r.status_code, r.text[:160])
        params.pop("affiliateFeePercent", None)
        params.pop("affiliateFeeRecipient", None)
        bps = 0
        r = requests.get(DLN, params=params, timeout=25)
        data = r.json() if r.content else {}
    if r.status_code >= 400 or data.get("error"):
        raise RuntimeError(str(data.get("error") or data.get("message") or r.text[:240]))
    if not ((data.get("tx") or {}).get("data") or (data.get("tx") or {}).get("to")):
        raise RuntimeError(str(data.get("errorMessage") or "deBridge returned no tx."))
    return {"raw": data, "user": user, "recv": recv, "src": src, "dst": dst, "amt": amt, "via": "dln", "fee_bps": bps}


def _fee_line(pack: dict) -> str:
    bps = int(pack.get("fee_bps") or 0)
    return f"Ferzan fee {bps / 100:g}% (included; FERZAN holders pay less)\n" if bps > 0 else ""


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
            f"{_fee_line(pack)}"
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
        f"{_fee_line(pack)}"
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
    if CHAINS[src]["kind"] == "tron":
        raise RuntimeError("Bridging out of Tron isn't enabled yet.")
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
    rpc = sol_signer._rpc()
    root = Path(__file__).resolve().parent
    helper = root / "sol_bridge_send.js"
    if not helper.exists():
        helper = root / "sol_bridge_send.mjs"
    if not helper.exists():
        raise RuntimeError("sol_bridge_send.js is missing next to bridge.py.")
    env = os.environ.copy()
    env["FERZAN_SOL_KEY"] = sol_key
    env["NODE_PATH"] = str(helper.parent / "node_modules")
    proc = subprocess.run(
        ["node", str(helper), rpc, base64.b64encode(raw).decode()],
        capture_output=True,
        text=True,
        timeout=25,
        env=env,
        cwd=str(helper.parent),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "node sender failed").strip()[:400])
    sig = (proc.stdout or "").strip().split()[-1]
    if not sig:
        raise RuntimeError("Node sender returned no signature.")
    order_id = (data.get("orderId") or (data.get("order") or {}).get("orderId") or "").strip()
    if not order_id:
        order_id = _dln_order_from_sig(sig)
    dln = f"https://app.debridge.finance/order?orderId={order_id}" if order_id else "https://app.debridge.finance/orders"
    return {
        "text": (
            "Bridge submitted on Solana.\n"
            f"Solscan: https://solscan.io/tx/{sig}\n"
            f"deBridge order: {dln}\n"
            "Watching destination credit…"
        ),
        "sig": sig,
        "order_id": order_id,
        "dst": pack.get("dst") or "",
    }


def _dln_order_from_sig(sig: str) -> str:
    for url in (
        f"https://stats-api.dln.trade/api/Transaction/{sig}/orderIds",
        f"https://dln-api.debridge.finance/api/Transaction/{sig}/orderIds",
    ):
        try:
            body = requests.get(url, timeout=12).json() or {}
            ids = body.get("orderIds") or []
            if ids and isinstance(ids[0], dict):
                return str(ids[0].get("stringValue") or "")
            if ids:
                return str(ids[0])
        except Exception:
            continue
    return ""


def dln_status(order_id: str) -> dict:
    if not order_id:
        return {}
    try:
        st = requests.get(
            f"https://dln.debridge.finance/v1.0/dln/order/{order_id}/status",
            timeout=12,
        ).json() or {}
    except Exception:
        st = {}
    try:
        full = requests.get(
            f"https://stats-api.dln.trade/api/Orders/{order_id}",
            timeout=12,
        ).json() or {}
    except Exception:
        full = {}
    return {
        "status": st.get("status") or full.get("orderState") or "",
        "dest_tx": full.get("fulfillTransactionHash") or full.get("fulfilledTx") or "",
        "order_id": order_id,
    }


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
    desk_key = key if key in DESK else "eth"
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


def est_out(pack: dict) -> float:
    """What the route says the destination will receive, in destination native units (0.0 if unknown)."""
    data = pack.get("raw") or {}
    dec = CHAINS[pack["dst"]]["dec"]
    try:
        if pack.get("via") == "dln":
            raw = ((data.get("estimation") or {}).get("dstChainTokenOut") or {}).get("amount")
            return int(raw) / (10 ** dec) if raw is not None else 0.0
        cout = (data.get("details") or {}).get("currencyOut") or {}
        if cout.get("amountFormatted") is not None:
            return float(cout["amountFormatted"])
        return int(cout.get("amount") or 0) / (10 ** dec)
    except (TypeError, ValueError):
        return 0.0
