"""TRON live swap via SunSwap V2 + TronGrid. Same secp256k1 key as EVM."""

from __future__ import annotations

import hashlib
import os
import time

import requests

from evm_signer import live_enabled, max_usd

TRONGRID = (os.getenv("TRONGRID_URL") or "https://api.trongrid.io").rstrip("/")
ROUTER = "TNJVzGqKBWkJxJB5XYSqGAwUTV15U24pPq"
WTRX = "TNUC9Qb1rRpS5CbWLmNMxXBjyFoydXjWFR"
ALPH = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = ALPH[r] + out
    pad = 0
    for b in raw:
        if b == 0:
            pad += 1
        else:
            break
    return ALPH[0] * pad + (out or ALPH[0])


def _b58decode(text: str) -> bytes:
    n = 0
    for ch in text:
        n = n * 58 + ALPH.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    pad = 0
    for ch in text:
        if ch == ALPH[0]:
            pad += 1
        else:
            break
    return b"\x00" * pad + raw


def _check(payload: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]


def evm_key_to_tron(hex_key: str) -> tuple[str, str]:
    from eth_account import Account
    from eth_hash.auto import keccak
    from eth_keys import keys

    raw = hex_key.replace("0x", "").replace("0X", "")
    acct = Account.from_key("0x" + raw)
    pub = keys.PrivateKey(bytes.fromhex(raw)).public_key.to_bytes()
    payload = b"\x41" + keccak(pub)[12:]
    addr = _b58encode(payload + _check(payload))
    return addr, acct.key.hex()


def _to_hex(addr: str) -> str:
    addr = (addr or "").strip()
    if addr.startswith("41") and len(addr) == 42:
        return addr.lower()
    if addr.startswith("0x") and len(addr) == 42:
        return "41" + addr[2:].lower()
    raw = _b58decode(addr)
    if len(raw) >= 21:
        raw = raw[:21]
    return raw.hex()


def _headers() -> dict:
    key = (os.getenv("TRONGRID_API_KEY") or "").strip()
    h = {"Content-Type": "application/json"}
    if key:
        h["TRON-PRO-API-KEY"] = key
    return h


def _post(path: str, body: dict) -> dict:
    r = requests.post(f"{TRONGRID}{path}", json=body, headers=_headers(), timeout=20)
    try:
        return r.json()
    except Exception:
        return {"error": r.text[:180]}


def _sign(raw_hex: str, key_hex: str) -> str:
    from eth_keys import keys

    raw = key_hex.replace("0x", "")
    digest = hashlib.sha256(bytes.fromhex(raw_hex)).digest()
    sig = keys.PrivateKey(bytes.fromhex(raw)).sign_msg_hash(digest)
    return (sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([sig.v])).hex()


def _broadcast(tx: dict, key_hex: str) -> tuple[bool, str]:
    raw = tx.get("raw_data_hex") or ""
    if not raw:
        return False, str(tx.get("Error") or tx.get("message") or "TronGrid built no tx")
    tx["signature"] = [_sign(raw, key_hex)]
    out = _post("/wallet/broadcasttransaction", tx)
    if out.get("result") is True or out.get("txid") or out.get("txID"):
        txid = out.get("txid") or out.get("txID") or ""
        return True, f"https://tronscan.org/#/transaction/{txid}"
    return False, str(out.get("message") or out.get("Error") or out)[:220]


def _encode_swap(amount_out_min: int, path_hex: list[str], to_hex: str, deadline: int) -> str:
    def u256(n: int) -> str:
        return f"{int(n):064x}"

    def addr(h: str) -> str:
        h = h.lower().replace("0x", "")
        if h.startswith("41") and len(h) == 42:
            h = h[2:]
        return h.zfill(64)

    # amountOutMin, path offset, to, deadline, path length + items
    # head = 4 words then dynamic path at offset 0x80
    body = u256(amount_out_min) + u256(0x80) + addr(to_hex) + u256(deadline)
    body += u256(len(path_hex))
    for p in path_hex:
        body += addr(p)
    return body


