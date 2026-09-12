"""EVM hot wallet via 0x allowance-holder. This droplet only."""

from __future__ import annotations

import os

import requests

from chains import CHAINS, resolve_chain

NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
ZEROX = "https://api.0x.org/swap/allowance-holder/quote"
SUPPORTED = {"eth", "base", "bsc", "arb", "avax"}


def _as_int(val, default: int = 0) -> int:
    if val is None or val == "":
        return default
    if isinstance(val, int):
        return val
    s = str(val).strip()
    if s.startswith("0x"):
        return int(s, 16)
    return int(s)


def _addr(val) -> str:
    s = str(val or "").strip().lower()
    if s.startswith("0x"):
        s = s[2:]
    s = "".join(ch for ch in s if ch in "0123456789abcdef")
    s = (s.lstrip("0") or "0").zfill(40)
    out = "0x" + s
    try:
        from eth_utils import to_checksum_address

        return to_checksum_address(out)
    except Exception:
        return out


def native_balance(chain: str, address: str) -> tuple[float, str]:
    cid = resolve_chain(chain) or chain
    meta = CHAINS.get(cid) or {}
    symbol = meta.get("native") or "?"
    rpc = meta.get("rpc")
    if not rpc:
        return 0.0, symbol
    try:
        body = _rpc(rpc, "eth_getBalance", [address, "latest"])
        raw = body.get("result") or "0x0"
        wei = int(raw, 16) if str(raw).startswith("0x") else int(raw)
    except Exception:
        return 0.0, symbol
    return wei / 10**18, symbol


def live_enabled() -> bool:
    return os.getenv("LIVE_BUYS", "").strip().lower() in {"1", "true", "yes", "on"}


def configured() -> bool:
    return bool((os.getenv("SIGNER_KEY_EVM") or "").strip() and (os.getenv("ZEROX_API_KEY") or "").strip())


def max_usd() -> float:
    try:
        return max(1.0, min(50.0, float(os.getenv("SIGNER_MAX_USD", "10"))))
    except ValueError:
        return 10.0


def _key_hex() -> str:
    raw = (os.getenv("SIGNER_KEY_EVM") or "").strip()
    if raw.startswith("0x"):
        raw = raw[2:]
    return raw


def public_evm() -> str:
    from eth_account import Account

    return Account.from_key("0x" + _key_hex()).address


def status_text() -> str:
    if not (os.getenv("SIGNER_KEY_EVM") or "").strip():
        return "EVM signer empty. Add SIGNER_KEY_EVM on the droplet."
    if not (os.getenv("ZEROX_API_KEY") or "").strip():
        return "EVM key loaded. Add ZEROX_API_KEY from 0x.org."
    try:
        addr = public_evm()
    except Exception as exc:
        return f"EVM key present but invalid.\n{exc}"
    flag = "ON" if live_enabled() else "OFF"
    return (
        f"EVM signer {addr}\n"
        f"Live: {flag} · max ${max_usd():.0f}\n"
        "Chains: ETH, Base, BSC, Arb, Avax."
    )


def _native_decimals(chain: str) -> int:
    return 18


