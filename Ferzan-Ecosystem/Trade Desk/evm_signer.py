"""EVM hot wallet via 0x allowance-holder. This droplet only."""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("evm_signer")

from chains import CHAINS, ZEROX_LIVE, resolve_chain

NATIVE = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"
ZEROX = "https://api.0x.org/swap/allowance-holder/quote"
SUPPORTED = set(ZEROX_LIVE)

# CoinGecko id for the gas token so $ size is right on each chain.
NATIVE_CG = {
    "eth": "ethereum",
    "base": "ethereum",
    "arb": "ethereum",
    "op": "ethereum",
    "linea": "ethereum",
    "ink": "ethereum",
    "hood": "ethereum",
    "bsc": "binancecoin",
    "avax": "avalanche-2",
    "pol": "matic-network",
    "sonic": "sonic-3",
    "hype": "hyperliquid",
    "monad": "monad",
    "pulse": "pulsechain",
}


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
    raw = os.getenv("LIVE_BUYS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


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
        "Live EVM: " + ", ".join(sorted(SUPPORTED))
    )


def _native_decimals(chain: str) -> int:
    return 18


def _launch_get(path: str) -> dict:
    base = (os.getenv("LAUNCH_API_URL") or "http://127.0.0.1:8000").rstrip("/")
    token = (os.getenv("INTERNAL_API_TOKEN") or "").strip()
    headers = {"X-Ferzan-Internal": token} if token else {}
    r = requests.get(base + path, headers=headers, timeout=8)
    if r.status_code >= 400:
        log.warning("launch api %s -> %s %s", path, r.status_code, r.text[:180])
        return {}
    return r.json() if r.content else {}


def _referrer_wallet(user_id: int | None) -> str:
    if not user_id:
        return ""
    try:
        data = _launch_get(f"/internal/referrer-wallet/{int(user_id)}")
        return str(data.get("wallet") or "").strip()
    except Exception as exc:
        log.warning("referrer wallet lookup failed user=%s: %s", user_id, exc)
        return ""


def _curve_for_token(token: str) -> str:
    try:
        data = _launch_get(f"/internal/curve-for-token/{token}")
        return str(data.get("curve") or "").strip()
    except Exception as exc:
        log.warning("curve lookup failed token=%s: %s", token, exc)
        return ""


def _encode_curve_buy(min_out: int, referrer: str) -> str:
    from eth_hash.auto import keccak

    sel = keccak(b"buy(uint256,address)")[:4]
    ref = (referrer or "").replace("0x", "").replace("0X", "").zfill(40)
    if len(ref) != 40:
        ref = "0" * 40
    return "0x" + sel.hex() + int(min_out).to_bytes(32, "big").hex() + ref.rjust(64, "0")


def _quote_curve_tokens(rpc: str, curve: str, wei: int) -> int:
    from eth_hash.auto import keccak

    sel = keccak(b"quoteBuy(uint256)")[:4]
    data = "0x" + sel.hex() + int(wei).to_bytes(32, "big").hex()
    try:
        body = _rpc(rpc, "eth_call", [{"to": curve, "data": data}, "latest"])
        raw = str((body or {}).get("result") or "0x0")
        return int(raw, 16)
    except Exception:
        return 0


