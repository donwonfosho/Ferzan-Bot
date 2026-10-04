"""TON live buy + sell via STON.fi v1 (router SWAP op 0x25938561), signed with WalletV4R2."""

from __future__ import annotations

import os
import time

import requests

from evm_signer import live_enabled, max_usd

STON = "https://api.ston.fi"
# Official TON asset id used by STON.fi
TON_ASSET = "EQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAM9c"

# STON.fi v1 router "swap request" opcode. It always travels as the
# forward_payload (in a ref) of a TEP-74 jetton transfer: for sells that
# transfer goes to OUR jetton wallet, for buys it goes to the router's pTON
# wallet (pTON v1 takes a jetton-transfer-shaped body, per ston-fi/sdk
# PtonV1.getTonTransferTxParams).
SWAP_OP = 0x25938561
JETTON_TRANSFER_OP = 0xF8A7EA5
# ston-fi/sdk RouterV1.gasConstants.swapTonToJetton.forwardGasAmount
TON_TO_JETTON_FWD_NANO = 185_000_000

# Minimum extra TON (beyond the swap amount) reserved for router/pool gas if
# the STON.fi quote doesn't hand us gas_params for some reason.
MIN_GAS_RESERVE_NANO = 150_000_000  # 0.15 TON
MAX_GAS_RESERVE_NANO = 400_000_000  # 0.40 TON safety cap


def _headers() -> dict:
    h = {"Accept": "application/json"}
    key = (os.environ.get("TONAPI_KEY") or "").strip()  # optional paid/free key from tonconsole.com: higher limits
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _toncenter_headers() -> dict:
    key = (os.environ.get("TONCENTER_API_KEY") or "").strip()  # optional key from @tonapibot: higher limits
    return {"X-API-Key": key} if key else {}


def simulate(ask: str, units: str, slip: str = "0.05", offer: str | None = None) -> dict:
    """POST /v1/swap/simulate. STON.fi's v1 endpoint takes query-string params
    on a POST (not a JSON body, and not GET — both were confirmed live against
    the real API: GET -> 405, POST+JSON body -> 400 'missing field offer_address').
    `units` is the offer-side amount in base units (nanoTON when offer=TON_ASSET).
    """
    try:
        r = requests.post(
            f"{STON}/v1/swap/simulate",
            params={
                "offer_address": offer or TON_ASSET,
                "ask_address": ask,
                "units": str(units),
                "slippage_tolerance": slip,
            },
            headers=_headers(),
            timeout=20,
        )
    except requests.RequestException as exc:
        return {"error": str(exc)}
    try:
        data = r.json()
    except Exception:
        return {"error": r.text[:180], "status": r.status_code}
    if r.status_code >= 400:
        return {"error": data.get("error") or data.get("message") or str(data)[:180], "status": r.status_code}
    return data


def _run_async(coro):
    """Run an async coroutine from sync code that may already be inside a
    running event loop (python-telegram-bot's). asyncio.run() would raise
    'cannot be called from a running event loop' in that case, so we hand
    the coroutine to a fresh thread with its own loop instead.
    """
    import asyncio
    import concurrent.futures

    def runner():
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(runner).result()


_BCAST = {"n": 0}  # bumped every time a signed message is handed to the network


def _is_node_lag(exc) -> bool:
    low = str(exc).lower()
    return any(k in low for k in ("651", "cannot load block", "out of sync", "not in db", "liteserver",
                                  "timeout", "timed out", "connection", "no alive", "lite server"))


def _run_retry(make_coro, tries: int = 3):
    """Run a whole TON trade attempt; if a public node lags BEFORE anything was broadcast, start over on fresh
    nodes (a new LiteBalancer picks different liteservers). Never retries once a message has gone out."""
    import time as _t

    last = None
    for i in range(tries):
        before = _BCAST["n"]
        try:
            return _run_async(make_coro())
        except Exception as exc:  # noqa: BLE001
            last = exc
            if _BCAST["n"] != before or not _is_node_lag(exc) or i == tries - 1:
                raise
            _t.sleep(1.5 * (i + 1))
    raise last  # pragma: no cover


def _friendly_err(exc) -> str:
    """Public liteservers sometimes lag behind the chain ("cannot load block", code 651). That is a node problem,
    not a wallet problem: say so plainly instead of dumping the raw error at the user."""
    s = str(exc)
    low = s.lower()
    if "651" in low or "cannot load block" in low or "out of sync" in low or "not in db" in low or "liteserver" in low:
        return "a TON network node was out of sync. Nothing was sent. Tap try again in a few seconds."
    return s[:200]


def _http_balance_nano(addr: str) -> int | None:
    """Balance in nanoTON from the public HTTP APIs (tonapi, then toncenter). None when neither answers."""
    try:
        r = requests.get(f"https://tonapi.io/v2/accounts/{addr}", headers=_headers(), timeout=10)
        if r.status_code == 200:
            return int((r.json() or {}).get("balance") or 0)
        if r.status_code == 404:
            return 0  # never-used address: the chain has no record of it yet
    except Exception:  # noqa: BLE001
        pass
    try:
        r = requests.get("https://toncenter.com/api/v2/getAddressBalance", params={"address": addr},
                         headers=_toncenter_headers(), timeout=10)
        j = r.json() or {}
        if r.status_code == 200 and j.get("ok"):
            return int(j.get("result") or 0)
    except Exception:  # noqa: BLE001
        pass
    return None


