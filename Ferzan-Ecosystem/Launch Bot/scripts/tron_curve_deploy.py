"""
Deploy and test the Ferzan Tron bonding curve (FerzanTronCurveToken + FerzanTronCurve masters + FerzanTronCurveFactory).

  python -u scripts/tron_curve_deploy.py nile plan    # compile, find SunSwap + prove its pair hash, costs; sends nothing
  python -u scripts/tron_curve_deploy.py nile send    # deploy the two masters + the factory on Nile
  python -u scripts/tron_curve_deploy.py nile test    # launch a tiny curve, buy, sell, fill it, graduate, check the pool
  python -u scripts/tron_curve_deploy.py mainnet plan|send

Before graduation the coin only moves through its curve, so nobody can seed the SunSwap pool early; the pool is
created by graduate() (the caller is paid the graduation reward for that energy).
Deployer: /opt/ferzan/dbc-keys/evm-deployer.json. Addresses go to /opt/ferzan/dbc-keys/tron-factories.json.
Mainnet settings (in /opt/ferzan/.env): PLATFORM_TREASURY_TRX, TRON_CURVE_FEE_TRX (default 5),
TRON_CURVE_GRAD_REWARD_TRX (default 300), TRON_CURVE_MIN_GRAD_TRX (default 5000).
"""
import json
import subprocess
import sys
import time
from pathlib import Path

NET = sys.argv[1] if len(sys.argv) > 1 else ""
MODE = sys.argv[2] if len(sys.argv) > 2 else "plan"
if NET not in ("nile", "mainnet") or MODE not in ("plan", "send", "test"):
    sys.exit("usage: tron_curve_deploy.py nile|mainnet plan|send|test")
if NET == "mainnet" and MODE == "test":
    sys.exit("The live test runs on Nile only (a mainnet graduation costs real TRX).")

import os  # noqa: E402

os.environ["TRONGRID_URL"] = "https://nile.trongrid.io" if NET == "nile" else "https://api.trongrid.io"
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE.parent / "Trade Desk"))
import tron_signer as ts  # noqa: E402

SOLC = Path("/opt/ferzan/evm-tools/solc-0.8.24")
KEYS = Path("/opt/ferzan/dbc-keys")
RECORD = KEYS / "tron-factories.json"
EXPLORER = "https://nile.tronscan.org/#" if NET == "nile" else "https://tronscan.org/#"
ROUTER = {"nile": "TMn1qrmYUMSTXo9babrJLzepKZoPC7M6Sy", "mainnet": "TNJVzGqKBWkJxJB5XYSqGAwUTV15U24pPq"}[NET]


def env_file(path="/opt/ferzan/.env"):
    out = {}
    try:
        for line in open(path):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def keccak(b: bytes) -> bytes:
    from eth_hash.auto import keccak as k
    return k(b)


def hex41(a: str) -> str:
    return ts._to_hex(a)


def b58(h: str) -> str:
    raw = bytes.fromhex(h if h.startswith("41") else "41" + h)
    return ts._b58encode(raw + ts._check(raw))


def word_addr(res: str) -> str:
    return "41" + res[-40:] if res and len(res) >= 40 else ""


def const(contract: str, sig: str, params: str = "", owner: str = "") -> str:
    out = ts._post("/wallet/triggerconstantcontract", {
        "owner_address": hex41(owner or contract), "contract_address": hex41(contract),
        "function_selector": sig, "parameter": params, "visible": False})
    return (out.get("constant_result") or [""])[0]