def buy_curve(
    chain: str,
    curve: str,
    usd: float,
    key_hex: str | None = None,
    referrer: str = "",
    slip_bps: int | None = None,
) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live buys OFF. LIVE_BUYS=1"
    cid = resolve_chain(chain)
    if cid not in SUPPORTED:
        return False, f"Curve buy is EVM only. Not {chain}."
    curve = _addr(curve)
    raw = (key_hex or _key_hex()).replace("0x", "").replace("0X", "")
    from eth_account import Account
    from price_fetcher import get_price_usd

    meta = CHAINS[cid]
    try:
        px = float(get_price_usd(NATIVE_CG.get(cid, "ethereum")) or 0)
    except Exception:
        px = 0.0
    if px <= 0:
        px = 600.0 if cid == "bsc" else 3000.0
    wei = max(10**12, int((usd / max(px, 1e-9)) * 10 ** _native_decimals(cid)))
    acct = Account.from_key("0x" + raw)
    quoted = _quote_curve_tokens(meta["rpc"], curve, wei)
    slip = int(slip_bps if slip_bps is not None else 1000)
    min_out = 0 if quoted <= 0 else max(1, quoted * (10_000 - max(1, min(slip, 4900))) // 10_000)
    data = _encode_curve_buy(min_out, referrer)
    raw_tx = {
        "to": curve,
        "data": data,
        "value": wei,
        "chainId": int(meta["chain_id"]),
        "gas": 350000,
        "gasPrice": _gas_price(meta["rpc"]),
        "nonce": _nonce_guarded(meta, acct.address),
    }
    try:
        signed = acct.sign_transaction(raw_tx)
        raw_hex = "0x" + signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        body = _send_raw(meta, raw_hex, acct.address, raw_tx["nonce"])
    except Exception as exc:
        return False, f"Curve buy failed: {exc}"
    if body.get("error"):
        err = body["error"]
        return False, str(err.get("message") if isinstance(err, dict) else err)
    txh = body.get("result") or ""
    if not txh:
        return False, "Curve RPC accepted nothing."
    exp = (meta.get("explorer_tx") or "https://basescan.org/tx/{txid}").format(txid=txh)
    tag = f" ref {referrer[:8]}…" if referrer else ""
    return True, f"Live curve buy ~${usd:.2f}{tag}\n{exp}"


def buy_evm(
    chain: str,
    buy_token: str,
    usd: float,
    key_hex: str | None = None,
    slip_bps: int | None = None,
    user_id: int | None = None,
) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live buys OFF. LIVE_BUYS=1"
    cid = resolve_chain(chain)
    if not key_hex and not configured():
        return False, "Set SIGNER_KEY_EVM and ZEROX_API_KEY."
    if cid == "pulse":
        return False, "Pulse is signals-only until PulseX is wired. Use SOL / ETH / Base / BNB / ARB / AVAX / POL / OP / Linea / Sonic / HYPE / Hood / Ink / Monad."
    if cid in {"trx", "ton"}:
        return False, f"{cid.upper()} is signals-only. Live swap is EVM + Solana."
    if cid not in SUPPORTED:
        return False, f"Live EVM is {', '.join(sorted(SUPPORTED))}. Not {chain}."
    token = (buy_token or "").strip()
    if not token.startswith("0x") or len(token) != 42:
        return False, "Need a 0x contract."
    curve = _curve_for_token(token)
    if curve:
        return buy_curve(
            cid,
            curve,
            usd,
            key_hex=key_hex,
            referrer=_referrer_wallet(user_id),
            slip_bps=slip_bps,
        )
    usd = min(max(1.0, float(usd)), max_usd())
    try:
        from eth_account import Account
        from price_fetcher import get_price_usd
    except Exception as exc:
        return False, f"EVM deps missing: {exc}"

    meta = CHAINS[cid]
    try:
        px = float(get_price_usd(NATIVE_CG.get(cid, "ethereum")) or 0)
    except Exception:
        px = 0.0
    if px <= 0:
        px = 600.0 if cid == "bsc" else 20.0 if cid in {"avax", "pol", "hype", "sonic", "monad"} else 3000.0
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
                "slippageBps": str(int(slip_bps if slip_bps is not None else 1000)),
            },
            timeout=20,
        )
        quote = qr.json() if qr.content else {}
    except requests.RequestException as exc:
        if cid == "hood":
            import hood

            return hood.buy_hood(buy_token, usd, key_hex)
        return False, f"0x quote failed: {exc}"
    if qr.status_code >= 400:
        if cid == "hood":
            import hood

            return hood.buy_hood(buy_token, usd, key_hex)
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
        "nonce": _nonce_guarded(meta, acct.address),
    }
    try:
        signed = acct.sign_transaction(raw_tx)
        raw_hex = "0x" + signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        body = _send_raw(meta, raw_hex, acct.address, raw_tx["nonce"])
    except Exception as exc:
        return False, f"EVM broadcast failed: {exc} | to={raw_tx.get('to')}"
    if body.get("error"):
        err = body["error"]
        return False, str(err.get("message") if isinstance(err, dict) else err)
    txh = body.get("result") or ""
    if not txh:
        return False, "RPC accepted nothing."
    exp = (meta.get("explorer_tx") or "https://basescan.org/tx/{txid}").format(txid=txh)
    return True, f"Live {cid.upper()} buy ~${usd:.2f}\n{exp}"


