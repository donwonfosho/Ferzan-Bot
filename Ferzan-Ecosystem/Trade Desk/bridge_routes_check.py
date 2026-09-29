"""Read-only: which desk chains can Relay / deBridge actually bridge, and which have a swap route. Sends nothing."""
import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import bridge
from chains import CHAINS as DESK, ZEROX_LIVE

try:
    relay = {int(c["id"]) for c in requests.get("https://api.relay.link/chains", timeout=15).json().get("chains", []) if c.get("id")}
except Exception as e:
    print("relay list failed:", str(e)[:80])
    relay = set()
dln = bridge._dln_live()
print(f"{'chain':8} {'id':>8}  relay  debridge  swap")
for k in DESK:
    if k in ("ton",):
        print(f"{k:8} {'-':>8}  no     no        STON.fi   (no bridge route)")
        continue
    cid = bridge.chain_id(k) if k in bridge.CHAINS else 0
    r = "yes" if cid in relay else "no"
    d = "yes" if (k in ("sol", "trx") or bridge.dln_chain(k)) else "no"
    sw = {"sol": "Jupiter", "trx": "SunSwap"}.get(k) or ("0x" if k in ZEROX_LIVE else "none")
    print(f"{k:8} {cid or '-':>8}  {r:6} {d:9} {sw}")
