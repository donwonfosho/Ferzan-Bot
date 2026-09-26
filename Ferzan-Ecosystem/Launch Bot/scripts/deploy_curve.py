"""
Deploy Ferzan's bonding-curve factory v2 (FerzanCurveFactory) to one EVM chain.

  python scripts/deploy_curve.py bsc plan    # compile + on-chain checks, sends nothing
  python scripts/deploy_curve.py bsc send    # deploy for real (once per chain)

Same toolchain + deployer key as deploy_plain.py. DEX factory, WETH, treasury and the
launch fee are fixed forever in that deployment (no owner, no admin).
"""
import json, subprocess, sys, time
from pathlib import Path

from eth_account import Account
from web3 import Web3

CHAINS = {
    "bsc": {
        "rpc": "https://bsc-dataseed.binance.org", "chain_id": 56, "sym": "BNB", "fee_wei": 15 * 10**15,
        "dex": "PancakeSwap V2", "dex_factory": "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73",
        "weth": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "weth_symbol": "WBNB",
        "probe": "0x55d398326f99059fF775485246999027B3197955",  # USDT: a WBNB/USDT pool must exist
        "env": "FACTORY_BSC_CURVE", "explorer": "https://bscscan.com/address/",
    },
    "base": {
        "rpc": "https://mainnet.base.org", "chain_id": 8453, "sym": "ETH", "fee_wei": 3 * 10**15,
        "dex": "Uniswap V2", "dex_factory": "0x8909Dc15e40173Ff4699343b6eB8132c65e18eC6",
        "weth": "0x4200000000000000000000000000000000000006", "weth_symbol": "WETH",
        "probe": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC: a WETH/USDC pool must exist
        "env": "FACTORY_BASE_CURVE", "explorer": "https://basescan.org/address/",
    },
}
TOOLS = Path("/opt/ferzan/evm-tools")
SOLC = TOOLS / "solc-0.8.24"
LIB = TOOLS / "lib"
KEYS = Path("/opt/ferzan/dbc-keys")
RECORD = KEYS / "evm-factories.json"
HERE = Path(__file__).resolve().parent.parent  # Launch Bot folder

V2_FACTORY_ABI = [{"name": "getPair", "type": "function", "stateMutability": "view",
                   "inputs": [{"name": "a", "type": "address"}, {"name": "b", "type": "address"}],
                   "outputs": [{"name": "", "type": "address"}]}]
PAIR_ABI = [{"name": "factory", "type": "function", "stateMutability": "view", "inputs": [],
             "outputs": [{"name": "", "type": "address"}]}]
ERC20_ABI = [{"name": "symbol", "type": "function", "stateMutability": "view", "inputs": [],
              "outputs": [{"name": "", "type": "string"}]}]


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


