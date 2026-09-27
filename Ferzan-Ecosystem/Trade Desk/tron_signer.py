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


def _w(h: str) -> str:
    """One 32-byte ABI word for a Tron address given as 41.. hex."""
    h = h.lower().replace("0x", "")
    return (h[2:] if h.startswith("41") and len(h) == 42 else h).zfill(64)


def _const(contract_hex: str, owner_hex: str, sig: str, param: str) -> list[int]:
    out = _post("/wallet/triggerconstantcontract", {"owner_address": owner_hex, "contract_address": contract_hex,
                                                    "function_selector": sig, "parameter": param, "visible": False})
    res = (out.get("constant_result") or [""])[0] or ""
    if not res or (out.get("result") or {}).get("result") is False:
        return []
    return [int(res[i:i + 64], 16) for i in range(0, len(res), 64)]


def _quote_out(amount_in: int, path_hex: list[str], owner_hex: str) -> int:
    """SunSwap V2 getAmountsOut: what the pool would give right now (0 = no pool / no liquidity)."""
    param = f"{int(amount_in):064x}" + f"{64:064x}" + f"{len(path_hex):064x}" + "".join(_w(p) for p in path_hex)
    words = _const(_to_hex(ROUTER), owner_hex, "getAmountsOut(uint256,address[])", param)
    return words[-1] if len(words) >= 2 + len(path_hex) else 0


def _trx_balance(owner_hex: str) -> int:
    return int(_post("/wallet/getaccount", {"address": owner_hex}).get("balance") or 0)


def _send_and_wait(built: dict, key_hex: str, timeout_s: int = 60) -> tuple[str, str, float]:
    """(result, link, TRX burned). result: SUCCESS / REVERT / OUT_OF_ENERGY / ... / 'unconfirmed' / 'not sent: why'."""
    tx = built.get("transaction") or built
    txid = tx.get("txID") or ""
    ok, msg = _broadcast(tx, key_hex)
    if not ok or not txid:
        return "not sent: " + msg, "", 0.0
    link = f"https://tronscan.org/#/transaction/{txid}"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(3)
        info = _post("/wallet/gettransactioninfobyid", {"value": txid})
        if info.get("id"):
            return (info.get("receipt") or {}).get("result") or "SUCCESS", link, (info.get("fee") or 0) / 1e6
    return "unconfirmed", link, 0.0


ENERGY_SPARE_SUN = 15_000_000  # ~15 TRX kept for the swap's energy (a SunSwap trade burns about 7-13 TRX)


