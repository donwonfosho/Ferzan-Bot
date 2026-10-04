"""
TON bonding-curve launches (keeper-assisted graduation).

The curve is contracts/ton/ferzan_curve.fc (compiled to ton-code/ferzan_curve.b64, pinned by hash below).
One TonConnect request from the creator's own wallet carries four messages:
  1. deploy the curve contract (state_init) with a small TON buffer for its own gas
  2. deploy the jetton minter and mint the WHOLE supply straight to the curve's own coin wallet
  3. change_admin -> nobody, so the supply can never grow
  4. the Ferzan launch fee to PLATFORM_TREASURY_TON (memo FERZAN_LAUNCH:<request id>)
The curve has no owner and no admin message. When it fills, anyone may call graduate(): the raised TON and the
remaining coins go to the fixed keeper (TON_KEEPER_ADDRESS, baked into the curve at launch), which opens the
STON.fi pool and burns the LP.

Trading before the mint has landed would strand a buyer's TON, so: start_time is set ~2 minutes ahead, and the site
only lists a curve after verify_curve_launch() has proven the coin wallet holds the full supply.

  python ton_curve.py golden     # prints the layout fingerprints the TypeScript tests compare against
"""
from __future__ import annotations

import base64
import os
import time

import ton_launch as tl
from ton_launch import WALLET_CODE_HASH, _addr, _b64, _code, _content, _fmt, testnet

CURVE_CODE_HASH = "000832f5c48c56c407ed080d88820fc7c8618817502e13bc5d0acf9e410e969c"

OP_BUY = 0x62757931
OP_SELL = 0x73656C6C
OP_GRADUATE = 0x67726164
OP_TRANSFER = 0x0F8A7EA5

CURVE_TON = 50_000_000        # 0.05 TON buffer the curve keeps for its own gas
BUY_OVERHEAD = 120_000_000    # 0.12 TON on top of the amount spent (matches the contract)
SELL_ATTACH = 300_000_000     # 0.3 TON attached to the coin transfer (most of it returns)
SELL_FORWARD = 200_000_000    # 0.2 TON forwarded to the curve with the sell instruction
GRAD_ATTACH = 300_000_000     # 0.3 TON to call graduate()
START_DELAY_S = 120

_LAUNCH_SUPPLY_DECIMALS = 9


def min_grad_nano() -> int:
    return int(float(os.environ.get("TON_CURVE_MIN_GRAD_TON") or "2000") * 1e9)


def keeper_address() -> str:
    k = (os.environ.get("TON_KEEPER_ADDRESS") or "").strip()
    if not k:
        raise ValueError("Set TON_KEEPER_ADDRESS (the graduation keeper wallet) before TON curve launches.")
    return k


def _curve_code():
    return _code("ferzan_curve", CURVE_CODE_HASH)


def _cfg(treasury, creator, minter, keeper, wallet_code):
    from pytoniq_core import begin_cell

    keeper_cell = begin_cell().store_address(keeper).store_ref(wallet_code).end_cell()
    return (begin_cell().store_address(treasury).store_address(creator).store_address(minter)
            .store_ref(keeper_cell).end_cell())


def curve_data(treasury, creator, minter, keeper, wallet_code, grad_nano: int, supply_raw: int, start: int):
    """Initial storage. Layout MUST match load_main()/load_cfg() in ferzan_curve.fc (checked by the golden test)."""
    from pytoniq_core import begin_cell

    return (begin_cell().store_coins(int(grad_nano)).store_coins(int(supply_raw)).store_coins(0).store_coins(0)
            .store_uint(int(start), 32).store_uint(0, 1).store_uint(0, 1)
            .store_ref(_cfg(treasury, creator, minter, keeper, wallet_code)).end_cell())


def state_init(code, data):
    from pytoniq_core import begin_cell

    # StateInit: split_depth=None, special=None, code=^Cell, data=^Cell, library=None
    return begin_cell().store_uint(0b00110, 5).store_ref(code).store_ref(data).end_cell()


def _addr_of(si):
    from pytoniq_core import Address

    return Address(f"0:{si.hash.hex()}")


