"""
Deploy Ferzan's v3 factories (vanity token addresses via CREATE2) to one EVM chain.

  python scripts/deploy_v3.py bsc curve plan   # compile + on-chain checks, sends nothing
  python scripts/deploy_v3.py bsc curve send   # deploy for real (once per chain + kind)
  python scripts/deploy_v3.py bsc plain plan|send

curve = FerzanCurveFactoryV3, plain = LaunchTokenFactoryV3. Same toolchain + deployer key
as before. Treasury, fee (and DEX/WETH for curves) are fixed forever; no owner, no admin.
After a deploy it runs a live vanity self-test (read-only) against the new factory.
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
    "ethereum": {
        "rpc": "https://ethereum-rpc.publicnode.com", "rpc_env": "ETHEREUM_RPC_URL", "chain_id": 1, "sym": "ETH",
        "fee_wei": 3 * 10**15, "dex": "Uniswap V2", "dex_factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "weth": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "weth_symbol": "WETH",
        "probe": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC: a WETH/USDC pool must exist
        "env": "FACTORY_ETH_CURVE", "explorer": "https://etherscan.io/address/",
    },
    "robinhood": {
        "rpc": "https://rpc.mainnet.chain.robinhood.com", "rpc_env": "ROBINHOOD_RPC_URL", "chain_id": 4663, "sym": "ETH",
        "fee_wei": 3 * 10**15, "dex": "Uniswap V2", "dex_factory": "0x8bceaa40b9acdfaedf85adf4ff01f5ad6517937f",
        "weth": "0x0Bd7D308f8E1639FAb988df18A8011f41EAcAD73", "weth_symbol": "WETH",
        # no well-known stable pool yet: verify via Uniswap's official Router02 (its factory() and WETH() must match)
        "router": "0x89e5db8b5aa49aa85ac63f691524311aeb649eba",
        "env": "FACTORY_HOOD_CURVE", "explorer": "https://robinhoodchain.blockscout.com/address/",
    },
}
ROUTER_ABI = [{"name": n, "type": "function", "stateMutability": "view", "inputs": [],
               "outputs": [{"name": "", "type": "address"}]} for n in ("factory", "WETH")]
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


def compile_factory(kind):
    if not SOLC.exists() or not (LIB / "@openzeppelin/contracts/token/ERC20/ERC20.sol").exists():
        sys.exit("ABORT: compiler/OpenZeppelin missing in /opt/ferzan/evm-tools")
    cname = "FerzanCurveFactoryV3" if kind == "curve" else "LaunchTokenFactoryV3"
    if kind == "plain" and "Ownable" in (HERE / "contracts" / "LaunchToken.sol").read_text():
        sys.exit("ABORT: LaunchToken.sol still has an owner role")
    src = HERE / "contracts" / f"{cname}.sol"
    res = subprocess.run(
        [str(SOLC), "--optimize", "--optimize-runs", "200", "--evm-version", "paris",
         "--base-path", str(HERE), "--include-path", str(LIB),
         "--combined-json", "abi,bin", str(src)],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        sys.exit("ABORT: compile failed\n" + res.stderr[-1500:])
    data = json.loads(res.stdout)["contracts"]
    key = next(k for k in data if k.endswith(":" + cname))
    abi = data[key]["abi"]
    return (json.loads(abi) if isinstance(abi, str) else abi), "0x" + data[key]["bin"]


def deployer():
    f = KEYS / "evm-deployer.json"
    if not f.exists():
        sys.exit("ABORT: deployer key missing (/opt/ferzan/dbc-keys/evm-deployer.json)")
    return Account.from_key(json.loads(f.read_text())["key"])


def main():
    if len(sys.argv) < 3 or sys.argv[1] not in CHAINS or sys.argv[2] not in ("curve", "plain"):
        sys.exit("usage: deploy_v3.py bsc|base|ethereum|robinhood curve|plain [plan|send]")
    chain, kind, mode = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "plan")
    c = CHAINS[chain]
    rec_key = f"{chain}_{kind}_v3"
    env_name = c["env"] if kind == "curve" else c["env"].replace("_CURVE", "_PLAIN")
    treasury = (env_file().get("PLATFORM_TREASURY_EVM") or "").strip()
    if not Web3.is_address(treasury):
        sys.exit("ABORT: PLATFORM_TREASURY_EVM missing/invalid in /opt/ferzan/.env")
    treasury = Web3.to_checksum_address(treasury)
    record = json.loads(RECORD.read_text()) if RECORD.exists() else {}
    rpc = c["rpc"]
    if c.get("rpc_env"):  # a private endpoint (e.g. Alchemy) if one is saved
        for p in ("/opt/ferzan/.env", str(HERE / ".env")):
            rpc = env_file(p).get(c["rpc_env"]) or rpc
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    if w3.eth.chain_id != c["chain_id"]:
        sys.exit(f"ABORT: RPC is on chain {w3.eth.chain_id}, expected {c['chain_id']}")
    if rec_key in record and w3.eth.get_code(record[rec_key]) not in (b"", b"\x00"):
        print(f"Already deployed on {chain}: {record[rec_key]}\n{env_name}={record[rec_key]}")
        return

    # --- prove the DEX factory + WETH addresses are the real ones on this chain
    dex = Web3.to_checksum_address(c["dex_factory"])
    weth = Web3.to_checksum_address(c["weth"])
    if w3.eth.get_code(dex) in (b"", b"\x00") or w3.eth.get_code(weth) in (b"", b"\x00"):
        sys.exit("ABORT: DEX factory or WETH has no code on this chain")
    if w3.eth.contract(address=weth, abi=ERC20_ABI).functions.symbol().call() != c["weth_symbol"]:
        sys.exit("ABORT: wrapped-native symbol mismatch")
    if c.get("probe"):
        probe_pair = w3.eth.contract(address=dex, abi=V2_FACTORY_ABI).functions.getPair(
            weth, Web3.to_checksum_address(c["probe"])).call()
        if int(probe_pair, 16) == 0:
            sys.exit("ABORT: DEX factory has no WETH/stable pool - wrong factory?")
        if w3.eth.contract(address=probe_pair, abi=PAIR_ABI).functions.factory().call() != dex:
            sys.exit("ABORT: probe pool does not belong to this DEX factory")
    else:
        router = w3.eth.contract(address=Web3.to_checksum_address(c["router"]), abi=ROUTER_ABI)
        if router.functions.factory().call() != dex or router.functions.WETH().call() != weth:
            sys.exit("ABORT: official router does not point at this DEX factory / WETH")
        probe_pair = f"router {c['router']}"

    abi, bytecode = compile_factory(kind)
    acct = deployer()
    bal = w3.eth.get_balance(acct.address)
    Factory = w3.eth.contract(abi=abi, bytecode=bytecode)
    ctor = Factory.constructor(dex, weth, treasury, c["fee_wei"]) if kind == "curve" else Factory.constructor(treasury, c["fee_wei"])
    gas = int(ctor.estimate_gas({"from": acct.address}) * 1.2) if bal > 0 else 4_500_000
    gas_price = int(w3.eth.gas_price * 1.25)  # headroom: base fee can rise before the tx lands
    cost = gas * gas_price
    print(f"Factory        : {'FerzanCurveFactoryV3' if kind == 'curve' else 'LaunchTokenFactoryV3'} (vanity addresses)")
    print(f"Chain          : {chain} ({c['chain_id']})")
    print(f"DEX            : {c['dex']} factory {dex}  (verified: live {c['weth_symbol']} pool {probe_pair})")
    print(f"Wrapped native : {weth} ({c['weth_symbol']})")
    print(f"Deployer wallet: {acct.address}  balance {Web3.from_wei(bal, 'ether')} {c['sym']}")
    print(f"Treasury       : {treasury}")
    print(f"Launch fee     : {Web3.from_wei(c['fee_wei'], 'ether')} {c['sym']} (fixed in the contract)")
    if kind == "curve":
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
                  and (kind == "plain" or (f.functions.dexFactory().call() == dex and f.functions.weth().call() == weth)))
            break
        except Exception:
            time.sleep(5)
    print(f"On-chain check (treasury, fee{', DEX, WETH' if kind == 'curve' else ''}): {'OK' if ok else 'MISMATCH!' if ok is False else 'node not synced yet - recheck later'}")
    selftest(w3, f, kind, acct.address)
    print(f"{env_name}={addr}")


def selftest(w3, f, kind, who):
    """Read-only: find a vanity salt against the live factory and confirm it with predictToken()."""
    sys.path.insert(0, str(HERE))
    import vanity
    t = time.time()
    try:
        if kind == "curve":
            h = f.functions.tokenInitCodeHash("Self Test", "TEST", 10**27).call()
        else:
            h = f.functions.tokenInitCodeHash("Self Test", "TEST", 10**27, who, "", False).call()
        salt = vanity.find_salt(f.address, bytes(h), who)
        if not salt:
            print("Vanity self-test: no salt found in time (launches fall back to normal addresses)")
            return
        if kind == "curve":
            pred = f.functions.predictToken(who, salt, "Self Test", "TEST", 10**27).call()
        else:
            pred = f.functions.predictToken(who, salt, "Self Test", "TEST", 10**27, "", False).call()
        good = pred.lower().endswith(vanity.EVM_SUFFIX)
        print(f"Vanity self-test: {pred} ({time.time() - t:.1f}s) {'OK' if good else 'MISMATCH!'}")
    except Exception as e:
        print(f"Vanity self-test failed: {e}")


if __name__ == "__main__":
    main()