def buy_tron(token: str, usd: float, key_hex: str | None = None, slip_bps: int = 1000) -> tuple[bool, str]:
    """SunSwap V2 buy with a real minimum-out (quote minus your slippage), a balance check first,
    and a result read back from the chain."""
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

        px = float(get_price_usd("tron") or 0)
    except Exception:
        px = 0.0
    if px <= 0:
        return False, "TRX price feed is down, buy skipped. Nothing sent."  # never guess: a wrong price over-spends
    sun = max(1_000_000, int((usd / px) * 1_000_000))
    token_hex, wtrx_hex, owner_hex = _to_hex(token), _to_hex(WTRX), _to_hex(addr_t)
    bal = _trx_balance(owner_hex)
    if bal < sun + ENERGY_SPARE_SUN:
        return False, (f"Not enough TRX on {addr_t}: {bal / 1e6:,.2f} TRX, this buy needs about "
                       f"{(sun + ENERGY_SPARE_SUN) / 1e6:,.2f} (incl. ~15 TRX for energy). Nothing sent.")
    quoted = _quote_out(sun, [wtrx_hex, token_hex], owner_hex)
    if quoted <= 0:
        return False, "No SunSwap V2 pool with liquidity for this token yet. Nothing sent."
    slip = max(10, min(5000, int(slip_bps)))
    min_out = max(1, quoted * (10_000 - slip) // 10_000)
    param = _encode_swap(min_out, [wtrx_hex, token_hex], owner_hex, int(time.time()) + 300)
    built = _post("/wallet/triggersmartcontract", {
        "owner_address": owner_hex, "contract_address": _to_hex(ROUTER),
        "function_selector": "swapExactETHForTokens(uint256,address[],address,uint256)",
        "parameter": param, "fee_limit": 150_000_000, "call_value": sun, "visible": False})
    result, link, burned = _send_and_wait(built, raw)
    if result.startswith("not sent"):
        return False, "TRON SunSwap buy failed: " + result[10:] + ". Nothing sent."
    if result == "SUCCESS":
        return True, f"Live TRON buy ~${usd:.2f} ({sun / 1e6:,.2f} TRX, max slippage {slip / 100:g}%) from {addr_t}\n{link}"
    if result == "unconfirmed":
        return False, f"TRON buy sent but not confirmed within a minute. Check before retrying:\n{link}"
    return False, (f"TRON buy didn't fill ({result}): the price moved more than your {slip / 100:g}% slippage. "
                   f"Your TRX stayed in the wallet; about {burned:.2f} TRX went to network energy.\n{link}")


def sell_tron(token: str, key_hex: str | None = None, slip_bps: int = 1000) -> tuple[bool, str]:
    """Sells the whole balance on SunSwap V2: approves only when needed (and waits for it), then swaps with
    a real minimum-out and reads the result back from the chain."""
    if not live_enabled():
        return False, "Live sells OFF."
    raw = (key_hex or os.getenv("SIGNER_KEY_EVM") or "").replace("0x", "").replace("0X", "")
    if not raw:
        return False, "Need the EVM/TRON key."
    addr_t, _ = evm_key_to_tron(raw)
    token_hex, router_hex, owner_hex, wtrx_hex = _to_hex(token), _to_hex(ROUTER), _to_hex(addr_t), _to_hex(WTRX)
    bal_w = _const(token_hex, owner_hex, "balanceOf(address)", _w(owner_hex))
    bal = bal_w[0] if bal_w else 0
    if bal <= 0:
        return False, f"No TRC20 balance on {addr_t} for that token."
    quoted = _quote_out(bal, [token_hex, wtrx_hex], owner_hex)
    if quoted <= 0:
        return False, "No SunSwap V2 pool with liquidity for this token. Nothing sent."
    if _trx_balance(owner_hex) < 2 * ENERGY_SPARE_SUN:
        return False, f"Need about {2 * ENERGY_SPARE_SUN / 1e6:.0f} TRX on {addr_t} for network energy. Nothing sent."
    allow_w = _const(token_hex, owner_hex, "allowance(address,address)", _w(owner_hex) + _w(router_hex))
    note = ""
    if not allow_w or allow_w[0] < bal:
        built = _post("/wallet/triggersmartcontract", {
            "owner_address": owner_hex, "contract_address": token_hex, "function_selector": "approve(address,uint256)",
            "parameter": _w(router_hex) + "f" * 64, "fee_limit": 100_000_000, "call_value": 0, "visible": False})
        res, link, _b = _send_and_wait(built, raw)
        if res != "SUCCESS":
            return False, f"TRON approve didn't confirm ({res}). Nothing sold.\n{link}"
        note = f"Approved: {link}\n"
    slip = max(10, min(5000, int(slip_bps)))
    min_out = max(1, quoted * (10_000 - slip) // 10_000)
    param = f"{bal:064x}" + f"{min_out:064x}" + f"{160:064x}" + _w(owner_hex) + f"{int(time.time()) + 300:064x}"
    param += f"{2:064x}" + _w(token_hex) + _w(wtrx_hex)
    built2 = _post("/wallet/triggersmartcontract", {
        "owner_address": owner_hex, "contract_address": router_hex,
        "function_selector": "swapExactTokensForETH(uint256,uint256,address[],address,uint256)",
        "parameter": param, "fee_limit": 150_000_000, "call_value": 0, "visible": False})
    result, link, burned = _send_and_wait(built2, raw)
    if result.startswith("not sent"):
        return False, note + "TRON sell failed: " + result[10:]
    if result == "SUCCESS":
        return True, f"{note}Live TRON sell (~{quoted / 1e6:,.2f} TRX quoted, max slippage {slip / 100:g}%)\n{link}"
    if result == "unconfirmed":
        return False, f"{note}TRON sell sent but not confirmed within a minute. Check before retrying:\n{link}"
    return False, (f"{note}TRON sell didn't fill ({result}): price moved more than {slip / 100:g}%. Tokens are still "
                   f"in your wallet; about {burned:.2f} TRX went to energy.\n{link}")


def status_text(key_hex: str = "") -> str:
    raw = (key_hex or os.getenv("SIGNER_KEY_EVM") or "").replace("0x", "")
    if not raw:
        return "TRON: same key as EVM. /wallet first."
    try:
        addr, _ = evm_key_to_tron(raw)
    except Exception as exc:
        return f"TRON key error: {exc}"
    return f"TRON {addr}\nSunSwap V2 live. Fund TRX + energy on that address."
