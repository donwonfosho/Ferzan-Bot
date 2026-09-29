"""Cross-chain buy: when a user taps Buy on a token but the chain's gas coin is short,
offer to fund it from another chain's balance (bridge), wait for it to land, then buy.

Nothing moves until the user confirms. The bridge is a normal desk bridge signed with
the user's own desk wallet (bridge.py: Relay, deBridge). Then the existing per-chain buy
path runs, so curves, slippage caps and cost basis all behave exactly as a normal buy.

The planning maths is pure (plan / need_native) so it is unit-testable; the I/O helpers
(balances, run, wait_arrival) import the signers lazily.
"""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("crossbuy")

MARK = "⁣[[XBUY]]"  # invisible marker: _done() turns it into the confirm buttons

# chain -> label/unit/kind, whether we can SIGN a bridge out of it (src), whether a bridge can reach it (dst)
CHAINS = {
    "sol": {"name": "Solana", "unit": "SOL", "src": True, "dst": True},
    "eth": {"name": "Ethereum", "unit": "ETH", "src": True, "dst": True},
    "base": {"name": "Base", "unit": "ETH", "src": True, "dst": True},
    "bsc": {"name": "BNB", "unit": "BNB", "src": True, "dst": True},
    "hood": {"name": "Robinhood Chain", "unit": "ETH", "src": True, "dst": True},
    "arc": {"name": "Arc", "unit": "USDC", "src": True, "dst": True},
    "trx": {"name": "Tron", "unit": "TRX", "src": False, "dst": True},
    "ton": {"name": "TON", "unit": "TON", "src": False, "dst": False},  # no bridge route yet
}
# native coin that must stay behind for gas on the chain we buy on / leave from
RESERVE = {"sol": 0.012, "eth": 0.0015, "base": 0.0004, "bsc": 0.0025, "hood": 0.0004, "arc": 1.0, "trx": 40.0, "ton": 0.35}
# the bridge's own flat fee, paid in the source chain's native coin (deBridge table)
SRC_FEE = {"sol": 0.015, "eth": 0.001, "base": 0.001, "bsc": 0.005, "hood": 0.001, "arc": 1.0}
PAD = 1.05  # extra on the top-up for fees / price drift
MIN_BRIDGE_USD = 3.0
MIN_OUT_RATIO = 0.90  # abort if the live quote delivers under 90% of the shortfall
PENDING_TTL = 300
ARRIVAL_WAIT = 420

INFLIGHT: set[int] = set()  # users with a bridge running: no second offer until it finishes
_PENDING: dict[int, dict] = {}
_LOCK = threading.Lock()
_tl = threading.local()


def enabled() -> bool:
    return (os.getenv("CROSSBUY", "1").strip().lower() not in {"0", "false", "no", "off"})


def allow(flag: bool) -> None:
    """Set on the worker thread by a MANUAL buy tap only (auto-buys, snipes and DCA never offer a bridge)."""
    _tl.allow = bool(flag)


def allowed() -> bool:
    return bool(getattr(_tl, "allow", False)) and enabled()


def chain_of(desk_chain: str) -> str:
    return desk_chain if desk_chain in CHAINS else ""


# ---------------------------------------------------------------- pure planning
def need_native(dst: str, usd: float, prices: dict) -> float | None:
    px = prices.get(dst)
    if not px or px <= 0:
        return None
    return usd / px + RESERVE[dst]


def plan(dst: str, usd: float, bal: dict, prices: dict, cap_usd: float = 500.0) -> dict | None:
    """None if the wallet on `dst` can already cover the buy, or no way to top it up.
    Otherwise {src, dst, amt (source native), short_usd, spare_usd}."""
    info = CHAINS.get(dst)
    if not info or not info["dst"]:
        return None
    have = bal.get(dst)
    need = need_native(dst, usd, prices)
    if have is None or need is None:
        return None
    if have >= need:
        return None
    px_d = prices[dst]
    short_usd = (need - have) * px_d * PAD
    short_usd = max(short_usd, MIN_BRIDGE_USD)
    if short_usd > cap_usd:
        return None
    best = None
    for s, si in CHAINS.items():
        if s == dst or not si["src"]:
            continue
        b, px = bal.get(s), prices.get(s)
        if b is None or not px or px <= 0:
            continue
        spare_usd = (b - RESERVE[s] - SRC_FEE.get(s, 0.0)) * px
        if spare_usd < short_usd * 1.02:
            continue
        if best is None or spare_usd > best["spare_usd"]:
            amt = short_usd / px
            best = {"src": s, "dst": dst, "amt": amt, "short_usd": short_usd, "spare_usd": spare_usd,
                    "want_out": need - have}
    if best:
        best["amt"] = float(f"{best['amt']:.6g}") if best["amt"] < 1 else round(best["amt"] + 5e-7, 6)
    return best