def buy_evm(chain: str, buy_token: str, usd: float, key_hex: str | None = None) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live buys OFF. LIVE_BUYS=1"
    if not key_hex and not configured():
        return False, "Set SIGNER_KEY_EVM and ZEROX_API_KEY."
    cid = resolve_chain(chain)
    if cid not in SUPPORTED:
        return False, f"Live EVM is {', '.join(sorted(SUPPORTED))}. Not {chain}."
    token = (buy_token or "").strip()
    if not token.startswith("0x") or len(token) != 42:
        return False, "Need a 0x contract."
    usd = min(max(1.0, float(usd)), max_usd())
    try:
        from eth_account import Account
        from price_fetcher import get_price_usd
    except Exception as exc:
        return False, f"EVM deps missing: {exc}"

    meta = CHAINS[cid]
    try:
        px = float(get_price_usd("ethereum") if cid != "bsc" else get_price_usd("binancecoin") or 300)
    except Exception:
        px = 300.0 if cid != "bsc" else 600.0
    if cid == "bsc":
        try:
            px = float(get_price_usd("binancecoin") or 600)
        except Exception:
            px = 600.0
    wei = max(10**12, int((usd / max(px, 1e-9)) * 10 ** _native_decimals(cid)))
    raw = (key_hex or _key_hex()).replace("0x", "").replace("0X", "")
    acct = Account.from_key("0x" + raw)
    headers = {
        "0x-api-key": os.getenv("ZEROX_API_KEY", "").strip(),
        "0x-version": "v2",
        "Accept": "application/json",
    }
    try:
        qr = requests.get(
            ZEROX,
            headers=headers,
            params={
                "chainId": str(meta["chain_id"]),
                "sellToken": NATIVE,
                "buyToken": token,
                "sellAmount": str(wei),
                "taker": acct.address,
                "txOrigin": acct.address,
                "slippageBps": "150",
            },
            timeout=20,
        )
        quote = qr.json() if qr.content else {}
    except requests.RequestException as exc:
        return False, f"0x quote failed: {exc}"
    if qr.status_code >= 400:
        return False, str(quote.get("reason") or quote.get("message") or qr.text[:180])
    tx = (quote.get("transaction") or quote.get("tx") or {})
    if not tx.get("to") or not tx.get("data"):
        return False, str(quote.get("message") or "0x returned no transaction")
    raw_tx = {
        "to": _addr(tx["to"]),
        "data": tx["data"] if str(tx["data"]).startswith("0x") else "0x" + str(tx["data"]),
        "value": _as_int(tx.get("value"), wei),
        "chainId": int(meta["chain_id"]),
        "gas": _as_int(tx.get("gas") or tx.get("gasLimit"), 400000),
        "gasPrice": _as_int(tx.get("gasPrice"), 2_000_000_000),
        "nonce": _nonce(meta["rpc"], acct.address),
    }
    try:
        signed = acct.sign_transaction(raw_tx)
        raw_hex = "0x" + signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        rr = requests.post(
            meta["rpc"],
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction", "params": [raw_hex]},
            timeout=20,
        )
        body = rr.json() if rr.content else {}
    except Exception as exc:
        return False, f"EVM broadcast failed: {exc} | to={raw_tx.get('to')}"
    if body.get("error"):
        err = body["error"]
        return False, str(err.get("message") if isinstance(err, dict) else err)
    txh = body.get("result") or ""
    if not txh:
        return False, "RPC accepted nothing."
    exp = meta.get("explorer") or "https://etherscan.io"
    return True, f"Live {cid.upper()} buy ~${usd:.2f}\n{exp}/tx/{txh}"


def _nonce(rpc: str, addr: str) -> int:
    r = requests.post(
        rpc,
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionCount", "params": [addr, "pending"]},
        timeout=15,
    )
    data = r.json() if r.content else {}
    val = data.get("result") or "0x0"
    return int(val, 16)


def _rpc(rpc: str, method: str, params: list):
    r = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    return r.json() if r.content else {}


def _gas_price(rpc: str) -> int:
    body = _rpc(rpc, "eth_gasPrice", [])
    val = body.get("result") or "0x77359400"
    return int(val, 16)


def _erc20_balance(rpc: str, token: str, owner: str) -> int:
    data = "0x70a08231" + owner[2:].lower().zfill(64)
    body = _rpc(rpc, "eth_call", [{"to": token, "data": data}, "latest"])
    val = body.get("result") or "0x0"
    return int(val, 16)


def _estimate_gas(rpc: str, frm: str, to: str, data: str, value: int) -> int:
    body = _rpc(
        rpc,
        "eth_estimateGas",
        [{"from": frm, "to": _addr(to), "data": data, "value": hex(int(value))}],
    )
    if body.get("result"):
        try:
            return max(21000, int(int(body["result"], 16) * 1.3))
        except Exception:
            pass
    return 550000


