"""Quote-only route check for cross-chain buying.

Asks Relay / deBridge for a ~$9 quote for every source -> destination pair among our chains
and prints which ones work. SENDS NOTHING, uses no keys and no wallet database (dummy addresses).

  python xbuy_routes.py
"""
from __future__ import annotations

import os
import sys
import types
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

for line in open("/opt/ferzan/.env") if os.path.exists("/opt/ferzan/.env") else []:
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        k = k.strip()
        if k in {"RELAY_API_KEY", "ARC_RPC_URL", "ARC_CHAIN_ID"} or k.startswith(("DLN_ID_", "DLN_NATIVE_")):
            os.environ.setdefault(k, v.strip().strip('"').strip("'"))

SOL = "Gh9ZwEmdLJ8DscKNTkTqPbNwLNNBjuSzaG9Vp2KGtKJr"  # ordinary address; burn addresses are refused by deBridge compliance
EVM = "0x5B38Da6a701c568545dCfcB03FcB875f56beddC4"
uw = types.ModuleType("user_wallets")
uw.ensure = lambda uid: {"sol_pub": SOL, "evm_pub": EVM}
uw.secrets = lambda uid: ("unused", "0x" + "11" * 32)
sys.modules["user_wallets"] = uw

import bridge  # noqa: E402
import crossbuy  # noqa: E402

AMT = {"sol": "0.06", "eth": "0.003", "base": "0.003", "bsc": "0.015", "hood": "0.003", "arc": "10"}


def one(pair):
    s, d = pair
    try:
        pack = bridge.quote(1, s, d, AMT[s])
        return s, d, True, pack["via"], bridge.est_out(pack), ""
    except Exception as exc:
        return s, d, False, "", 0.0, str(exc).replace("\n", " ")[:110]


def main() -> None:
    dsts = [c for c, i in crossbuy.CHAINS.items() if i["dst"]]
    pairs = [(s, d) for s in AMT for d in dsts if s != d]
    with ThreadPoolExecutor(max_workers=6) as pool:
        res = {(s, d): (ok, via, out, err) for s, d, ok, via, out, err in pool.map(one, pairs)}
    print("route check (quotes only, nothing sent). R = Relay, D = deBridge, . = no route, - = same chain")
    print("from\\to  " + "".join(f"{d:>6}" for d in dsts))
    for s in AMT:
        cells = []
        for d in dsts:
            if s == d:
                cells.append(f"{'-':>6}")
                continue
            ok, via, _o, _e = res[(s, d)]
            cells.append(f"{('R' if via == 'relay' else 'D') if ok else '.':>6}")
        print(f"{s:8} " + "".join(cells))
    print("\nsources we can sign: sol eth base bsc hood arc. tron is a destination only; ton has no bridge route yet.")
    bad = sorted({(s, d, e) for (s, d), (ok, _v, _o, e) in res.items() if not ok})
    if bad:
        print(f"\n{len(bad)} pairs without a route (first 12):")
        for s, d, e in bad[:12]:
            print(f"  {s}->{d}: {e}")
    print(f"\n{sum(1 for v in res.values() if v[0])}/{len(res)} pairs quoted OK")


if __name__ == "__main__":
    main()