def _http_seqno(addr: str) -> int | None:
    """Wallet seqno over HTTP. 0 only when the chain positively says the wallet was never deployed.
    None on any doubt, so the caller refuses instead of signing with a guessed number."""
    try:
        r = requests.get("https://toncenter.com/api/v2/getWalletInformation", params={"address": addr},
                         headers=_toncenter_headers(), timeout=10)
        j = r.json() or {}
        res = j.get("result") or {}
        if r.status_code == 200 and j.get("ok") and res.get("seqno") is not None:
            return int(res["seqno"])
    except Exception:  # noqa: BLE001
        pass
    try:
        r = requests.get(f"https://tonapi.io/v2/accounts/{addr}", headers=_headers(), timeout=10)
        if r.status_code == 404:
            return 0
        if r.status_code == 200 and str((r.json() or {}).get("status") or "").lower() in ("uninit", "nonexist"):
            return 0
    except Exception:  # noqa: BLE001
        pass
    return None


def jetton_amount_pub(owner: str, jetton: str) -> float | None:
    """Jetton balance (whole units) for a wallet address, from public data only. 0.0 when the wallet holds none,
    None when the answer could not be read. Needs no key, so the Mini App can use it."""
    try:
        r = requests.get(f"https://tonapi.io/v2/accounts/{owner}/jettons/{jetton}", headers=_headers(), timeout=10)
        if r.status_code == 404:
            return 0.0
        if r.status_code != 200:
            return None
        j = r.json() or {}
        dec = int(((j.get("jetton") or {}).get("decimals")) or 9)
        return int(j.get("balance") or 0) / (10 ** dec)
    except Exception:  # noqa: BLE001
        return None


_ADDR: dict[str, str] = {}  # public key (hex) -> wallet address, filled by every lookup that worked


def _remember_addr(seed64: bytes, addr: str) -> None:
    if addr:
        _ADDR[bytes(seed64[32:]).hex()] = addr


def _offline_address(seed64: bytes) -> str | None:
    """The wallet address from the key alone (no network): remembered from an earlier lookup, else derived.
    None if neither works."""
    known = _ADDR.get(bytes(seed64[32:]).hex())
    if known:
        return known
    try:
        import asyncio

        from pytoniq import WalletV4R2

        w = asyncio.run(WalletV4R2.from_private_key(None, seed64))
        addr = w.address.to_str(is_user_friendly=True, is_bounceable=False)
        _remember_addr(seed64, addr)
        return addr
    except Exception:  # noqa: BLE001
        return None


def _ton_keypair_bytes(secret: str):
    """Ferzan's TON wallet reuses the user's Solana ed25519 key (same curve).
    pytoniq's from_private_key wants the full 64-byte NaCl secret key
    (32-byte seed + 32-byte pubkey) — same layout solders.Keypair stores.
    """
    from solders.keypair import Keypair

    secret = (secret or "").strip()
    if not secret:
        raise ValueError("No TON/Solana key on file. Open /wallet first.")
    try:
        kp = Keypair.from_base58_string(secret)
    except Exception:
        import base64

        kp = Keypair.from_bytes(base64.b64decode(secret))
    return bytes(kp)


async def _address_and_balance(seed64: bytes) -> tuple[str, int]:
    from pytoniq import LiteBalancer, WalletV4R2

    provider = LiteBalancer.from_mainnet_config(trust_level=2)
    await provider.start_up()
    try:
        wallet = await WalletV4R2.from_private_key(provider, seed64)
        state = await provider.get_account_state(wallet.address)
        nano = int(getattr(state, "balance", 0) or 0)
        # UQ.. (non-bounceable): what people should send to. TON sent to the EQ.. form of a wallet that was
        # never used bounces straight back.
        addr = wallet.address.to_str(is_user_friendly=True, is_bounceable=False)
        _remember_addr(seed64, addr)
        return addr, nano
    finally:
        await provider.close_all()


def address_and_balance(secret: str) -> tuple[str, float]:
    """(TON address, TON balance) for the wallet derived from `secret` —
    same ed25519 key as the user's Solana wallet, different derived address."""
    seed64 = _ton_keypair_bytes(secret)
    # Fast path: the address comes from the key and the balance from the public HTTP APIs (about a
    # second). The slow liteserver round trips below are only for when that does not answer.
    _fast = _offline_address(seed64)
    if _fast:
        _nano = _http_balance_nano(_fast)
        if _nano is not None:
            return _fast, _nano / 1e9
    last: Exception | None = None
    for attempt in range(3):  # read-only lookup: public liteservers are often busy, so retry before giving up
        try:
            addr, nano = _run_async(_address_and_balance(seed64))
            return addr, nano / 1e9
        except Exception as exc:
            last = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    # Every liteserver try failed: the address comes from the key and the balance from the public HTTP APIs.
    addr = _offline_address(seed64)
    if addr:
        nano = _http_balance_nano(addr)
        if nano is not None:
            return addr, nano / 1e9
    raise last if last else RuntimeError("TON lookup failed")


# Signed messages expire after this; we poll for longer, so "not confirmed"
# means the message can no longer land and a user retry can't double-trade.
MSG_TTL_S = 40
CONFIRM_WAIT_S = 60.0


async def _is_uninitialized(provider, wallet) -> bool:
    """True only when the chain positively says this wallet has no contract
    yet (never used). Any doubt -> False, so callers refuse rather than sign
    with a guessed seqno 0."""
    state = await provider.get_account_state(wallet.address)
    text = repr(state).lower()
    return ("uninit" in text or "nonexist" in text) and "active" not in text


async def _seqno(wallet) -> int:
    """Seqno for polling. A failed read counts as 'not advanced yet'."""
    try:
        return int(await wallet.get_seqno())
    except Exception:
        return -1


