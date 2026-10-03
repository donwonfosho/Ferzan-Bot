"""
Deploy and test Ferzan's Tron launch contracts (FerzanTrc20 master + FerzanTronFactory).

  python scripts/tron_deploy.py nile plan      # compile, show the deployer + balance + estimates, send nothing
  python scripts/tron_deploy.py nile send      # deploy master + factory on Nile (Tron's test network)
  python scripts/tron_deploy.py nile test      # launch a test token through the factory and check it
  python scripts/tron_deploy.py mainnet plan|send|test

Deployer: /opt/ferzan/dbc-keys/evm-deployer.json (the same key as the EVM deploys; Tron address derived from it).
Treasury (mainnet): PLATFORM_TREASURY_TRX in /opt/ferzan/.env. Launch fee: TRON_FEE_TRX (default 0 = free; Nile 1 TRX).
Addresses are saved in /opt/ferzan/dbc-keys/tron-factories.json before anything else, so nothing deploys twice.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

NET = sys.argv[1] if len(sys.argv) > 1 else ""
MODE = sys.argv[2] if len(sys.argv) > 2 else "plan"
if NET not in ("nile", "mainnet") or MODE not in ("plan", "send", "test"):
    sys.exit("usage: tron_deploy.py nile|mainnet plan|send|test")
os.environ["TRONGRID_URL"] = "https://nile.trongrid.io" if NET == "nile" else "https://api.trongrid.io"

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE.parent / "Trade Desk"))
import tron_signer as ts  # noqa: E402  (base58, signing, TronGrid POST; same code the Trade Bot trades with)

SOLC = Path("/opt/ferzan/evm-tools/solc-0.8.24")
KEYS = Path("/opt/ferzan/dbc-keys")
RECORD = KEYS / "tron-factories.json"
EXPLORER = "https://nile.tronscan.org/#" if NET == "nile" else "https://tronscan.org/#"


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


def compile_all():
    src = HERE / "contracts" / "tron" / "FerzanTronFactory.sol"
    res = subprocess.run([str(SOLC), "--optimize", "--optimize-runs", "200", "--evm-version", "istanbul",
                          "--base-path", str(HERE), "--combined-json", "abi,bin", str(src)],
                         capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit("ABORT: compile failed\n" + res.stderr[-1500:])
    data = json.loads(res.stdout)["contracts"]
    out = {}
    for name in ("FerzanTrc20", "FerzanTronFactory"):
        key = next(k for k in data if k.endswith(":" + name))
        abi = data[key]["abi"]
        out[name] = (abi if isinstance(abi, str) else json.dumps(abi), data[key]["bin"])
    return out


def deployer():
    f = KEYS / "evm-deployer.json"
    if not f.exists():
        sys.exit("ABORT: deployer key missing")
    key = json.loads(f.read_text())["key"].replace("0x", "")
    addr, _ = ts.evm_key_to_tron(key)
    return addr, key


def hex41(addr: str) -> str:
    return ts._to_hex(addr)


def b58(hex41_: str) -> str:
    raw = bytes.fromhex(hex41_ if hex41_.startswith("41") else "41" + hex41_)
    return ts._b58encode(raw + ts._check(raw))


def balance_sun(addr: str) -> int:
    return int(ts._post("/wallet/getaccount", {"address": hex41(addr)}).get("balance") or 0)


def wait_info(txid: str, timeout=90) -> dict:
    t = time.time()
    while time.time() - t < timeout:
        info = ts._post("/wallet/gettransactioninfobyid", {"value": txid})
        if info.get("id"):
            return info
        time.sleep(3)
    return {}


def send_signed(tx: dict, key: str) -> str:
    if "txID" not in tx:
        sys.exit(f"ABORT: TronGrid did not build the transaction: {str(tx)[:300]}")
    ok, msg = ts._broadcast(tx, key)
    if not ok:
        sys.exit(f"ABORT: broadcast failed: {msg}")
    return tx["txID"]


def deploy(name, abi, bytecode, params_hex, owner, key, fee_limit):
    tx = ts._post("/wallet/deploycontract", {
        "owner_address": hex41(owner), "abi": abi, "bytecode": bytecode, "parameter": params_hex,
        "fee_limit": fee_limit, "call_value": 0, "consume_user_resource_percent": 100,
        "origin_energy_limit": 10_000_000, "name": name,
    })
    txid = send_signed(tx, key)
    info = wait_info(txid)
    receipt = info.get("receipt") or {}
    if receipt.get("result") != "SUCCESS":
        sys.exit(f"ABORT: {name} deploy failed on-chain: {receipt.get('result')} {EXPLORER}/transaction/{txid}")
    addr = b58(info["contract_address"])
    used = receipt.get("energy_usage_total") or 0
    burned = (info.get("fee") or 0) / 1e6
    print(f"{name}: {addr}  (energy {used:,}, burned {burned:.2f} TRX)  {EXPLORER}/contract/{addr}")
    return addr


def const_call(contract: str, sig: str, params_hex: str = "", owner: str = ""):
    out = ts._post("/wallet/triggerconstantcontract", {
        "owner_address": hex41(owner or contract), "contract_address": hex41(contract),
        "function_selector": sig, "parameter": params_hex})
    res = (out.get("constant_result") or [""])[0]
    return res


def main():
    from eth_abi import decode, encode

    rec = json.loads(RECORD.read_text()) if RECORD.exists() else {}
    net = rec.setdefault(NET, {})
    owner, key = deployer()
    fee_trx = float(env_file().get("TRON_FEE_TRX") or (1 if NET == "nile" else 0))
    fee_sun = int(fee_trx * 1_000_000)
    treasury = owner if NET == "nile" else (env_file().get("PLATFORM_TREASURY_TRX") or "").strip()
    if not treasury.startswith("T") or len(treasury) != 34:
        sys.exit("ABORT: set PLATFORM_TREASURY_TRX (a T... address) in /opt/ferzan/.env first")
    bal = balance_sun(owner)
    print(f"Network        : {NET}")
    print(f"Deployer       : {owner}  balance {bal / 1e6:,.2f} TRX")
    print(f"Treasury       : {treasury}")
    print(f"Launch fee     : {fee_trx:g} TRX (fixed in the factory)")

    if MODE in ("plan", "send"):
        built = compile_all()
        m_abi, m_bin = built["FerzanTrc20"]
        f_abi, f_bin = built["FerzanTronFactory"]
        print(f"Bytecode       : master {len(m_bin) // 2:,} bytes, factory {len(f_bin) // 2:,} bytes, compiled OK")
        print("Est. cost      : about 90-180 TRX of energy for both deploys if the wallet has no staked energy")
        if net.get("factory"):
            live = int(const_call(net["factory"], "launchFeeSun()") or "0", 16)
            if live == fee_sun:
                print(f"Already deployed: factory {net['factory']} (master {net.get('master')})")
                return
            # The fee is fixed per factory: a new fee means a new factory (the master is reused). Keep the old address on record.
            print(f"Recorded factory {net['factory']} charges {live / 1e6:g} TRX; a new one at {fee_trx:g} TRX will be deployed")
            if MODE == "send":
                net[f"factory_old_{live}"] = net.pop("factory")
                RECORD.write_text(json.dumps(rec, indent=1))
        need = 150  # a real deploy of both contracts burned 88 TRX
        if MODE == "send" and bal < need * 1_000_000:
            sys.exit(f"ABORT: the deployer has {bal / 1e6:,.2f} TRX; send about {need} TRX to {owner} first. Nothing was sent.")
        if MODE == "plan":
            if bal < need * 1_000_000:
                where = "from the Nile faucet (https://nileex.io/join/getJoinPage)" if NET == "nile" else "on Tron"
                print(f"\nNEXT: get about {need} TRX {where} to {owner}, then run plan again.")
            else:
                print("\nPlan OK - nothing sent. Run with 'send' to deploy.")
            return
        if not net.get("master"):
            net["master"] = deploy("FerzanTrc20", m_abi, m_bin, "", owner, key, 1_000_000_000)
            RECORD.write_text(json.dumps(rec, indent=1))
        params = encode(["address", "address", "uint256"],
                        ["0x" + hex41(net["master"])[2:], "0x" + hex41(treasury)[2:], fee_sun]).hex()
        net["factory"] = deploy("FerzanTronFactory", f_abi, f_bin, params, owner, key, 1_000_000_000)
        RECORD.write_text(json.dumps(rec, indent=1))
        print(f"\nTRON_FACTORY_{'NILE' if NET == 'nile' else 'MAINNET'}={net['factory']}")
        return

    # --- test: launch a token through the factory and read it back
    fac = net.get("factory")
    if not fac:
        sys.exit("ABORT: deploy the factory first")
    supply = 1_000_000 * 10**6
    params = encode(["string", "string", "uint256"], ["Ferzan Tron Test", "FZTRX", supply]).hex()
    tx = ts._post("/wallet/triggersmartcontract", {
        "owner_address": hex41(owner), "contract_address": hex41(fac),
        "function_selector": "launchToken(string,string,uint256)", "parameter": params,
        "call_value": fee_sun, "fee_limit": 200_000_000}).get("transaction") or {}
    txid = send_signed(tx, key)
    info = wait_info(txid)
    receipt = info.get("receipt") or {}
    used = receipt.get("energy_usage_total") or 0
    burned = (info.get("fee") or 0) / 1e6
    if receipt.get("result") != "SUCCESS":
        sys.exit(f"TRON TEST: FAILED ({receipt.get('result')}) {EXPLORER}/transaction/{txid}")
    topic = keccak(b"TokenLaunched(address,address,string,string,uint256)").hex()
    token = ""
    for log in info.get("log") or []:
        t = log.get("topics") or []
        if t and t[0].lower() == topic:
            token = b58("41" + t[1][-40:])
    name = decode(["string"], bytes.fromhex(const_call(token, "name()")))[0] if token else ""
    bal_t = int(const_call(token, "balanceOf(address)", encode(["address"], ["0x" + hex41(owner)[2:]]).hex()) or "0", 16) if token else 0
    total = int(const_call(token, "totalSupply()") or "0", 16) if token else 0
    ok = bool(token) and name == "Ferzan Tron Test" and bal_t == supply == total
    print(f"Test token     : {token}  name '{name}'  supply {total / 1e6:,.0f}  creator holds {bal_t / 1e6:,.0f}")
    print(f"Launch cost    : energy {used:,}, burned {burned:.2f} TRX + {fee_trx:g} TRX fee   {EXPLORER}/transaction/{txid}")
    print("TRON TEST:", "PASSED" if ok else "FAILED")


if __name__ == "__main__":
    main()