def _nonce(rpc: str, addr: str) -> int:
    r = requests.post(
        rpc,
        json={"jsonrpc": "2.0", "id": 1, "method": "eth_getTransactionCount", "params": [addr, "pending"]},
        timeout=15,
    )
    data = r.json() if r.content else {}
    val = data.get("result") or "0x0"
    return int(val, 16)


# --- EVM MEV protection -------------------------------------------------
# Only the final signed tx goes to a private, MEV-protected endpoint; quotes,
# balances, gas and nonces stay on the chain's normal RPC. If the private
# endpoint refuses, the SAME signed tx (same hash + nonce, so it can never
# double-trade) goes out through the public RPC instead.
# EVM_MEV_PROTECT=0 in .env turns this off desk-wide;
# EVM_MEV_RPC_<chainId>=<url> overrides/adds an endpoint for a chain.
_MEV_RPCS = {
    1: "https://rpc.flashbots.net/fast",        # Ethereum: Flashbots Protect
    56: "https://bscrpc.pancakeswap.finance",   # BNB Chain: PancakeSwap MEV Guard
}
# Private txs don't show in the public "pending" pool, so the public RPC would
# hand the NEXT trade the same nonce. Remember nonces we sent privately.
_NONCE_MEM: dict = {}
_NONCE_MEM_TTL = 180  # seconds


def _mev_rpc(meta: dict) -> str:
    if (os.getenv("EVM_MEV_PROTECT", "1").strip().lower()) in {"0", "false", "off", "no"}:
        return ""
    cid = int(meta.get("chain_id") or 0)
    return (os.getenv(f"EVM_MEV_RPC_{cid}") or _MEV_RPCS.get(cid, "")).strip()


def _nonce_guarded(meta: dict, addr: str) -> int:
    import time as _time

    n = _nonce(meta["rpc"], addr)
    rec = _NONCE_MEM.get((int(meta.get("chain_id") or 0), str(addr).lower()))
    if rec and _time.time() - rec[1] < _NONCE_MEM_TTL and rec[0] + 1 > n:
        return rec[0] + 1
    return n


def _send_raw(meta: dict, raw_hex: str, addr: str = "", nonce=None) -> dict:
    import time as _time

    priv = _mev_rpc(meta)
    if priv:
        try:
            body = _rpc(priv, "eth_sendRawTransaction", [raw_hex])
        except Exception as exc:
            body = {"error": {"message": f"{exc}"}}
        if isinstance(body, dict) and body.get("result"):
            if addr and nonce is not None:
                _NONCE_MEM[(int(meta.get("chain_id") or 0), str(addr).lower())] = (int(nonce), _time.time())
            log.info("evm tx sent via MEV-protected rpc (chain %s): %s", meta.get("chain_id"), body.get("result"))
            return body
        err = body.get("error") if isinstance(body, dict) else body
        log.warning("MEV-protected rpc refused (chain %s): %s -- sending same tx via public rpc", meta.get("chain_id"), err)
    return _rpc(meta["rpc"], "eth_sendRawTransaction", [raw_hex])


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


# Error text that means "try again with a bigger gas price", not "this tx is
# broken" -- retrying on anything else (bad nonce reused wrong, insufficient
# funds, reverts) would just resend the same failure.
_UNDERPRICED_HINTS = (
    "underpriced",
    "fee too low",
    "gas price too low",
    "max fee per gas less than",
    "transaction fee is too low",
)