def compile_factory():
    if not SOLC.exists() or not (LIB / "@openzeppelin/contracts/token/ERC20/ERC20.sol").exists():
        sys.exit("ABORT: compiler/OpenZeppelin missing in /opt/ferzan/evm-tools")
    src = HERE / "contracts" / "FerzanCurveFactory.sol"
    res = subprocess.run(
        [str(SOLC), "--optimize", "--optimize-runs", "200", "--evm-version", "paris",
         "--base-path", str(HERE), "--include-path", str(LIB),
         "--combined-json", "abi,bin", str(src)],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        sys.exit("ABORT: compile failed\n" + res.stderr[-1500:])
    data = json.loads(res.stdout)["contracts"]
    key = next(k for k in data if k.endswith(":FerzanCurveFactory"))
    abi = data[key]["abi"]
    return (json.loads(abi) if isinstance(abi, str) else abi), "0x" + data[key]["bin"]


def deployer():
    f = KEYS / "evm-deployer.json"
    if not f.exists():
        sys.exit("ABORT: deployer key missing (/opt/ferzan/dbc-keys/evm-deployer.json)")
    return Account.from_key(json.loads(f.read_text())["key"])


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in CHAINS:
        sys.exit("usage: deploy_curve.py bsc|base [plan|send]")
    chain, mode = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "plan")
    c = CHAINS[chain]
    rec_key = f"{chain}_curve"
    treasury = (env_file().get("PLATFORM_TREASURY_EVM") or "").strip()
    if not Web3.is_address(treasury):
        sys.exit("ABORT: PLATFORM_TREASURY_EVM missing/invalid in /opt/ferzan/.env")
    treasury = Web3.to_checksum_address(treasury)
    record = json.loads(RECORD.read_text()) if RECORD.exists() else {}
    w3 = Web3(Web3.HTTPProvider(c["rpc"], request_kwargs={"timeout": 30}))
    if w3.eth.chain_id != c["chain_id"]:
        sys.exit(f"ABORT: RPC is on chain {w3.eth.chain_id}, expected {c['chain_id']}")
    if rec_key in record and w3.eth.get_code(record[rec_key]) not in (b"", b"\x00"):
        print(f"Already deployed on {chain}: {record[rec_key]}\n{c['env']}={record[rec_key]}")
        return

    # --- prove the DEX factory + WETH addresses are the real ones on this chain
    dex = Web3.to_checksum_address(c["dex_factory"])
    weth = Web3.to_checksum_address(c["weth"])
    if w3.eth.get_code(dex) in (b"", b"\x00") or w3.eth.get_code(weth) in (b"", b"\x00"):
        sys.exit("ABORT: DEX factory or WETH has no code on this chain")
    if w3.eth.contract(address=weth, abi=ERC20_ABI).functions.symbol().call() != c["weth_symbol"]:
        sys.exit("ABORT: wrapped-native symbol mismatch")
    probe_pair = w3.eth.contract(address=dex, abi=V2_FACTORY_ABI).functions.getPair(
        weth, Web3.to_checksum_address(c["probe"])).call()
    if int(probe_pair, 16) == 0:
        sys.exit("ABORT: DEX factory has no WETH/stable pool - wrong factory?")
    if w3.eth.contract(address=probe_pair, abi=PAIR_ABI).functions.factory().call() != dex:
        sys.exit("ABORT: probe pool does not belong to this DEX factory")

    abi, bytecode = compile_factory()
    acct = deployer()
    bal = w3.eth.get_balance(acct.address)
    Factory = w3.eth.contract(abi=abi, bytecode=bytecode)
    ctor = Factory.constructor(dex, weth, treasury, c["fee_wei"])
    gas = int(ctor.estimate_gas({"from": acct.address}) * 1.2) if bal > 0 else 4_500_000
    gas_price = w3.eth.gas_price
    cost = gas * gas_price
    print(f"Chain          : {chain} ({c['chain_id']})")
    print(f"DEX            : {c['dex']} factory {dex}  (verified: live {c['weth_symbol']} pool {probe_pair})")
    print(f"Wrapped native : {weth} ({c['weth_symbol']})")
    print(f"Deployer wallet: {acct.address}  balance {Web3.from_wei(bal, 'ether')} {c['sym']}")
    print(f"Treasury       : {treasury}")
    print(f"Launch fee     : {Web3.from_wei(c['fee_wei'], 'ether')} {c['sym']} (fixed in the contract)")
    print(f"Trading fee    : 1% - 50% creator / 50% platform (40/50/10 with a referrer)")
    print(f"Bytecode       : {len(bytecode) // 2 - 1} bytes, compiled OK")
    print(f"Est. deploy gas: {gas} @ {Web3.from_wei(gas_price, 'gwei'):.4f} gwei = ~{Web3.from_wei(cost, 'ether'):.6f} {c['sym']}")
    if bal < cost * 2:
        print(f"\nNEXT: send ~{Web3.from_wei(max(cost * 3, 10**15), 'ether'):.5f} {c['sym']} on {chain} to {acct.address}, then run plan again.")
        return
    if mode != "send":
        print("\nPlan OK - nothing sent. Run with 'send' to deploy.")
        return

    tx = ctor.build_transaction({
        "from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
        "gas": gas, "gasPrice": gas_price, "chainId": c["chain_id"],
    })
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    txh = w3.eth.send_raw_transaction(raw)
    print("Sent:", txh.hex())
    rcpt = w3.eth.wait_for_transaction_receipt(txh, timeout=180)
    if rcpt.status != 1:
        sys.exit("ABORT: deploy transaction failed on-chain")
    addr = rcpt.contractAddress
    # save first, so a slow RPC node can never make us deploy twice
    record[rec_key] = addr
    RECORD.write_text(json.dumps(record, indent=1))
    print(f"DEPLOYED + saved: {c['explorer']}{addr}")
    f = w3.eth.contract(address=addr, abi=abi)
    ok = None
    for _ in range(12):
        try:
            ok = (f.functions.platformTreasury().call() == treasury
                  and f.functions.launchFeeWei().call() == c["fee_wei"]
                  and f.functions.dexFactory().call() == dex and f.functions.weth().call() == weth)
            break
        except Exception:
            time.sleep(5)
    print(f"On-chain check (treasury, fee, DEX, WETH): {'OK' if ok else 'MISMATCH!' if ok is False else 'node not synced yet - recheck later'}")
    print(f"{c['env']}={addr}")


if __name__ == "__main__":
    main()