async def _seqno_for_send(provider, wallet) -> int:
    """Seqno to sign with. 0 ONLY for a verified-undeployed wallet; if the
    get-method fails on a deployed wallet we raise instead of signing with 0
    (a stale 0 could make a trade that never executes look confirmed)."""
    import asyncio

    last: Exception | None = None
    for i in range(3):  # a different liteserver is usually picked on the next try
        try:
            return int(await wallet.get_seqno())
        except Exception as exc:  # noqa: BLE001
            last = exc
            await asyncio.sleep(1.0 * (i + 1))
    try:
        if await _is_uninitialized(provider, wallet):
            return 0
    except Exception:  # noqa: BLE001
        pass
    sq = await asyncio.to_thread(_http_seqno, wallet.address.to_str(is_user_friendly=True, is_bounceable=False))
    if sq is not None:
        return sq
    raise RuntimeError(f"couldn't read wallet seqno, nothing sent: {_friendly_err(last)}") from last


async def _await_seqno(wallet, sent_seqno: int, timeout_s: float = CONFIRM_WAIT_S) -> bool:
    """True once the wallet's seqno moves past the one we signed with, i.e.
    the chain accepted our external message. That proves the wallet sent the
    swap message; it does NOT prove the swap filled (a slippage bounce
    refunds TON/jettons back to the wallet)."""
    import asyncio
    import time as _t

    deadline = _t.monotonic() + timeout_s
    while _t.monotonic() < deadline:
        await asyncio.sleep(3)
        if await _seqno(wallet) > sent_seqno:
            return True
    return False


async def broadcast(provider, boc: bytes) -> tuple[bool, str]:
    """Hands a signed external message to the network by three routes: our liteserver plus the public
    tonapi and toncenter relays. One liteserver alone can accept a message and still drop it before it
    reaches a block. Sending the same message twice is safe: the wallet seqno lets it land only once."""
    import asyncio
    import base64

    b64, errs, ok = base64.b64encode(boc).decode(), [], False
    _BCAST["n"] += 1
    try:
        await provider.raw_send_message(boc)
        ok = True
    except Exception as exc:  # noqa: BLE001
        errs.append(f"liteserver: {str(exc)[:80]}")
    for url in ("https://tonapi.io/v2/blockchain/message", "https://toncenter.com/api/v2/sendBoc"):
        try:
            r = await asyncio.to_thread(requests.post, url, json={"boc": b64}, headers=(_toncenter_headers() if "toncenter" in url else _headers()), timeout=20)
            if r.status_code == 200:
                ok = True
            else:
                errs.append(f"{url.split('/')[2]}: HTTP {r.status_code} {r.text[:80]}")
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{url.split('/')[2]}: {type(exc).__name__}")
    return ok, "; ".join(errs)


async def _send_one(provider, wallet, destination, value: int, body, **msg_kwargs) -> tuple[str, int]:
    """Sign + broadcast one internal message from `wallet`. Handles a
    never-used wallet (no contract deployed yet): seqno 0 + state_init in the
    same external message deploys the wallet AND executes the transfer —
    wallet.transfer() can't do that (get_seqno fails on an undeployed
    contract and it never attaches state_init). Returns the external
    message hash (hex) for an explorer link, and the seqno it was signed
    with (pass that to _await_seqno)."""
    import time as _t

    msg = wallet.create_wallet_internal_message(destination=destination, value=value, body=body, **msg_kwargs)
    seqno = await _seqno_for_send(provider, wallet)
    # state_init is ignored by the chain for an already-active account, so
    # attaching it whenever seqno is 0 is safe either way.
    state_init = wallet.state_init if seqno == 0 else None
    signed = wallet.raw_create_transfer_msg(
        private_key=wallet.private_key,
        seqno=seqno,
        wallet_id=wallet.wallet_id,
        messages=[msg],
        valid_until=int(_t.time()) + MSG_TTL_S,
    )
    ext = wallet.create_external_msg(dest=wallet.address, state_init=state_init, body=signed)
    cell = ext.serialize()
    ok, why = await broadcast(provider, cell.to_boc())
    if not ok:
        raise RuntimeError(f"no TON relay accepted the message ({why})")
    return cell.hash.hex(), seqno


def _sdk_swap(direction: str, wallet: str, jetton: str, units: int, slip: str) -> dict:
    """Swap message for pools the hand-built v1 code cannot make (STON.fi v2): built by the official SDK in node,
    which signs and sends nothing. Returns {to, value, body_b64, ...} or {"error": ...}."""
    import json
    import subprocess
    from pathlib import Path

    d = Path(os.getenv("TON_SDK_DIR") or (Path(__file__).resolve().parent.parent / "Launch Bot" / "scripts" / "ton-keeper"))
    script = d / "ton_swap_params.mjs"
    if not script.exists():
        return {"error": "the STON.fi v2 helper is not installed on this server"}
    arg = json.dumps({"dir": direction, "wallet": wallet, "jetton": jetton, "units": str(units), "slip": slip})
    try:
        r = subprocess.run(["node", str(script), arg], cwd=str(d), capture_output=True, text=True, timeout=60)
        data = json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        return {"error": f"v2 helper failed: {str(exc)[:120]}"}
    return data if data.get("ok") else {"error": data.get("error") or "v2 helper failed"}


async def _send_sdk_params(provider, wallet, p: dict) -> tuple[str, int]:
    import base64

    from pytoniq_core import Address, Cell

    body = Cell.one_from_boc(base64.b64decode(p["body_b64"]))
    return await _send_one(provider, wallet, Address(p["to"]), int(p["value"]), body)


def _swap_body(ask_wallet: str, min_out: int, to_address):
    from pytoniq_core import Address, begin_cell

    return (
        begin_cell()
        .store_uint(SWAP_OP, 32)
        .store_address(Address(ask_wallet))
        .store_coins(min_out)
        .store_address(to_address)
        .store_bit(0)  # has_ref (no referral)
        .end_cell()
    )


