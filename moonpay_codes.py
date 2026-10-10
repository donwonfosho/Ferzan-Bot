# Read-only: lists MoonPay currency codes for Ferzan's chains. Changes nothing, prints no secrets.
import json, os, re, sys, urllib.request
URL = "https://api.moonpay.com/v3/currencies"
def get(key=""):
    u = URL + (("?apiKey=" + key) if key else "")
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.load(r)
def envkey():
    try:
        for ln in open("/opt/ferzan/.env"):
            m = re.match(r"(MOONPAY_PK|MOONPAY_KEY)=(.+)", ln.strip())
            if m: return m.group(2).strip().strip("\"'")
    except OSError: pass
    return ""
try:
    d = get(); how = "no key"
except Exception as e:
    k = envkey()
    if not k: sys.exit("MoonPay refused the keyless request (%s) and no key is set in the env file." % str(e)[:60])
    try: d = get(k); how = "env key (value not shown)"
    except Exception as e2: sys.exit("MoonPay request failed: %s" % str(e2)[:80])
print("fetched %d currencies (%s)" % (len(d), how))
byc = {c.get("code"): c for c in d if isinstance(c, dict)}
print("\n== codes Ferzan already uses ==")
for code in "sol usdc_sol eth usdc eth_base usdc_base bnb bnb_bsc eth_arbitrum usdc_arbitrum eth_optimism matic_polygon avax_cchain ton trx".split():
    c = byc.get(code)
    print("%-16s %s" % (code, ("OK  " + str(c.get("name")) + ("  [suspended]" if c.get("isSuspended") else "")) if c else "MISSING"))
print("\n== candidates for bridge-only chains ==")
pat = re.compile(r"arc|robin|hood|monad|sonic|hyper|\bink\b|linea|stable|pulse|\bmon\b|\bhype\b", re.I)
n = 0
for c in d:
    if not isinstance(c, dict) or c.get("type") != "crypto": continue
    md = c.get("metadata") or {}
    blob = " ".join(str(x) for x in (c.get("code"), c.get("name"), md.get("networkCode")))
    if pat.search(blob):
        n += 1
        print("%-18s | %-34s | net=%s chainId=%s%s%s" % (c.get("code"), c.get("name"), md.get("networkCode"), md.get("chainId"),
              " SUSPENDED" if c.get("isSuspended") else "", " sell" if c.get("isSellSupported") else ""))
print("(%d matches)" % n)