def _broadcast(acct, meta: dict, to: str, data: str, value: int = 0) -> tuple[bool, str]:
    data_hex = data if str(data).startswith("0x") else "0x" + str(data)
    raw_tx = {
        "to": _addr(to),
        "data": data_hex,
        "value": int(value),
        "chainId": int(meta["chain_id"]),
        "gas": _estimate_gas(meta["rpc"], acct.address, to, data_hex, int(value)),
        "gasPrice": int(_gas_price(meta["rpc"]) * 1.2),
        "nonce": _nonce(meta["rpc"], acct.address),
    }
    signed = acct.sign_transaction(raw_tx)
    raw_hex = signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex
    body = _rpc(meta["rpc"], "eth_sendRawTransaction", [raw_hex])
    if body.get("error"):
        err = body["error"]
        return False, str(err.get("message") if isinstance(err, dict) else err)
    txh = body.get("result") or ""
    if not txh:
        return False, "RPC accepted nothing."
    cid = int(meta.get("chain_id") or 1)
    if cid == 8453:
        exp = "https://basescan.org"
    elif cid == 56:
        exp = "https://bscscan.com"
    else:
        exp = meta.get("explorer") or "https://etherscan.io"
    return True, f"{exp}/tx/{txh}"


def sell_evm(chain: str, sell_token: str) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live sells OFF. LIVE_BUYS=1"
    if not configured():
        return False, "Set SIGNER_KEY_EVM and ZEROX_API_KEY."
    cid = resolve_chain(chain)
    if cid not in SUPPORTED:
        return False, f"Live EVM is {', '.join(sorted(SUPPORTED))}. Not {chain}."
    token = _addr(sell_token)
    try:
        from eth_account import Account
    except Exception as exc:
        return False, f"EVM deps missing: {exc}"
    meta = CHAINS[cid]
    acct = Account.from_key("0x" + _key_hex())
    rpcs = [meta["rpc"]]
    if cid == "base":
        rpcs += ["https://base.publicnode.com", "https://base.llamarpc.com"]
    bal = 0
    for rpc in rpcs:
        try:
            bal = _erc20_balance(rpc, token, acct.address)
        except Exception:
            bal = 0
        if bal > 0:
            meta = dict(meta)
            meta["rpc"] = rpc
            break
    if bal <= 0:
        return False, (
            f"No token balance on {cid} for {token}\n"
            f"Wallet {acct.address}\n"
            f"https://basescan.org/token/{token}?a={acct.address}"
        )
    headers = {
        "0x-api-key": os.getenv("ZEROX_API_KEY", "").strip(),
        "0x-version": "v2",
        "Accept": "application/json",
    }
    try:
        qr = requests.get(
            ZEROX,
            headers=headers,
            params={
                "chainId": str(meta["chain_id"]),
                "sellToken": token,
                "buyToken": NATIVE,
                "sellAmount": str(bal),
                "taker": acct.address,
                "txOrigin": acct.address,
                "slippageBps": "300",
            },
            timeout=20,
        )
        quote = qr.json() if qr.content else {}
    except requests.RequestException as exc:
        return False, f"0x quote failed: {exc}"
    if qr.status_code >= 400:
        return False, str(quote.get("reason") or quote.get("message") or qr.text[:180])
    issues = quote.get("issues") or {}
    allow = issues.get("allowance") if isinstance(issues, dict) else None
    if allow and allow.get("spender"):
        spender = _addr(allow["spender"])
        approve_data = "0x095ea7b3" + spender[2:].lower().zfill(64) + ("f" * 64)
        ok, msg = _broadcast(acct, meta, token, approve_data, 0)
        if not ok:
            return False, f"Approve failed: {msg}"
        import time

        time.sleep(15)
        approve_note = f"Approved {spender}\n{msg}\n"
        try:
            qr = requests.get(
                ZEROX,
                headers=headers,
                params={
                    "chainId": str(meta["chain_id"]),
                    "sellToken": token,
                    "buyToken": NATIVE,
                    "sellAmount": str(bal),
                    "taker": acct.address,
                    "txOrigin": acct.address,
                    "slippageBps": "300",
                },
                timeout=20,
            )
            quote = qr.json() if qr.content else {}
        except requests.RequestException as exc:
            return False, approve_note + f"0x requote failed: {exc}"
    else:
        approve_note = ""
    tx = quote.get("transaction") or quote.get("tx") or {}
    if not tx.get("to") or not tx.get("data"):
        return False, approve_note + str(quote.get("message") or "0x returned no sell tx")
    ok, msg = _broadcast(acct, meta, tx["to"], tx["data"], _as_int(tx.get("value"), 0))
    if not ok:
        return False, approve_note + f"Sell failed: {msg}"
    return True, approve_note + f"Live {cid.upper()} sell (full bag)\n{msg}"