async def _swap_ton_to_jetton(
    seed64: bytes, router_addr: str, pton_wallet: str, ask_wallet: str, nano: int, min_out: int, fwd: int
) -> tuple[str, str, bool]:
    """Mirror of ston-fi/sdk RouterV1.getSwapTonToJettonTxParams +
    PtonV1.getTonTransferTxParams: jetton-transfer body to the router's pTON
    wallet, value = offer + forward gas. Returns (wallet, msg hash, confirmed)."""
    from pytoniq import LiteBalancer, WalletV4R2
    from pytoniq_core import Address, begin_cell

    provider = LiteBalancer.from_mainnet_config(trust_level=2)
    await provider.start_up()
    try:
        wallet = await WalletV4R2.from_private_key(provider, seed64)
        swap = _swap_body(ask_wallet, min_out, wallet.address)
        body = (
            begin_cell()
            .store_uint(JETTON_TRANSFER_OP, 32)
            .store_uint(0, 64)  # query_id
            .store_coins(nano)  # amount of TON being swapped
            .store_address(Address(router_addr))  # destination = router
            .store_uint(0, 2)  # response_destination = addr_none (SDK passes none)
            .store_bit(0)  # no custom_payload
            .store_coins(fwd)  # forward_ton_amount
            .store_bit(1)  # forward_payload in a ref
            .store_ref(swap)
            .end_cell()
        )
        h, seqno = await _send_one(provider, wallet, Address(pton_wallet), nano + fwd, body)
        ok = await _await_seqno(wallet, seqno)
        return wallet.address.to_str(), h, ok
    finally:
        await provider.close_all()


def _http_jetton_info(owner: str, jetton: str) -> tuple[str | None, int] | None:
    """(jetton-wallet address or None, raw balance) from tonapi. (None, 0) when the owner holds none of it,
    None when the answer could not be read."""
    try:
        r = requests.get(f"https://tonapi.io/v2/accounts/{owner}/jettons/{jetton}", headers=_headers(), timeout=10)
        if r.status_code == 404:
            return None, 0
        if r.status_code != 200:
            return None
        j = r.json() or {}
        return ((j.get("wallet_address") or {}).get("address") or None), int(j.get("balance") or 0)
    except Exception:  # noqa: BLE001
        return None


def _is_empty_wallet_error(exc: Exception) -> bool:
    """True when a get-method failed because the contract itself is missing or not active (a jetton wallet that
    was never deployed), as opposed to the liteserver being behind, busy or unreachable."""
    low = str(exc).lower()
    if "651" in low or "cannot load block" in low or "out of sync" in low or "not in db" in low or "liteserver" in low:
        return False
    return type(exc).__name__ == "RunGetMethodError" or "exit code" in low or "exit_code" in low


async def _jetton_wallet_and_balance(provider, jetton_master: str, owner) -> tuple[object, int]:
    """TEP-74: ask the jetton master for owner's jetton-wallet address, then
    read that wallet's balance. Balance 0 only when the jetton wallet is truly not deployed: a node that is behind
    or busy is retried, then tonapi is asked, and if nobody answers this raises (it never reports a fake 0)."""
    import asyncio

    from pytoniq_core import Address, begin_cell

    owner_str = owner.to_str(is_user_friendly=True, is_bounceable=False) if hasattr(owner, "to_str") else str(owner)
    last: Exception | None = None
    jw = None
    for i in range(3):
        try:
            res = await provider.run_get_method(
                address=Address(jetton_master),
                method="get_wallet_address",
                stack=[begin_cell().store_address(owner).end_cell().begin_parse()],
            )
            jw = res[0].load_address()
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            await asyncio.sleep(1.0 * (i + 1))
    if jw is None:
        info = await asyncio.to_thread(_http_jetton_info, owner_str, jetton_master)
        if info is not None:
            addr, raw = info
            return (Address(addr) if addr else None), raw
        raise last if last else RuntimeError("jetton wallet lookup failed")
    for i in range(3):
        try:
            data = await provider.run_get_method(address=jw, method="get_wallet_data", stack=[])
            return jw, int(data[0])
        except Exception as exc:  # noqa: BLE001
            if _is_empty_wallet_error(exc):
                return jw, 0
            last = exc
            await asyncio.sleep(1.0 * (i + 1))
    info = await asyncio.to_thread(_http_jetton_info, owner_str, jetton_master)
    if info is not None:
        return jw, info[1]
    raise last if last else RuntimeError("jetton balance lookup failed")


def _jetton_decimals(jetton: str) -> int:
    """Display-only: STON.fi asset metadata, falling back to the TEP-64
    default of 9 (USDT-style 6-decimal jettons are listed by STON.fi)."""
    try:
        r = requests.get(f"{STON}/v1/assets/{jetton}", headers=_headers(), timeout=10)
        dec = ((r.json() or {}).get("asset") or {}).get("decimals")
        return int(dec) if dec is not None else 9
    except Exception:
        return 9


async def _holding(seed64: bytes, jetton: str) -> tuple[int, str]:
    from pytoniq import LiteBalancer, WalletV4R2

    provider = LiteBalancer.from_mainnet_config(trust_level=2)
    await provider.start_up()
    try:
        wallet = await WalletV4R2.from_private_key(provider, seed64)
        _jw, bal = await _jetton_wallet_and_balance(provider, jetton, wallet.address)
        return bal, wallet.address.to_str()
    finally:
        await provider.close_all()