def compile_all():
    src = HERE / "contracts" / "tron" / "FerzanTronCurveFactory.sol"
    res = subprocess.run([str(SOLC), "--optimize", "--optimize-runs", "200", "--evm-version", "istanbul",
                          "--base-path", str(HERE), "--combined-json", "abi,bin", str(src)], capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit("ABORT: compile failed\n" + res.stderr[-2000:])
    data = json.loads(res.stdout)["contracts"]
    out = {}
    for name in ("FerzanTronCurveToken", "FerzanTronCurve", "FerzanTronCurveFactory"):
        key = next(k for k in data if k.endswith(":" + name))
        abi = data[key]["abi"]
        out[name] = (abi if isinstance(abi, str) else json.dumps(abi), data[key]["bin"])
    return out


def deployer():
    key = json.loads((KEYS / "evm-deployer.json").read_text())["key"].replace("0x", "")
    addr, _ = ts.evm_key_to_tron(key)
    return addr, key


def balance(addr: str) -> int:
    return int(ts._post("/wallet/getaccount", {"address": hex41(addr)}).get("balance") or 0)


def wait_info(txid: str, timeout=120) -> dict:
    t = time.time()
    while time.time() - t < timeout:
        info = ts._post("/wallet/gettransactioninfobyid", {"value": txid})
        if info.get("id"):
            return info
        time.sleep(3)
    return {}


def send(tx: dict, key: str, what: str) -> dict:
    if "txID" not in tx:
        sys.exit(f"ABORT: TronGrid did not build {what}: {str(tx)[:300]}")
    ok, msg = ts._broadcast(tx, key)
    if not ok:
        sys.exit(f"ABORT: {what} broadcast failed: {msg}")
    info = wait_info(tx["txID"])
    r = info.get("receipt") or {}
    if r.get("result") != "SUCCESS":
        sys.exit(f"ABORT: {what} failed on-chain: {r.get('result')} {EXPLORER}/transaction/{tx['txID']}")
    print(f"  {what}: energy {r.get('energy_usage_total') or 0:,}, burned {(info.get('fee') or 0) / 1e6:.2f} TRX")
    return info


def deploy(name, abi, bytecode, params, owner, key):
    tx = ts._post("/wallet/deploycontract", {
        "owner_address": hex41(owner), "abi": abi, "bytecode": bytecode, "parameter": params,
        "fee_limit": 1_500_000_000, "call_value": 0, "consume_user_resource_percent": 100,
        "origin_energy_limit": 10_000_000, "name": name})
    info = send(tx, key, f"deploy {name}")
    addr = b58(info["contract_address"])
    print(f"  {name}: {addr}  {EXPLORER}/contract/{addr}")
    return addr


def call(contract, sig, params, owner, key, value=0, fee_limit=300_000_000, what=""):
    built = ts._post("/wallet/triggersmartcontract", {
        "owner_address": hex41(owner), "contract_address": hex41(contract), "function_selector": sig,
        "parameter": params, "call_value": int(value), "fee_limit": fee_limit, "visible": False})
    return send(built.get("transaction") or {}, key, what or sig.split("(")[0])


def can_transfer(owner: str, token: str) -> bool:
    """Simulates sending 1 unit of the coin to an unrelated wallet (sends nothing)."""
    from eth_abi import encode

    r = ts._post("/wallet/triggerconstantcontract", {
        "owner_address": hex41(owner), "contract_address": hex41(token), "function_selector": "transfer(address,uint256)",
        "parameter": encode(["address", "uint256"], ["0x" + "11" * 20, 1]).hex(), "visible": False})
    failed = ((r.get("transaction") or {}).get("ret") or [{}])[0].get("ret") == "FAILED"
    return bool((r.get("result") or {}).get("result")) and not failed


def main():
    from eth_abi import encode

    rec = json.loads(RECORD.read_text()) if RECORD.exists() else {}
    net = rec.setdefault(NET, {})
    owner, key = deployer()
    env = env_file()
    fee_trx = float(env.get("TRON_CURVE_FEE_TRX") or (1 if NET == "nile" else 5))
    reward_trx = float(env.get("TRON_CURVE_GRAD_REWARD_TRX") or (5 if NET == "nile" else 300))
    min_grad_trx = float(env.get("TRON_CURVE_MIN_GRAD_TRX") or (10 if NET == "nile" else 5000))
    treasury = owner if NET == "nile" else (env.get("PLATFORM_TREASURY_TRX") or "").strip()
    if not (treasury.startswith("T") and len(treasury) == 34):
        sys.exit("ABORT: set PLATFORM_TREASURY_TRX first")
    bal = balance(owner)
    dexf = b58(word_addr(const(ROUTER, "factory()")))
    wtrx = b58(word_addr(const(ROUTER, "WETH()")))
    print(f"Network      : {NET}")
    print(f"Deployer     : {owner}  balance {bal / 1e6:,.2f} TRX")
    print(f"Treasury     : {treasury}")
    print(f"SunSwap V2   : router {ROUTER}  factory {dexf}  WTRX {wtrx}")
    print(f"Settings     : launch fee {fee_trx:g} TRX, graduation reward {reward_trx:g} TRX, min graduation {min_grad_trx:g} TRX")

    if MODE in ("plan", "send"):
        pairs = int(const(dexf, "allPairsLength()") or "0", 16)
        if pairs <= 0 or not wtrx.startswith("T"):
            sys.exit("ABORT: that SunSwap factory/WTRX does not answer like SunSwap V2")
        print(f"SunSwap check: {pairs:,} pools on this factory")
        built = compile_all()
        for n, (_a, b) in built.items():
            print(f"Bytecode     : {n} {len(b) // 2:,} bytes")
        if net.get("curve_factory_v2"):
            print(f"Already deployed: curve factory v2 {net['curve_factory_v2']}")
            return
        need = 260_000_000 if net.get("curve_token_master") else 320_000_000  # v2 reuses the coin master
        if bal < need:
            where = "from the Nile faucet (https://nileex.io/join/getJoinPage)" if NET == "nile" else "on Tron"
            print(f"\nNEXT: get about {need // 1_000_000} TRX {where} to {owner}, then run plan again.")
            return
        if MODE == "plan":
            print("\nPlan OK - nothing sent. Run with 'send' to deploy.")
            return
        if not net.get("curve_token_master"):  # the coin master is unchanged in v2, so an existing one is reused
            net["curve_token_master"] = deploy("FerzanTronCurveToken", *built["FerzanTronCurveToken"], "", owner, key)
            RECORD.write_text(json.dumps(rec, indent=1))
        if not net.get("curve_master_v2"):  # shared settings are immutables of the curve master in v2
            cparams = encode(["address", "address", "address", "uint256"], [
                "0x" + hex41(treasury)[2:], "0x" + hex41(wtrx)[2:], "0x" + hex41(dexf)[2:], int(reward_trx * 1e6)]).hex()
            net["curve_master_v2"] = deploy("FerzanTronCurve", *built["FerzanTronCurve"], cparams, owner, key)
            RECORD.write_text(json.dumps(rec, indent=1))
        params = encode(["address", "address", "uint256", "uint256"], [
            "0x" + hex41(net["curve_token_master"])[2:], "0x" + hex41(net["curve_master_v2"])[2:], int(fee_trx * 1e6),
            int(min_grad_trx * 1e6)]).hex()
        net["curve_factory_v2"] = deploy("FerzanTronCurveFactory", *built["FerzanTronCurveFactory"], params, owner, key)
        RECORD.write_text(json.dumps(rec, indent=1))
        f2 = net["curve_factory_v2"]
        print(f"  factory reads: treasury {b58(word_addr(const(f2, 'platformTreasury()')))}, "
              f"reward {int(const(f2, 'gradRewardSun()') or '0', 16) / 1e6:g} TRX, fee {int(const(f2, 'launchFeeSun()') or '0', 16) / 1e6:g} TRX, "
              f"min graduation {int(const(f2, 'minGradTarget()') or '0', 16) / 1e6:g} TRX")
        print(f"\nTRON_CURVE_FACTORY_{'NILE' if NET == 'nile' else 'MAINNET'}={f2}")
        return

    # ---------------- live test on Nile: launch -> buy -> sell -> fill -> graduate -> check the pool
    fac = net.get("curve_factory_v2")
    if not fac:
        sys.exit("ABORT: deploy the v2 curve factory first (nile send)")
    fee_sun = int(const(fac, "launchFeeSun()") or "0", 16)
    grad = 30_000_000  # 30 TRX target keeps the test cheap
    supply = 1_000_000_000 * 10**6
    print("\nLaunch:")
    info = call(fac, "launch(string,string,uint256,uint256,uint256,uint256)",
                encode(["string", "string", "uint256", "uint256", "uint256", "uint256"],
                       ["Ferzan Curve Test", "FZCT", supply, grad, 0, 0]).hex(), owner, key, value=fee_sun,
                fee_limit=800_000_000, what="launch")
    topic = keccak(b"CurveLaunched(address,address,address)").hex()
    curve = token = ""
    for lg in info.get("log") or []:
        t = lg.get("topics") or []
        if t and t[0] == topic:
            curve, token = b58("41" + t[1][-40:]), b58("41" + t[2][-40:])
    if not curve:
        sys.exit("ABORT: no CurveLaunched event")
    print(f"  curve {curve}  token {token}")
    zero = "0x" + "00" * 20
    print("Trades:")
    call(curve, "buy(uint256,address)", encode(["uint256", "address"], [1, zero]).hex(), owner, key, value=8_000_000,
         fee_limit=200_000_000, what="buy 8 TRX")
    held = int(const(token, "balanceOf(address)", encode(["address"], ["0x" + hex41(owner)[2:]]).hex()) or "0", 16)
    call(token, "approve(address,uint256)", encode(["address", "uint256"], ["0x" + hex41(curve)[2:], 2**256 - 1]).hex(),
         owner, key, fee_limit=100_000_000, what="approve")
    call(curve, "sell(uint256,uint256,address)", encode(["uint256", "uint256", "address"], [held // 2, 1, zero]).hex(),
         owner, key, fee_limit=200_000_000, what="sell half")
    blocked = not can_transfer(owner, token)  # before graduation the coin must only move through the curve
    print(f"  wallet-to-wallet transfer refused before graduation: {blocked}")
    call(curve, "buy(uint256,address)", encode(["uint256", "address"], [1, zero]).hex(), owner, key, value=40_000_000,
         fee_limit=200_000_000, what="buy 40 TRX (fills the curve, refund)")
    complete = int(const(curve, "complete()") or "0", 16) == 1
    x = int(const(curve, "ethReserve()") or "0", 16)
    y = int(const(curve, "tokenReserve()") or "0", 16)
    print(f"  complete: {complete}")
    before = balance(owner)
    print("Graduate:")
    g = call(curve, "graduate()", "", owner, key, fee_limit=1_500_000_000, what="graduate (creates the SunSwap pool)")
    pair = b58(word_addr(const(dexf, "getPair(address,address)", encode(["address", "address"], [
        "0x" + hex41(token)[2:], "0x" + hex41(wtrx)[2:]]).hex())))
    pool_curve = b58(word_addr(const(curve, "pool()")))
    opened = can_transfer(owner, token)
    r = const(pair, "getReserves()")
    t0 = word_addr(const(pair, "token0()"))
    r0, r1 = int(r[:64], 16), int(r[64:128], 16)
    trx_r, tok_r = (r1, r0) if t0 == hex41(token) else (r0, r1)
    lp_dead = int(const(pair, "balanceOf(address)", encode(["address"], ["0x" + "00" * 18 + "dead"]).hex()) or "0", 16)
    pool_price, curve_price = trx_r / tok_r if tok_r else 0, x / y if y else 0
    reward = balance(owner) - before + (g.get("fee") or 0)
    ok = (complete and blocked and opened and pair == pool_curve and trx_r > 0 and tok_r > 0 and lp_dead > 0
          and abs(pool_price - curve_price) / curve_price < 0.001)
    print(f"  pool {pair}: {trx_r / 1e6:,.4f} TRX + {tok_r / 1e6:,.0f} coins, LP burned {lp_dead > 0}, "
          f"coin moves freely now {opened}")
    print(f"  price: pool {pool_price:.10f} vs curve {curve_price:.10f} sun per unit; graduation reward paid {reward / 1e6:.2f} TRX")
    print("\nTRON CURVE TEST:", "PASSED" if ok else "FAILED")


if __name__ == "__main__":
    main()
