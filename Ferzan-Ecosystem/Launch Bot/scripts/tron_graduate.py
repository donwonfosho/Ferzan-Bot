"""
Graduation keeper for one Ferzan Tron curve:  python scripts/tron_graduate.py <curve T-address>

Calls graduate() from the deployer wallet (/opt/ferzan/dbc-keys/evm-deployer.json) once the curve is full.
graduate() opens the SunSwap pool, burns the LP and pays the caller the graduation reward (300 TRX on
mainnet), which covers the ~230 TRX of energy. It sends nothing unless the curve is complete, not yet
graduated, made by our factory, and the wallet holds enough TRX. Prints one JSON line.
"""
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "Trade Desk"))
import tron_launch as tl  # noqa: E402

os.environ.setdefault("TRONGRID_URL", tl._grid())
if tl._setting("TRONGRID_API_KEY"):
    os.environ.setdefault("TRONGRID_API_KEY", tl._setting("TRONGRID_API_KEY"))
import tron_signer as ts  # noqa: E402

NEED_SUN = int(float(tl._setting("TRON_KEEPER_MIN_TRX") or 260) * 1e6)
FEE_LIMIT = 600_000_000


def out(**kw):
    print(json.dumps(kw))
    sys.exit(0)


def q(curve_hex: str, sig: str) -> int:
    w = ts._const(curve_hex, curve_hex, sig, "")
    if not w:
        out(ok=False, error=f"could not read {sig}")
    return w[0]


def main():
    if len(sys.argv) != 2:
        out(ok=False, error="usage: tron_graduate.py <curve>")
    curve = sys.argv[1].strip()
    try:
        curve_hex, fac_hex = ts._to_hex(curve), ts._to_hex(tl.curve_factory())
    except Exception:
        out(ok=False, error="bad address or no TRON_CURVE_FACTORY")
    if "41" + f"{q(curve_hex, 'factory()'):040x}" != fac_hex:
        out(ok=False, error="not a Ferzan curve")
    if q(curve_hex, "graduated()"):
        out(ok=False, error="already graduated")
    if not q(curve_hex, "complete()"):
        out(ok=False, error="not full yet")
    key = json.loads(Path("/opt/ferzan/dbc-keys/evm-deployer.json").read_text())["key"].replace("0x", "")
    addr, _ = ts.evm_key_to_tron(key)
    owner = ts._to_hex(addr)
    bal = ts._trx_balance(owner)
    if bal < NEED_SUN:
        out(ok=False, error="low_balance", address=addr, balance_trx=bal / 1e6, need_trx=NEED_SUN / 1e6)
    built = ts._post("/wallet/triggersmartcontract", {
        "owner_address": owner, "contract_address": curve_hex, "function_selector": "graduate()", "parameter": "",
        "call_value": 0, "fee_limit": FEE_LIMIT, "visible": False})
    result, link, burned = ts._send_and_wait(built, key, timeout_s=90)
    if result == "SUCCESS":
        out(ok=True, link=link, burned_trx=burned, address=addr)
    if q(curve_hex, "graduated()"):  # someone else graduated it first
        out(ok=False, error="already graduated", link=link)
    out(ok=False, error=f"graduate {result}", link=link, burned_trx=burned)


if __name__ == "__main__":
    main()