def jetton_holding(secret: str, jetton: str) -> tuple[float, str]:
    """(token amount in whole units, TON wallet address). Blocking, read-only."""
    seed64 = _ton_keypair_bytes(secret)
    last: Exception | None = None
    for attempt in range(3):  # public liteservers are sometimes behind: a retry usually lands on a good one
        try:
            raw, owner = _run_async(_holding(seed64, jetton))
            _remember_addr(seed64, owner)
            return raw / (10 ** _jetton_decimals(jetton)), owner
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))
    owner = _offline_address(seed64)
    if owner:
        amt = jetton_amount_pub(owner, jetton)
        if amt is not None:
            return amt, owner
    raise last if last else RuntimeError("TON holding lookup failed")


async def _swap_jetton_to_ton(seed64: bytes, jetton: str, pct: int, slip: str) -> tuple[bool, str]:
    import asyncio

    from pytoniq import LiteBalancer, WalletV4R2
    from pytoniq_core import Address, begin_cell

    provider = LiteBalancer.from_mainnet_config(trust_level=2)
    await provider.start_up()
    try:
        wallet = await WalletV4R2.from_private_key(provider, seed64)
        jw, bal = await _jetton_wallet_and_balance(provider, jetton, wallet.address)
        if bal <= 0:
            return False, "Nothing to sell — this wallet holds 0 of that jetton."
        amount = bal if pct >= 100 else bal * pct // 100
        if amount <= 0:
            return False, "Sell size rounds to 0."
        sim = simulate(TON_ASSET, str(amount), slip=slip, offer=jetton)
        if sim.get("error"):
            return False, "STON.fi quote failed: " + str(sim["error"])[:180]
        if str((sim.get("router") or {}).get("major_version", 1)) != "1":
            p = await asyncio.to_thread(_sdk_swap, "sell", wallet.address.to_str(), jetton, amount, slip)
            if p.get("error"):
                return False, "STON.fi v2 sell failed: " + p["error"] + ". Nothing sent."
            h, seqno = await _send_sdk_params(provider, wallet, p)
            if not await _await_seqno(wallet, seqno):
                return False, ("TON sell sent but not confirmed on-chain within 60s — check the wallet before "
                               f"retrying: https://tonviewer.com/{wallet.address.to_str()}")
            return True, (f"Sold {pct}% via STON.fi v2 · ~{int(p.get('ask_units') or 0) / 1e9:.4f} TON expected\n"
                          f"Wallet: https://tonviewer.com/{wallet.address.to_str()}")
        router_addr = sim.get("router_address") or (sim.get("router") or {}).get("address")
        ask_wallet = sim.get("ask_jetton_wallet")  # router's pTON wallet
        min_out = int(sim.get("min_ask_units") or 0)
        if not router_addr or not ask_wallet or min_out <= 0:
            return False, "STON.fi quote missing router/pool fields — no TON pool for this jetton?"
        gas = sim.get("gas_params") or {}
        fwd = int(gas.get("forward_gas") or 0) or 175_000_000
        total = fwd + (int(gas.get("estimated_gas_consumption") or 0) or 50_000_000)
        total = min(max(total, MIN_GAS_RESERVE_NANO), MAX_GAS_RESERVE_NANO)
        swap = _swap_body(ask_wallet, min_out, wallet.address)
        # TEP-74 jetton transfer, sent to OUR jetton wallet; destination is the
        # router (new owner), forward payload carries the STON.fi swap request.
        body = (
            begin_cell()
            .store_uint(0xF8A7EA5, 32)
            .store_uint(0, 64)  # query_id
            .store_coins(amount)
            .store_address(Address(router_addr))
            .store_address(wallet.address)  # response_destination (excess TON back)
            .store_bit(0)  # no custom_payload
            .store_coins(fwd)  # forward_ton_amount
            .store_bit(1)  # forward_payload in a ref
            .store_ref(swap)
            .end_cell()
        )
        h, seqno = await _send_one(provider, wallet, jw, total, body)
        if not await _await_seqno(wallet, seqno):
            return False, (
                "TON sell sent but not confirmed on-chain within 60s — check the "
                f"wallet before retrying: https://tonviewer.com/{wallet.address.to_str()}"
            )
        out_ton = int(sim.get("ask_units") or 0) / 1e9
        return True, (
            f"Sold {pct}% via STON.fi · ~{out_ton:.4f} TON expected\n"
            f"Wallet: https://tonviewer.com/{wallet.address.to_str()}"
        )
    finally:
        await provider.close_all()


# ------------------------------------------------------------------ Ferzan TON curves --
# A coin launched by the Ferzan TON curve trades on its curve until it graduates to STON.fi. Which coin has which
# curve comes from the curve index (only confirmed launches built by our launch flow are in it, and a curve's address
# commits to its pinned code, so a look-alike contract can't sit at that address).
CURVE_SLIP_BPS = 500      # default 5% when the caller gives none
_CURVES: dict = {"ts": 0.0, "rows": {}}


def _index_db() -> str:
    launch = os.environ.get("LAUNCH_DB_PATH") or "/opt/ferzan/app/launch/launch_bot.db"
    return os.environ.get("CURVE_INDEX_DB") or os.path.join(os.path.dirname(launch) or ".", "curve_index.db")


def _raw(addr: str) -> str:
    from pytoniq_core import Address

    return Address(addr).to_str(is_user_friendly=False).lower()


def _in_thread(fn, *a):
    """ton_curve's chain reads use asyncio.run(); run them on a fresh thread so a running bot loop can't break them."""
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(fn, *a).result(timeout=60)


def _tc():
    import sys
    from pathlib import Path

    d = str(Path(__file__).resolve().parent.parent / "Launch Bot")
    if d not in sys.path:
        sys.path.append(d)
    import ton_curve

    return ton_curve