def build_curve_launch_tx(request_id: str, creator_address: str, supply_raw: int, metadata_url: str,
                          grad_nano: int, start_delay_s: int = START_DELAY_S) -> tl.TonLaunchTx:
    from pytoniq_core import Address, begin_cell

    treasury_s = (os.environ.get("PLATFORM_TREASURY_TON") or "").strip()
    if not treasury_s:
        raise ValueError("Set PLATFORM_TREASURY_TON before TON launches.")
    if not (0 < int(supply_raw) < 2**120):
        raise ValueError("supply out of range")
    if int(grad_nano) < min_grad_nano():
        raise ValueError(f"The graduation target must be at least {min_grad_nano() / 1e9:g} TON.")
    creator, treasury, keeper = _addr(creator_address), _addr(treasury_s), _addr(keeper_address())
    minter_code = _code("jetton-minter", tl.MINTER_CODE_HASH)
    wallet_code = _code("jetton-wallet", WALLET_CODE_HASH)
    curve_code = _curve_code()

    minter_data = (begin_cell().store_coins(0).store_address(creator)
                   .store_ref(_content(metadata_url)).store_ref(wallet_code).end_cell())
    minter_si = state_init(minter_code, minter_data)
    minter = Address(f"0:{minter_si.hash.hex()}")

    start = int(time.time()) + int(start_delay_s)
    cdata = curve_data(treasury, creator, minter, keeper, wallet_code, grad_nano, supply_raw, start)
    curve_si = state_init(curve_code, cdata)
    curve = _addr_of(curve_si)

    qid = int(time.time())
    internal_transfer = (begin_cell().store_uint(tl.OP_INTERNAL_TRANSFER, 32).store_uint(qid, 64)
                         .store_coins(int(supply_raw))
                         .store_address(None)
                         .store_address(creator)       # excess TON goes back to the creator
                         .store_coins(0).store_bit(0).end_cell())
    mint = (begin_cell().store_uint(tl.OP_MINT, 32).store_uint(qid, 64).store_address(curve)   # supply -> the CURVE
            .store_coins(tl.WALLET_TON).store_ref(internal_transfer).end_cell())
    drop_admin = (begin_cell().store_uint(tl.OP_CHANGE_ADMIN, 32).store_uint(qid + 1, 64)
                  .store_address(None).end_cell())
    fee_body = begin_cell().store_uint(0, 32).store_bytes(f"FERZAN_LAUNCH:{request_id}".encode()).end_cell()

    messages = [
        {"address": _fmt(curve, bounceable=False), "amount": str(CURVE_TON), "stateInit": _b64(curve_si)},
        {"address": _fmt(minter, bounceable=False), "amount": str(tl.DEPLOY_TON), "stateInit": _b64(minter_si), "payload": _b64(mint)},
        {"address": _fmt(minter, bounceable=True), "amount": str(tl.ADMIN_TON), "payload": _b64(drop_admin)},
    ]
    fee = tl.launch_fee_nano()
    if fee > 0:
        messages.append({"address": _fmt(treasury, bounceable=False), "amount": str(fee), "payload": _b64(fee_body)})
    total = CURVE_TON + tl.DEPLOY_TON + tl.ADMIN_TON + fee
    out = tl.TonLaunchTx(
        minter=_fmt(minter), minter_raw=minter.to_str(is_user_friendly=False), messages=messages,
        valid_until=int(time.time()) + 600, network="-3" if testnet() else "-239",
        note=(f"Your wallet sends {total / 1e9:.2f} TON; most of the {(CURVE_TON + tl.DEPLOY_TON + tl.ADMIN_TON) / 1e9:.2f} TON "
              f"for the contracts stays as their gas buffer or comes back as change. Trading opens about 2 minutes after launch."),
        cells={"curve": curve, "curve_si": curve_si, "minter": minter, "start": start, "grad": int(grad_nano)},
    )
    out.curve = _fmt(curve)  # type: ignore[attr-defined]
    return out


# --------------------------------------------------------------------------- chain reads
async def _run(address: str, method: str, stack=None):
    from pytoniq import LiteBalancer

    provider = LiteBalancer.from_testnet_config(trust_level=2) if testnet() else LiteBalancer.from_mainnet_config(trust_level=2)
    await provider.start_up()
    try:
        return await provider.run_get_method(address=_addr(address), method=method, stack=stack or [])
    finally:
        await provider.close_all()


def _run_sync(address: str, method: str, stack=None):
    """Chain read: liteserver first, then the toncenter/tonapi backup (see ton_launch.run_get_method)."""
    return tl.run_get_method(address, method, stack)


def _same(a, b) -> bool:
    try:
        return _addr(a).to_str(is_user_friendly=False) == _addr(b).to_str(is_user_friendly=False)
    except Exception:
        return False


def curve_state(curve: str) -> dict:
    """{grad, supply, real, sold, start, complete, graduated} straight from the contract."""
    st = _run_sync(curve, "get_curve")
    grad, supply, real, sold, start, complete, graduated = [int(x) for x in st[:7]]
    return {"grad": grad, "supply": supply, "real": real, "sold": sold, "start": start,
            "complete": bool(complete), "graduated": bool(graduated)}


def quote_buy(curve: str, spend_nano: int) -> dict:
    """spend_nano is the amount to spend, NOT counting the 0.12 TON overhead."""
    st = _run_sync(curve, "get_quote_buy", [int(spend_nano)])
    out, gross, refund, fee = [int(x) for x in st[:4]]
    return {"tokens_out": out, "spent": gross, "refund": refund, "fee": fee}


def quote_sell(curve: str, tokens_in: int) -> dict:
    st = _run_sync(curve, "get_quote_sell", [int(tokens_in)])
    out, fee = [int(x) for x in st[:2]]
    return {"ton_out": out, "fee": fee}