def _broadcast(acct, meta: dict, to: str, data: str, value: int = 0, gas_limit: int | None = None) -> tuple[bool, str]:
    data_hex = data if str(data).startswith("0x") else "0x" + str(data)
    # gas_limit: callers that pre-computed the exact fee (a "send all"
    # withdrawal) pass it so the fee we sign matches the fee they reserved.
    gas = int(gas_limit) if gas_limit else _estimate_gas(meta["rpc"], acct.address, to, data_hex, int(value))
    nonce = _nonce_guarded(meta, acct.address)
    base_gas_price = _gas_price(meta["rpc"])
    bumps = (1.2, 1.6, 2.2)  # first try, then two retries if underpriced
    last_err = "RPC accepted nothing."
    for attempt, bump in enumerate(bumps):
        raw_tx = {
            "to": _addr(to),
            "data": data_hex,
            "value": int(value),
            "chainId": int(meta["chain_id"]),
            "gas": gas,
            "gasPrice": int(base_gas_price * bump),
            "nonce": nonce,
        }
        signed = acct.sign_transaction(raw_tx)
        raw_hex = signed.raw_transaction.hex() if hasattr(signed, "raw_transaction") else signed.rawTransaction.hex()
        if not raw_hex.startswith("0x"):
            raw_hex = "0x" + raw_hex
        body = _send_raw(meta, raw_hex, acct.address, nonce)
        if body.get("error"):
            err = body["error"]
            last_err = str(err.get("message") if isinstance(err, dict) else err)
            if attempt < len(bumps) - 1 and any(h in last_err.lower() for h in _UNDERPRICED_HINTS):
                log.warning("tx underpriced (attempt %s), bumping gas and retrying: %s", attempt + 1, last_err)
                continue
            return False, last_err
        txh = body.get("result") or ""
        if not txh:
            if attempt < len(bumps) - 1:
                continue
            return False, last_err
        cid = int(meta.get("chain_id") or 1)
        exp = (meta.get("explorer_tx") or "https://basescan.org/tx/{txid}").format(txid=txh)
        return True, exp
    return False, last_err


def send_native(chain: str, dest: str, key_hex: str | None = None) -> tuple[bool, str]:
    cid = resolve_chain(chain) or "eth"
    meta = CHAINS.get(cid) or CHAINS["eth"]
    dest = _addr(dest)
    try:
        from eth_account import Account
    except Exception as exc:
        return False, str(exc)
    raw = (key_hex or _key_hex()).replace("0x", "").replace("0X", "")
    acct = Account.from_key("0x" + raw)
    body = _rpc(meta["rpc"], "eth_getBalance", [acct.address, "latest"])
    wei = int(body.get("result") or "0x0", 16)
    gas_price = int(_gas_price(meta["rpc"]) * 1.2)
    gas = 21000
    need = gas * gas_price
    if wei <= need + 10**12:
        return False, f"Not enough {meta.get('native')} to collect."
    value = wei - need
    ok, msg = _broadcast(acct, meta, dest, "0x", value)
    if not ok:
        return False, "Collect failed: " + msg
    return True, f"Collected {value / 10**18:.6f} {meta.get('native')}\n{msg}"


def sell_evm(chain: str, sell_token: str, key_hex: str | None = None, pct: int = 100) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live sells OFF. LIVE_BUYS=1"
    cid = resolve_chain(chain)
    if not key_hex and not configured():
        return False, "Set SIGNER_KEY_EVM and ZEROX_API_KEY."
    if cid == "pulse":
        return False, "Pulse sell is not wired yet."
    if cid in {"trx", "ton"}:
        return False, f"{cid.upper()} is signals-only."
    if cid not in SUPPORTED:
        return False, f"Live EVM is {', '.join(sorted(SUPPORTED))}. Not {chain}."
    token = _addr(sell_token)
    try:
        from eth_account import Account
    except Exception as exc:
        return False, f"EVM deps missing: {exc}"
    meta = CHAINS[cid]
    raw = (key_hex or _key_hex()).replace("0x", "").replace("0X", "")
    acct = Account.from_key("0x" + raw)
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
    pct = max(1, min(100, int(pct)))
    bal = bal * pct // 100
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
        if cid == "hood":
            import hood

            return hood.sell_hood(sell_token, key_hex)
        return False, f"0x quote failed: {exc}"
    if qr.status_code >= 400:
        if cid == "hood":
            import hood

            return hood.sell_hood(sell_token, key_hex)
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