def curve_info(jetton: str) -> dict:
    """{} for normal coins; {'curve', 'graduated'} for a Ferzan TON curve coin (per the curve index)."""
    import sqlite3
    import time as _t

    if os.environ.get("TON_CURVE_TRADE", "1") == "0":
        return {}
    if _t.time() - _CURVES["ts"] > 20:
        rows = {}
        try:
            c = sqlite3.connect(f"file:{_index_db()}?mode=ro", uri=True, timeout=5)
            for curve, token, grad in c.execute("SELECT curve, token, graduated FROM curves WHERE chain = 'ton'"):
                try:
                    rows[_raw(token)] = {"curve": curve, "graduated": bool(grad)}
                except Exception:
                    continue
            c.close()
        except Exception:
            return _CURVES["rows"].get(_raw(jetton), {}) if _CURVES["rows"] else {}
        _CURVES.update(ts=_t.time(), rows=rows)
    try:
        return dict(_CURVES["rows"].get(_raw(jetton), {}))
    except Exception:
        return {}


async def _ton_balance(provider, addr) -> int:
    """Wallet TON balance before a curve trade: retried on other nodes, then the public HTTP APIs. Raises only if nobody answers
    (never a fake 0 that would block the trade or let it through)."""
    import asyncio

    last: Exception | None = None
    for i in range(3):
        try:
            st = await provider.get_account_state(addr)
            return int(getattr(st, "balance", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            last = exc
            await asyncio.sleep(1.0 * (i + 1))
    nano = await asyncio.to_thread(_http_balance_nano, addr.to_str(is_user_friendly=True, is_bounceable=False))
    if nano is not None:
        return nano
    raise last if last else RuntimeError("couldn't read the wallet balance")


async def _curve_tx(seed64: bytes, jetton: str, msg: dict, expect: str) -> dict:
    """Signs one message from the user's wallet and watches their coin balance to see whether the curve filled it.
    expect: 'up' (a buy adds coins) or 'down' (a sell removes them)."""
    import asyncio
    import base64

    from pytoniq import LiteBalancer, WalletV4R2
    from pytoniq_core import Address, Cell

    provider = LiteBalancer.from_mainnet_config(trust_level=2)
    await provider.start_up()
    try:
        wallet = await WalletV4R2.from_private_key(provider, seed64)
        jw, before = await _jetton_wallet_and_balance(provider, jetton, wallet.address)
        have = await _ton_balance(provider, wallet.address)
        need = int(msg["amount"]) + 60_000_000
        if have < need:
            return {"sent": False, "error": f"Not enough TON: need about {need / 1e9:.3f} (incl. gas), you have {have / 1e9:.3f}."}
        body = Cell.one_from_boc(base64.b64decode(msg["payload"]))
        h, seqno = await _send_one(provider, wallet, Address(msg["address"]), int(msg["amount"]), body)
        landed = await _await_seqno(wallet, seqno)
        after, filled = before, False
        for _ in range(20):
            await asyncio.sleep(3)
            try:
                _jw, after = await _jetton_wallet_and_balance(provider, jetton, wallet.address)
            except Exception:
                continue
            if (expect == "up" and after > before) or (expect == "down" and after < before):
                filled = True
                break
        return {"sent": True, "landed": landed, "filled": filled, "before": before, "after": after,
                "addr": wallet.address.to_str(), "jw": jw.to_str() if hasattr(jw, "to_str") else str(jw), "hash": h}
    finally:
        await provider.close_all()


async def _wallet_coin(seed64: bytes, jetton: str) -> tuple[str, str, int]:
    """(owner address, owner's coin wallet, balance) for the wallet derived from seed64."""
    from pytoniq import LiteBalancer, WalletV4R2

    provider = LiteBalancer.from_mainnet_config(trust_level=2)
    await provider.start_up()
    try:
        wallet = await WalletV4R2.from_private_key(provider, seed64)
        jw, bal = await _jetton_wallet_and_balance(provider, jetton, wallet.address)
        return wallet.address.to_str(), (jw.to_str() if hasattr(jw, "to_str") else str(jw)), int(bal)
    finally:
        await provider.close_all()


def _curve_open(tc, curve: str) -> tuple[dict | None, str]:
    import time as _t

    st = _in_thread(tc.curve_state, curve)
    if st["graduated"] or st["complete"]:
        return None, "This Ferzan curve is full and is moving to STON.fi. Try again in a few minutes. Nothing sent."
    if st["start"] > _t.time():
        return None, f"Trading on this curve opens in about {int((st['start'] - _t.time()) / 60) + 1} min. Nothing sent."
    return st, ""


def _curve_buy(ci: dict, jetton: str, nano: int, usd: float, seed64: bytes, slip_bps: int) -> tuple[bool, str]:
    tc = _tc()
    st, why = _curve_open(tc, ci["curve"])
    if st is None:
        return False, why
    q = _in_thread(tc.quote_buy, ci["curve"], nano)
    if q["tokens_out"] <= 0:
        return False, "The curve would not quote this buy. Nothing sent."
    min_out = q["tokens_out"] * (10_000 - max(1, min(5000, slip_bps))) // 10_000
    msg = tc.build_buy_message(ci["curve"], nano, min_out)
    r = _run_retry(lambda: _curve_tx(seed64, jetton, msg, "up"))
    if not r.get("sent"):
        return False, r.get("error", "not sent") + " Nothing sent."
    link = f"https://tonviewer.com/{r['addr']}"
    if r["filled"]:
        got = (r["after"] - r["before"]) / 1e9
        return True, (f"Live TON curve buy ~${usd:.2f} ({q['spent'] / 1e9:,.3f} TON, {got:,.0f} coins, 1% curve fee included). "
                      f"Confirmed by your wallet balance.\nWallet: {link}")
    if r["landed"]:
        return False, ("Curve buy sent but no coins arrived yet. If the price moved past your slippage the curve sends your TON "
                       f"back (minus a little gas). Check before retrying:\n{link}")
    return False, f"Curve buy sent but not confirmed within a minute. Check before retrying:\n{link}"


def _curve_sell(ci: dict, jetton: str, pct: int, seed64: bytes, slip_bps: int) -> tuple[bool, str]:
    tc = _tc()
    st, why = _curve_open(tc, ci["curve"])
    if st is None:
        return False, why.replace("Try again", "Sell again")
    owner, jw, bal = _run_retry(lambda: _wallet_coin(seed64, jetton))
    if bal <= 0:
        return False, "Nothing to sell: this wallet holds 0 of that coin."
    amount = bal if pct >= 100 else bal * pct // 100
    if amount <= 0:
        return False, "Sell size rounds to 0."
    q = _in_thread(tc.quote_sell, ci["curve"], amount)
    if q["ton_out"] <= 0:
        return False, "The curve would not quote this sell. Nothing sent."
    min_out = q["ton_out"] * (10_000 - max(1, min(5000, slip_bps))) // 10_000
    msg = tc.build_sell_message(ci["curve"], owner, jw, amount, min_out)
    r = _run_retry(lambda: _curve_tx(seed64, jetton, msg, "down"))
    if not r.get("sent"):
        return False, r.get("error", "not sent") + " Nothing sent."
    link = f"https://tonviewer.com/{r['addr']}"
    if r["filled"]:
        return True, f"Sold {pct}% on the Ferzan curve · ~{q['ton_out'] / 1e9:,.4f} TON expected (after the 1% fee)\nWallet: {link}"
    return False, ("Curve sell sent but your coins haven't left yet. If the price moved past your slippage the coins come back "
                   f"to you. Check before retrying:\n{link}")


async def _buy_v2(seed64: bytes, jetton: str, nano: int, slip_bps: int):
    import asyncio

    from pytoniq import LiteBalancer, WalletV4R2

    provider = LiteBalancer.from_mainnet_config(trust_level=2)
    await provider.start_up()
    try:
        wallet = await WalletV4R2.from_private_key(provider, seed64)
        slip = f"{max(100, int(slip_bps or 1000)) / 10000:.4f}"
        p = await asyncio.to_thread(_sdk_swap, "buy", wallet.address.to_str(), jetton, nano, slip)
        if p.get("error"):
            return wallet.address.to_str(), "STON.fi v2 buy failed: " + p["error"] + ". Nothing sent.", 0.0
        h, seqno = await _send_sdk_params(provider, wallet, p)
        ok = await _await_seqno(wallet, seqno)
        return wallet.address.to_str(), ok, int(p["value"]) / 1e9
    finally:
        await provider.close_all()


def buy_ton(jetton: str, usd: float, secret: str | None = None, slip_bps: int = 0) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live buys OFF."
    jetton = (jetton or "").strip()
    if not jetton.startswith(("EQ", "UQ", "kQ")):
        return False, "Need a TON jetton address (EQ… / UQ…)."
    if not (secret or "").strip():
        return False, "No TON/Solana key on file. Open /wallet first."
    usd = min(max(1.0, float(usd)), max_usd())
    try:
        from price_fetcher import get_price_usd

        px = float(get_price_usd("the-open-network") or 0)
    except Exception:
        px = 0.0
    if px <= 0:
        # Never guess the TON price: a wrong guess over-spends the user.
        return False, "TON price feed is down — buy skipped, nothing sent."
    nano = max(10**7, int((usd / px) * 10**9))

    ci = curve_info(jetton)
    if ci and not ci.get("graduated"):  # a Ferzan curve coin still on its curve
        try:
            seed64 = _ton_keypair_bytes(secret)
        except Exception as exc:
            return False, f"TON key: {exc}"
        try:
            return _curve_buy(ci, jetton, nano, usd, seed64, slip_bps or CURVE_SLIP_BPS)
        except Exception as exc:
            return False, f"TON curve buy failed: {_friendly_err(exc)} Check your wallet before retrying."

    sim = simulate(jetton, str(nano))
    if sim.get("error"):
        return False, "STON.fi quote failed: " + str(sim["error"])[:180]

    router = sim.get("router") or {}
    router_addr = sim.get("router_address") or router.get("address")
    pton_wallet = router.get("pton_wallet_address") or sim.get("offer_jetton_wallet")
    ask_wallet = sim.get("ask_jetton_wallet")
    try:
        min_out = int(sim.get("min_ask_units") or 0)
    except (TypeError, ValueError):
        min_out = 0
    if not router_addr or not pton_wallet or not ask_wallet or min_out <= 0:
        return False, "STON.fi quote missing router/pool fields — pool may not exist for this token."
    if str(router.get("major_version", 1)) != "1":
        # v2 router: the official SDK builds the message, we sign it.
        try:
            seed64 = _ton_keypair_bytes(secret)
        except Exception as exc:
            return False, f"TON key: {exc}"
        try:
            addr, confirmed, spent = _run_retry(lambda: _buy_v2(seed64, jetton, nano, slip_bps))
        except Exception as exc:
            return False, f"TON send failed: {_friendly_err(exc)}"
        if isinstance(confirmed, str):
            return False, confirmed
        if not confirmed:
            return False, (f"TON buy sent (~{spent:.3f} TON) but not confirmed on-chain within 60s — "
                           f"check before retrying: https://tonviewer.com/{addr}")
        return True, f"~{spent:.3f} TON in (incl. gas) · STON.fi v2 swap confirmed by wallet\nWallet: https://tonviewer.com/{addr}"

    gas = sim.get("gas_params") or {}
    try:
        fwd = int(gas.get("forward_gas") or 0) or TON_TO_JETTON_FWD_NANO
    except (TypeError, ValueError):
        fwd = TON_TO_JETTON_FWD_NANO
    fwd = min(max(fwd, MIN_GAS_RESERVE_NANO), MAX_GAS_RESERVE_NANO)

    try:
        seed64 = _ton_keypair_bytes(secret)
    except Exception as exc:
        return False, f"TON key: {exc}"

    try:
        addr, _msg_hash, confirmed = _run_retry(
            lambda: _swap_ton_to_jetton(seed64, router_addr, pton_wallet, ask_wallet, nano, min_out, fwd)
        )
    except Exception as exc:
        return False, f"TON send failed: {_friendly_err(exc)}"

    spent = (nano + fwd) / 1e9
    if not confirmed:
        return False, (
            f"TON buy sent (~{spent:.3f} TON) but not confirmed on-chain within 60s — "
            f"check before retrying: https://tonviewer.com/{addr}"
        )
    return (
        True,
        f"~{spent:.3f} TON in (incl. gas) · STON.fi swap confirmed by wallet\n"
        f"Wallet: https://tonviewer.com/{addr}",
    )


def sell_ton(jetton: str, secret: str | None = None, pct: int = 100, slip: str = "0.10") -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live sells OFF."
    jetton = (jetton or "").strip()
    if not jetton.startswith(("EQ", "UQ", "kQ")):
        return False, "Need a TON jetton address (EQ… / UQ…)."
    pct = max(1, min(100, int(pct)))
    try:
        seed64 = _ton_keypair_bytes(secret or "")
    except Exception as exc:
        return False, f"TON key: {exc}"
    ci = curve_info(jetton)
    if ci and not ci.get("graduated"):
        try:
            return _curve_sell(ci, jetton, pct, seed64, int(float(slip) * 10_000) or CURVE_SLIP_BPS)
        except Exception as exc:
            return False, f"TON curve sell failed: {_friendly_err(exc)} Check your wallet before retrying."
    try:
        return _run_retry(lambda: _swap_jetton_to_ton(seed64, jetton, pct, slip))
    except Exception as exc:
        return False, f"TON sell failed: {_friendly_err(exc)}"


def status_text() -> str:
    try:
        import pytoniq  # noqa: F401

        extra = "pytoniq ON — buy + sell live"
    except Exception:
        extra = "pytoniq missing — pip install pytoniq pytoniq-core"
    return f"TON STON.fi quotes live. Send: {extra}"


async def _send_ton_native(seed64: bytes, dest: str, nano: int | None, reserve_nano: int) -> tuple[bool | None, str]:
    from pytoniq import LiteBalancer, WalletV4R2
    from pytoniq_core import Address, begin_cell

    to = Address(dest)
    provider = LiteBalancer.from_mainnet_config(trust_level=2)
    await provider.start_up()
    try:
        wallet = await WalletV4R2.from_private_key(provider, seed64)
        if to.to_str() == wallet.address.to_str():
            return False, "That's this wallet's own address."
        bal = await _ton_balance(provider, wallet.address)
        value = bal - int(reserve_nano) if nano is None else int(nano)
        if value <= 0 or value + int(reserve_nano) > bal:
            return False, f"Not enough TON (balance {bal / 1e9:.4f}; ~{reserve_nano / 1e9:.2f} stays for fees)."
        # Respect the address's own bounce flag (UQ.. = non-bounceable, the
        # right choice for a fresh wallet; EQ.. bounces back if undeployed).
        body = begin_cell().end_cell()
        kw = {"bounce": bool(to.is_bounceable)}
        try:  # pure object build (no network): does this pytoniq take bounce=?
            wallet.create_wallet_internal_message(destination=to, value=value, body=body, **kw)
        except TypeError:
            kw = {}  # older API: bounce follows its own default
        try:
            h, seqno = await _send_one(provider, wallet, to, value, body, **kw)
        except Exception as exc:
            # Could have failed before or after the broadcast: never say "not sent".
            return None, (
                f"TON send didn't confirm cleanly ({str(exc)[:100]}). It MAY have gone out — check "
                f"https://tonviewer.com/{wallet.address.to_str()} before trying again."
            )
        landed = await _await_seqno(wallet, seqno)
        link = f"https://tonviewer.com/transaction/{h}"
        head = f"{value / 1e9:.4f} TON → {dest[:6]}…{dest[-4:]}"
        if landed:
            return True, f"Sent {head}\n{link}"
        return None, f"Sent, not confirmed yet: {head}\nCheck https://tonviewer.com/{wallet.address.to_str()} before retrying."
    finally:
        try:
            await provider.close_all()
        except Exception:
            pass


def send_ton_native(secret: str, dest: str, nano: int | None, reserve_nano: int = 30_000_000) -> tuple[bool | None, str]:
    """Plain TON transfer out of the user's wallet. nano=None = send all
    except `reserve_nano` (fees + a deploy if this wallet was never used)."""
    try:
        seed64 = _ton_keypair_bytes(secret)
    except Exception as exc:
        return False, f"TON key problem, nothing sent: {exc}"
    try:
        return _run_retry(lambda: _send_ton_native(seed64, dest, nano, reserve_nano))
    except Exception as exc:
        return None, f"TON send ended with an error ({str(exc)[:100]}). Check your TON wallet before retrying."
