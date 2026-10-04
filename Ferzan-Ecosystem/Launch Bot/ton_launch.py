"""
TON coin launches: a standard TEP-74 jetton (the discoverable minter from @ton-community/assets-sdk),
deployed and signed by the creator's own wallet through TonConnect in the Mini App.

One TonConnect request carries three messages from the creator's wallet:
  1. deploy the jetton minter (state_init) with the creator as admin + mint the full supply to them
  2. change_admin -> nobody, so the supply can never grow (fixed-supply, like the other chains)
  3. the Ferzan launch fee to PLATFORM_TREASURY_TON, with the memo FERZAN_LAUNCH:<request id>
Messages between the same two accounts arrive in order, so the mint always lands before the admin drop.

The contract code is loaded from TON_CODE_DIR (default /opt/ferzan/ton-code) and must match the pinned
hashes below, so a swapped file can never be deployed.
Network: TON_NETWORK=testnet uses TON's test network (addresses, TonConnect and verification).
"""
from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

MINTER_CODE_HASH = "0571976c63ec1b7550230a2609dbedb36e1b64ef8d022a16b34ea57063185b2f"
WALLET_CODE_HASH = "a760d629d5343e76d045017d9dc216fc8a307a8377815feb2b0a5c490e733486"
CODE_DIR = Path(os.environ.get("TON_CODE_DIR") or "/opt/ferzan/ton-code")

OP_MINT = 21
OP_INTERNAL_TRANSFER = 0x178D4519
OP_CHANGE_ADMIN = 3

DEPLOY_TON = 250_000_000      # 0.25 TON to the minter (storage + mint gas; the excess returns to the creator)
WALLET_TON = 60_000_000       # of that, 0.06 TON forwarded to deploy the creator's jetton wallet
ADMIN_TON = 50_000_000        # 0.05 TON for the admin drop (the excess returns)


def testnet() -> bool:
    return (os.environ.get("TON_NETWORK") or "").strip().lower() == "testnet"


def launch_fee_nano() -> int:
    return int(os.environ.get("LAUNCH_FEE_NANOTON") or "300000000")  # 0.3 TON by default


@dataclass
class TonLaunchTx:
    minter: str                      # user-friendly minter (coin) address
    minter_raw: str                  # 0:hex
    messages: list = field(default_factory=list)  # TonConnect messages
    valid_until: int = 0
    network: str = "-239"            # TonConnect chain id: -239 mainnet, -3 testnet
    note: str = ""
    cells: dict = field(default_factory=dict, repr=False)  # raw cells, for the server-side testnet run


def _code(name: str, want: str):
    from pytoniq_core import Cell

    p = CODE_DIR / f"{name}.b64"
    if not p.exists():
        raise ValueError(f"TON contract code missing ({p}); run the TON setup step first.")
    cell = Cell.one_from_boc(base64.b64decode(p.read_text().strip()))
    if cell.hash.hex() != want:
        raise ValueError(f"TON contract code at {p} does not match the pinned hash; refusing to launch.")
    return cell


def _content(url: str):
    """Off-chain metadata: 0x01 + URL (one cell holds 126 bytes after the prefix)."""
    from pytoniq_core import begin_cell

    raw = url.encode()
    if len(raw) > 126:
        raise ValueError("metadata URL too long for one cell")
    return begin_cell().store_uint(1, 8).store_bytes(raw).end_cell()


def _addr(a: str):
    from pytoniq_core import Address

    return Address(a)


def _fmt(address, bounceable: bool = True) -> str:
    return address.to_str(is_user_friendly=True, is_bounceable=bounceable, is_test_only=testnet())


def _b64(cell) -> str:
    return base64.b64encode(cell.to_boc()).decode()


