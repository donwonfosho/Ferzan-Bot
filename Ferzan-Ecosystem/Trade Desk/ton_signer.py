"""TON live buy + sell via STON.fi v1 (router SWAP op 0x25938561), signed with WalletV4R2."""

from __future__ import annotations

import os

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
    return {"Accept": "application/json"}


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
        return wallet.address.to_str(), nano
    finally:
        await provider.close_all()


def address_and_balance(secret: str) -> tuple[str, float]:
    """(TON address, TON balance) for the wallet derived from `secret` —
    same ed25519 key as the user's Solana wallet, different derived address."""
    seed64 = _ton_keypair_bytes(secret)
    addr, nano = _run_async(_address_and_balance(seed64))
    return addr, nano / 1e9


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
    try:
        return int(await wallet.get_seqno())
    except Exception as exc:
        if await _is_uninitialized(provider, wallet):
            return 0
        raise RuntimeError(f"couldn't read wallet seqno, nothing sent: {exc}") from exc


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


async def _send_one(provider, wallet, destination, value: int, body) -> tuple[str, int]:
    """Sign + broadcast one internal message from `wallet`. Handles a
    never-used wallet (no contract deployed yet): seqno 0 + state_init in the
    same external message deploys the wallet AND executes the transfer —
    wallet.transfer() can't do that (get_seqno fails on an undeployed
    contract and it never attaches state_init). Returns the external
    message hash (hex) for an explorer link, and the seqno it was signed
    with (pass that to _await_seqno)."""
    import time as _t

    msg = wallet.create_wallet_internal_message(destination=destination, value=value, body=body)
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
    await provider.raw_send_message(cell.to_boc())
    return cell.hash.hex(), seqno


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


async def _jetton_wallet_and_balance(provider, jetton_master: str, owner) -> tuple[object, int]:
    """TEP-74: ask the jetton master for owner's jetton-wallet address, then
    read that wallet's balance. Balance 0 if the jetton wallet isn't deployed."""
    from pytoniq_core import Address, begin_cell

    res = await provider.run_get_method(
        address=Address(jetton_master),
        method="get_wallet_address",
        stack=[begin_cell().store_address(owner).end_cell().begin_parse()],
    )
    jw = res[0].load_address()
    try:
        data = await provider.run_get_method(address=jw, method="get_wallet_data", stack=[])
        bal = int(data[0])
    except Exception:
        bal = 0
    return jw, bal


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
    raw, owner = _run_async(_holding(_ton_keypair_bytes(secret), jetton))
    return raw / (10 ** _jetton_decimals(jetton)), owner


async def _swap_jetton_to_ton(seed64: bytes, jetton: str, pct: int, slip: str) -> tuple[bool, str]:
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
            return False, "STON.fi routed this token to a non-v1 pool — not supported yet, nothing sent."
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


def buy_ton(jetton: str, usd: float, secret: str | None = None) -> tuple[bool, str]:
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
        # This code builds v1 messages only; a v2 router needs a different payload.
        return False, "STON.fi routed this token to a non-v1 pool — not supported yet, nothing sent."

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
        addr, _msg_hash, confirmed = _run_async(
            _swap_ton_to_jetton(seed64, router_addr, pton_wallet, ask_wallet, nano, min_out, fwd)
        )
    except Exception as exc:
        return False, f"TON send failed: {exc}"

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
    try:
        return _run_async(_swap_jetton_to_ton(seed64, jetton, pct, slip))
    except Exception as exc:
        return False, f"TON sell failed: {exc}"


def status_text() -> str:
    try:
        import pytoniq  # noqa: F401

        extra = "pytoniq ON — buy + sell live"
    except Exception:
        extra = "pytoniq missing — pip install pytoniq pytoniq-core"
    return f"TON STON.fi quotes live. Send: {extra}"
