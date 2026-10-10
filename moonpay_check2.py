# Read-only: sell/suspended flags for the codes Ferzan uses, plus Polygon candidates. Changes nothing.
import json, urllib.request
req = urllib.request.Request("https://api.moonpay.com/v3/currencies", headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
d = json.load(urllib.request.urlopen(req, timeout=25))
by = {c.get("code"): c for c in d if isinstance(c, dict)}
print("== sell / suspended for codes in use ==")
for code in "sol usdc_sol eth usdc eth_base usdc_base bnb_bsc eth_arbitrum usdc_arbitrum eth_optimism avax_cchain ton trx usdc_arc eth_robinhood eth_linea mon_mon hype_hyperevm usdc_hyperevm".split():
    c = by.get(code)
    print("%-16s %s" % (code, ("sell=%s suspended=%s" % (bool(c.get("isSellSupported")), bool(c.get("isSuspended")))) if c else "MISSING"))
print("== Polygon / POL candidates ==")
for c in d:
    if isinstance(c, dict) and c.get("type") == "crypto":
        md = c.get("metadata") or {}
        if "polygon" in str(md.get("networkCode")).lower() or str(c.get("code")).lower().startswith(("pol", "matic")):
            print("%-18s | %-28s | net=%s" % (c.get("code"), c.get("name"), md.get("networkCode")))