def verify_curve_launch(curve: str, minter: str, supply_raw: int, creator: str, wait_s: int = 120) -> dict:
    """Proves the launch is what we built: right code, right owner-less wiring, the WHOLE supply sits in the curve's own
    coin wallet, and nobody can mint more."""
    deadline = time.time() + wait_s
    last = "not deployed yet"
    while time.time() < deadline:
        try:
            cs = curve_state(curve)
            if cs["supply"] != int(supply_raw) or cs["real"] != 0 or cs["sold"] != 0:
                return {"ok": False, "error": f"curve state is not a fresh curve for this supply: {cs}"}
            if not _same(_run_sync(curve, "get_minter")[0].load_address(), minter):
                return {"ok": False, "error": "the curve points at a different coin"}
            if not _same(_run_sync(curve, "get_creator")[0].load_address(), creator):
                return {"ok": False, "error": "the curve pays a different creator"}
            if not _same(_run_sync(curve, "get_keeper")[0].load_address(), keeper_address()):
                return {"ok": False, "error": "the curve graduates to a different keeper"}
            jd = _run_sync(minter, "get_jetton_data")
            total = int(jd[0])
            try:
                admin = jd[2].load_address()
            except Exception:
                admin = "unreadable"
            if total != int(supply_raw) or admin is not None:
                last = f"coin supply {total} (want {supply_raw}), admin {'still set' if admin else 'none'}"
            else:
                cw = _run_sync(curve, "get_wallet")[0].load_address().to_str(is_user_friendly=True)
                bal = int(_run_sync(cw, "get_wallet_data")[0])
                if bal == int(supply_raw):
                    return {"ok": True, "curve_state": cs, "coin_wallet": cw}
                last = f"coin wallet holds {bal} of {supply_raw} (mint not landed yet)"
        except Exception as e:
            last = str(e)[:140]
        time.sleep(6)
    return {"ok": False, "error": last}


def coin_wallet(minter: str, owner: str) -> str:
    """The owner's wallet contract for this coin (asked from the coin's minter)."""
    from pytoniq_core import begin_cell

    sl = begin_cell().store_address(_addr(owner)).end_cell().begin_parse()
    return _run_sync(minter, "get_wallet_address", [sl])[0].load_address().to_str(is_user_friendly=True)


def coin_balance(wallet: str) -> int:
    """Coins held by a coin-wallet contract; 0 when it isn't deployed yet."""
    try:
        return int(_run_sync(wallet, "get_wallet_data")[0])
    except Exception:
        return 0


# --------------------------------------------------------------------------- trade messages
def build_buy_message(curve: str, spend_nano: int, min_out: int = 0, referrer: str | None = None) -> dict:
    """TonConnect / signer message for a buy: TON to the curve with the buy instruction."""
    from pytoniq_core import begin_cell

    if spend_nano < 10_000_000:
        raise ValueError("Minimum buy is 0.01 TON.")
    ref = _addr(referrer) if referrer else None
    body = (begin_cell().store_uint(OP_BUY, 32).store_uint(int(time.time()), 64).store_uint(int(min_out), 128)
            .store_address(ref).end_cell())
    return {"address": _fmt(_addr(curve), bounceable=True), "amount": str(int(spend_nano) + BUY_OVERHEAD), "payload": _b64(body)}


def build_sell_message(curve: str, owner: str, owner_coin_wallet: str, amount_raw: int, min_out: int = 0,
                       referrer: str | None = None) -> dict:
    """A jetton transfer from the seller's coin wallet to the curve, with the sell instruction as forward payload."""
    from pytoniq_core import begin_cell

    payload = (begin_cell().store_uint(OP_SELL, 32).store_uint(int(min_out), 128)
               .store_address(_addr(referrer) if referrer else None).end_cell())
    body = (begin_cell().store_uint(OP_TRANSFER, 32).store_uint(int(time.time()), 64).store_coins(int(amount_raw))
            .store_address(_addr(curve)).store_address(_addr(owner)).store_bit(0)
            .store_coins(SELL_FORWARD).store_bit(1).store_ref(payload).end_cell())
    return {"address": _fmt(_addr(owner_coin_wallet), bounceable=True), "amount": str(SELL_ATTACH), "payload": _b64(body)}


def build_graduate_message(curve: str) -> dict:
    from pytoniq_core import begin_cell

    body = begin_cell().store_uint(OP_GRADUATE, 32).store_uint(int(time.time()), 64).end_cell()
    return {"address": _fmt(_addr(curve), bounceable=True), "amount": str(GRAD_ATTACH), "payload": _b64(body)}


# --------------------------------------------------------------------------- golden fingerprints
def golden() -> dict:
    """Fixed inputs -> hashes. The TypeScript tests rebuild the same things with @ton/core and must agree."""
    from pytoniq_core import Address

    t, c, m, k = (Address("0:" + ch * 64) for ch in "1234")
    wallet_code = _code("jetton-wallet", WALLET_CODE_HASH)
    code = _curve_code()
    data = curve_data(t, c, m, k, wallet_code, 100 * 10**9, 10**18, 1_700_000_000)
    si = state_init(code, data)
    return {"curve_code_hash": code.hash.hex(), "data_hash": data.hash.hex(), "state_init_hash": si.hash.hex(),
            "address_hash": si.hash.hex()}


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "golden":
        print(json.dumps(golden()))
    else:
        sys.exit(__doc__)