def build_unsigned_launch_tx(request_id: str, creator_address: str, supply_raw: int, metadata_url: str) -> TonLaunchTx:
    from pytoniq_core import Address, begin_cell

    treasury = (os.environ.get("PLATFORM_TREASURY_TON") or "").strip()
    if not treasury:
        raise ValueError("Set PLATFORM_TREASURY_TON before TON launches.")
    if not (0 < int(supply_raw) < 2**120):
        raise ValueError("supply out of range")
    creator = _addr(creator_address)
    minter_code = _code("jetton-minter", MINTER_CODE_HASH)
    wallet_code = _code("jetton-wallet", WALLET_CODE_HASH)

    data = (begin_cell().store_coins(0).store_address(creator)
            .store_ref(_content(metadata_url)).store_ref(wallet_code).end_cell())
    # StateInit: split_depth=None, special=None, code=^Cell, data=^Cell, library=None
    state_init = begin_cell().store_uint(0b00110, 5).store_ref(minter_code).store_ref(data).end_cell()
    minter = Address(f"0:{state_init.hash.hex()}")
    qid = int(time.time())

    internal_transfer = (begin_cell().store_uint(OP_INTERNAL_TRANSFER, 32).store_uint(qid, 64)
                         .store_coins(int(supply_raw))
                         .store_address(None)          # from: nobody (fresh mint)
                         .store_address(creator)       # excess TON goes back to the creator
                         .store_coins(0)               # no forward notification
                         .store_bit(0)                 # empty forward payload
                         .end_cell())
    mint = (begin_cell().store_uint(OP_MINT, 32).store_uint(qid, 64).store_address(creator)
            .store_coins(WALLET_TON).store_ref(internal_transfer).end_cell())
    drop_admin = (begin_cell().store_uint(OP_CHANGE_ADMIN, 32).store_uint(qid + 1, 64)
                  .store_address(None).end_cell())
    memo = f"FERZAN_LAUNCH:{request_id}".encode()
    fee_body = begin_cell().store_uint(0, 32).store_bytes(memo).end_cell()

    messages = [
        {"address": _fmt(minter, bounceable=False), "amount": str(DEPLOY_TON), "stateInit": _b64(state_init), "payload": _b64(mint)},
        {"address": _fmt(minter, bounceable=True), "amount": str(ADMIN_TON), "payload": _b64(drop_admin)},
    ]
    fee = launch_fee_nano()
    if fee > 0:
        messages.append({"address": _fmt(_addr(treasury), bounceable=False), "amount": str(fee), "payload": _b64(fee_body)})
    total = DEPLOY_TON + ADMIN_TON + fee
    return TonLaunchTx(
        minter=_fmt(minter), minter_raw=minter.to_str(is_user_friendly=False), messages=messages,
        valid_until=int(time.time()) + 600, network="-3" if testnet() else "-239",
        cells={"code": minter_code, "data": data, "mint": mint, "drop_admin": drop_admin, "fee": fee_body,
               "minter": minter, "treasury": _addr(treasury), "fee_nano": fee},
        note=f"Your wallet sends {total / 1e9:.2f} TON; part of the {(DEPLOY_TON + ADMIN_TON) / 1e9:.2f} TON for the contract comes back as change.",
    )


# ------------------------------------------------------------ chain reads with a backup
def _http_urls() -> tuple[str, str]:
    if testnet():
        return "https://testnet.toncenter.com", "https://testnet.tonapi.io"
    return "https://toncenter.com", "https://tonapi.io"


def _to_slice(data: str):
    """A cell/slice returned over HTTP (base64 or hex BOC) -> a pytoniq Slice, so .load_address() works like on a liteserver."""
    from pytoniq_core import Cell

    raw = None
    if len(data) % 2 == 0:
        try:
            raw = bytes.fromhex(data)
        except ValueError:
            raw = None
    if raw is None:
        raw = base64.b64decode(data)
    return Cell.one_from_boc(raw).begin_parse()


def _num(v) -> int:
    return int(str(v), 16) if str(v).lower().lstrip("-").startswith("0x") else int(str(v))


def _once_more_on_429(call):
    """The free API tiers allow about one request a second; on 'too many requests' wait a moment and ask once more."""
    r = call()
    if getattr(r, "status_code", 0) == 429:
        time.sleep(1.3)
        r = call()
    return r


def _center_stack(args, boc: bool) -> list:
    out = []
    for a in args:
        if isinstance(a, int):
            out.append({"type": "num", "value": str(a)})
        elif boc:  # the same address as a one-address slice cell, base64 BOC
            from pytoniq_core import Address, begin_cell

            out.append({"type": "slice", "value": base64.b64encode(
                begin_cell().store_address(Address(a)).end_cell().to_boc()).decode()})
        else:
            out.append({"type": "slice", "value": a})
    return out