def offer_text(p: dict, usd: float, sym: str, est_out: float | None) -> str:
    s, d = CHAINS[p["src"]], CHAINS[p["dst"]]
    got = f"~{est_out:.6g} {d['unit']}" if est_out else "the rest of what you need"
    return (
        f"⚠️ Not enough {d['unit']} on {d['name']} for a ${usd:,.2f} buy of {sym}.\n\n"
        f"🌉 Fund it from {s['name']}? I'll bridge {p['amt']:.6g} {s['unit']} (about ${p['short_usd']:,.2f}) "
        f"to your {d['name']} wallet, wait for it to land, then buy.\n"
        f"You receive {got}. Bridge fee is taken from the amount; nothing moves until you tap Confirm. "
        f"Offer expires in {PENDING_TTL // 60} min. (No buttons? send /xbuy)"
        f"{MARK}"
    )


def check(uid: int, dst: str, usd: float, prices_fn, cap_usd: float = 500.0) -> tuple[dict | None, dict, str]:
    """(plan or None, prices, note). Reads ONLY the destination balance first, so a funded wallet
    (the normal case) costs one cheap read; the other chains are read only when it is short."""
    from concurrent.futures import ThreadPoolExecutor

    info = CHAINS.get(dst)
    if not info or not info["dst"]:
        return None, {}, ""
    have = native_balance(uid, dst)
    if have is None:
        return None, {}, ""
    prices = dict(prices_fn() or {})
    prices.setdefault("arc", 1.0)
    if need_native(dst, usd, prices) is None or have >= need_native(dst, usd, prices):
        return None, prices, ""
    if uid in INFLIGHT:
        return None, prices, "busy"
    srcs = [c for c, i in CHAINS.items() if i["src"] and c != dst]
    with ThreadPoolExecutor(max_workers=len(srcs) or 1) as pool:
        got = list(pool.map(lambda c: native_balance(uid, c), srcs))
    bal = dict(zip(srcs, got))
    bal[dst] = have
    return plan(dst, usd, bal, prices, cap_usd), prices, ""


# ------------------------------------------------------------------- pending
def put_pending(uid: int, item: dict) -> None:
    with _LOCK:
        item["exp"] = time.time() + PENDING_TTL
        _PENDING[uid] = item


def take_pending(uid: int) -> dict | None:
    with _LOCK:
        item = _PENDING.pop(uid, None)
    if not item or item["exp"] < time.time():
        return None
    return item


def drop_pending(uid: int) -> None:
    with _LOCK:
        _PENDING.pop(uid, None)


# ------------------------------------------------------------------- live I/O
def native_balance(uid: int, cid: str) -> float | None:
    """Native balance of the user's active desk wallet on `cid`, or None if it can't be read."""
    try:
        import user_wallets

        w = user_wallets.ensure(uid)
        if cid == "sol":
            import signer

            return signer.sol_balance_lamports(w["sol_pub"]) / 1e9
        if cid == "trx":
            import tron_signer

            _sol, evm = user_wallets.secrets(uid)
            addr = tron_signer.evm_key_to_tron(evm.replace("0x", "").replace("0X", ""))[0]
            return tron_signer._trx_balance(tron_signer._to_hex(addr)) / 1e6
        if cid == "ton":
            import ton_signer

            sol_secret, _evm = user_wallets.secrets(uid)
            return float(ton_signer.address_and_balance(sol_secret)[1])
        import evm_signer

        amt, _sym = evm_signer.native_balance(cid, w["evm_pub"])
        return float(amt)
    except Exception:
        log.exception("balance read failed for %s", cid)
        return None


def all_balances(uid: int) -> dict:
    out = {}
    for cid in CHAINS:
        out[cid] = native_balance(uid, cid)
    return out


def run(uid: int, p: dict) -> tuple[bool, str, dict]:
    """Quote again (fresh), sanity-check the delivery, then sign and send the bridge.
    Blocking. Returns (ok, message, info)."""
    import bridge

    pack = bridge.quote(uid, p["src"], p["dst"], f"{p['amt']:.8f}".rstrip("0").rstrip("."))
    got = bridge.est_out(pack)
    want = p["want_out"]
    if got and want and got < want * MIN_OUT_RATIO:
        return False, (
            f"Bridge quote is too thin right now ({got:.6g} {CHAINS[p['dst']]['unit']} "
            f"vs the ~{want:.6g} needed). Nothing was sent. Try again in a minute."
        ), {}
    res = bridge.execute(uid, pack)
    text = res["text"] if isinstance(res, dict) else str(res)
    info = {"order_id": (res or {}).get("order_id", "") if isinstance(res, dict) else "", "got": got}
    return True, text, info


def wait_arrival(uid: int, dst: str, before: float, want: float, timeout: int = ARRIVAL_WAIT, step: int = 6) -> bool:
    """Poll the destination balance until it has grown by ~90% of what we expect. Blocking."""
    target = before + max(want, 0.0) * 0.9
    end = time.time() + timeout
    while time.time() < end:
        now = native_balance(uid, dst)
        if now is not None and now >= target:
            return True
        time.sleep(step)
    return False