def buy_tron(token: str, usd: float, key_hex: str | None = None) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live buys OFF."
    raw = (key_hex or os.getenv("SIGNER_KEY_EVM") or "").replace("0x", "").replace("0X", "")
    if not raw:
        return False, "Need the EVM/TRON key. /wallet first."
    try:
        addr_t, _ = evm_key_to_tron(raw)
    except Exception as exc:
        return False, f"TRON key: {exc}"
    usd = min(max(1.0, float(usd)), max_usd())
    try:
        from price_fetcher import get_price_usd

        px = float(get_price_usd("tron") or 0.12)
    except Exception:
        px = 0.12
    sun = max(1_000_000, int((usd / max(px, 1e-9)) * 1_000_000))  # 6 decimals
    token_hex = _to_hex(token)
    router_hex = _to_hex(ROUTER)
    wtrx_hex = _to_hex(WTRX)
    owner_hex = _to_hex(addr_t)
    deadline = int(time.time()) + 600
    param = _encode_swap(1, [wtrx_hex, token_hex], owner_hex, deadline)
    built = _post(
        "/wallet/triggersmartcontract",
        {
            "owner_address": owner_hex,
            "contract_address": router_hex,
            "function_selector": "swapExactETHForTokens(uint256,address[],address,uint256)",
            "parameter": param,
            "fee_limit": 150_000_000,
            "call_value": sun,
            "visible": False,
        },
    )
    tx = built.get("transaction") or built
    ok, msg = _broadcast(tx, raw)
    if not ok:
        return False, "TRON SunSwap buy failed: " + msg
    return True, f"Live TRON buy ~${usd:.2f} from {addr_t}\n{msg}"


def sell_tron(token: str, key_hex: str | None = None) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live sells OFF."
    raw = (key_hex or os.getenv("SIGNER_KEY_EVM") or "").replace("0x", "").replace("0X", "")
    if not raw:
        return False, "Need the EVM/TRON key."
    addr_t, _ = evm_key_to_tron(raw)
    token_hex = _to_hex(token)
    router_hex = _to_hex(ROUTER)
    owner_hex = _to_hex(addr_t)
    # approve max
    approve_param = (_to_hex(ROUTER)[2:] if False else "")
    approve_param = _to_hex(ROUTER)
    if approve_param.startswith("41"):
        approve_param = approve_param[2:]
    approve_param = approve_param.zfill(64) + ("f" * 64)
    built = _post(
        "/wallet/triggersmartcontract",
        {
            "owner_address": owner_hex,
            "contract_address": token_hex,
            "function_selector": "approve(address,uint256)",
            "parameter": approve_param,
            "fee_limit": 100_000_000,
            "call_value": 0,
            "visible": False,
        },
    )
    ok, msg = _broadcast(built.get("transaction") or built, raw)
    if not ok:
        return False, "TRON approve failed: " + msg
    time.sleep(4)
    # sell 100% needs balance; use a large in and let router pull allowance
    # Without a balance call, sell the full allowance path via swapExactTokensForETH
    deadline = int(time.time()) + 600
    # amountIn unknown — read balanceOf
    bal_body = _post(
        "/wallet/triggerconstantcontract",
        {
            "owner_address": owner_hex,
            "contract_address": token_hex,
            "function_selector": "balanceOf(address)",
            "parameter": owner_hex[2:].zfill(64) if len(owner_hex) == 42 else owner_hex.zfill(64),
            "visible": False,
        },
    )
    const = (bal_body.get("constant_result") or ["0"])[0]
    bal = int(const, 16) if const else 0
    if bal <= 0:
        return False, f"No TRC20 balance on {addr_t} for that token."
    param = f"{bal:064x}" + f"{1:064x}" + f"{128:064x}" + _to_hex(addr_t)[2:].zfill(64)
    param += f"{deadline:064x}" + f"{2:064x}" + token_hex[2:].zfill(64) + _to_hex(WTRX)[2:].zfill(64)
    built2 = _post(
        "/wallet/triggersmartcontract",
        {
            "owner_address": owner_hex,
            "contract_address": router_hex,
            "function_selector": "swapExactTokensForETH(uint256,uint256,address[],address,uint256)",
            "parameter": param,
            "fee_limit": 150_000_000,
            "call_value": 0,
            "visible": False,
        },
    )
    ok2, msg2 = _broadcast(built2.get("transaction") or built2, raw)
    if not ok2:
        return False, f"Approved.\n{msg}\nTRON sell failed: {msg2}"
    return True, f"Approved\n{msg}\nLive TRON sell\n{msg2}"


def status_text(key_hex: str = "") -> str:
    raw = (key_hex or os.getenv("SIGNER_KEY_EVM") or "").replace("0x", "")
    if not raw:
        return "TRON: same key as EVM. /wallet first."
    try:
        addr, _ = evm_key_to_tron(raw)
    except Exception as exc:
        return f"TRON key error: {exc}"
    return f"TRON {addr}\nSunSwap V2 live. Fund TRX + energy on that address."