def _http_get_method(address: str, method: str, stack=None) -> list:
    """Runs a get-method through toncenter, then tonapi (free keys TONCENTER_API_KEY / TONAPI_KEY are used when set).
    Returns the same shapes a liteserver gives: ints, and Slices for addresses/cells. Arguments are numbers or address
    strings; anything else raises so the caller keeps the liteserver error."""
    import requests

    args = list(stack or [])
    if not all((isinstance(a, int) and not isinstance(a, bool)) or isinstance(a, str) for a in args):
        raise ValueError("backup read only supports number and address arguments")
    center, tonapi = _http_urls()
    errs = []
    key = (os.environ.get("TONCENTER_API_KEY") or "").strip()
    has_addr = any(isinstance(a, str) for a in args)
    for boc in ((False, True) if has_addr else (False,)):  # toncenter takes an address string or a BOC slice: try both
        try:
            r = _once_more_on_429(lambda: requests.post(
                f"{center}/api/v3/runGetMethod", timeout=10, headers={"X-API-Key": key} if key else {},
                json={"address": address, "method": method, "stack": _center_stack(args, boc)}))
            j = r.json() if r.status_code == 200 else {}
            if r.status_code == 200 and int(j.get("exit_code", 1)) == 0:
                out = []
                for it in j.get("stack") or []:
                    t = it.get("type")
                    out.append(_num(it["value"]) if t == "num" else (None if t == "null" else _to_slice(str(it["value"]))))
                return out
            errs.append(f"toncenter {r.status_code} exit {j.get('exit_code')}")
        except Exception as e:  # noqa: BLE001
            errs.append(f"toncenter {type(e).__name__}")
    tkey = (os.environ.get("TONAPI_KEY") or "").strip()
    try:
        r = _once_more_on_429(lambda: requests.get(
            f"{tonapi}/v2/blockchain/accounts/{address}/methods/{method}", timeout=10,
            params=[("args", str(a)) for a in args],
            headers={"Accept": "application/json", **({"Authorization": f"Bearer {tkey}"} if tkey else {})}))
        j = r.json() if r.status_code == 200 else {}
        if r.status_code == 200 and j.get("success") and int(j.get("exit_code", 1)) == 0:
            out = []
            for it in j.get("stack") or []:
                t = it.get("type")
                if t == "num":
                    out.append(_num(it["num"]))
                elif t in ("cell", "slice"):
                    out.append(_to_slice(str(it[t])))
                else:
                    out.append(None)
            return out
        errs.append(f"tonapi {r.status_code}")
    except Exception as e:  # noqa: BLE001
        errs.append(f"tonapi {type(e).__name__}")
    raise RuntimeError("backup TON read failed: " + "; ".join(errs))


def _is_node_trouble(exc) -> bool:
    """True when a liteserver read failed because of the NODE (lagging, unreachable, slow), not because of the contract."""
    import asyncio

    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError, OSError)):
        return True
    low = str(exc).lower()
    return any(k in low for k in ("651", "cannot load block", "liteserver", "lite server", "out of sync", "not in db",
                                  "timeout", "timed out", "connection", "no alive", "unreachable"))


_LITE_BAD_UNTIL = [0.0]


def run_get_method(address: str, method: str, stack=None, tries: int = 2, http_args=None):
    """Get-method read: the liteserver first (two tries, 12s each; public liteservers sometimes answer 'cannot load block'
    651), then the HTTP backup. Returns the same list either way. A failure that is the contract's own (not the node's)
    is not retried and never switches the liteserver off."""
    import asyncio

    async def _lite():
        from pytoniq import LiteBalancer

        provider = LiteBalancer.from_testnet_config(trust_level=2) if testnet() else LiteBalancer.from_mainnet_config(trust_level=2)
        await provider.start_up()
        try:
            return await provider.run_get_method(address=_addr(address), method=method, stack=stack or [])
        finally:
            try:
                await asyncio.wait_for(provider.close_all(), 3)  # a stuck node must not hold the read open
            except Exception:  # noqa: BLE001
                pass

    last = None
    if time.time() >= _LITE_BAD_UNTIL[0]:  # a node that just failed is skipped for a minute, so one slow node can't stall every read
        for i in range(tries):
            try:
                return asyncio.run(asyncio.wait_for(_lite(), 12))
            except Exception as e:  # noqa: BLE001
                last = e
                if not _is_node_trouble(e):
                    break  # the contract itself said no: asking another node will not change that
                if i < tries - 1:
                    time.sleep(1.0 + i)
        else:
            _LITE_BAD_UNTIL[0] = time.time() + 60
    else:
        last = RuntimeError("liteserver skipped after a recent failure")
    try:
        return _http_get_method(address, method, stack if http_args is None else http_args)
    except Exception as e2:  # noqa: BLE001
        raise RuntimeError(f"{str(last)[:100]} | {str(e2)[:100]}") from last


async def _jetton_data(minter: str):
    from pytoniq import LiteBalancer

    provider = LiteBalancer.from_testnet_config(trust_level=2) if testnet() else LiteBalancer.from_mainnet_config(trust_level=2)
    await provider.start_up()
    try:
        return await provider.run_get_method(address=_addr(minter), method="get_jetton_data", stack=[])
    finally:
        await provider.close_all()


def verify_launch(minter: str, supply_raw: int, wait_s: int = 90) -> dict:
    """Proves the coin exists: total supply matches and nobody can mint more (admin dropped)."""
    deadline = time.time() + wait_s
    last = "not deployed yet"
    while time.time() < deadline:
        try:
            st = run_get_method(minter, "get_jetton_data")
            total = int(st[0])
            admin = None
            try:
                admin = st[2].load_address()
            except Exception:
                admin = "unreadable"
            if total == int(supply_raw) and admin is None:
                return {"ok": True, "total_supply": total}
            last = f"supply {total} (want {supply_raw}), admin {'still set' if admin else 'none'}"
        except Exception as e:
            last = str(e)[:120]
        time.sleep(6)
    return {"ok": False, "error": last}
