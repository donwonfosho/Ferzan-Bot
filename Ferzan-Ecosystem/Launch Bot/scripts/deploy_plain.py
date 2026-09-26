"""
Deploy Ferzan's plain-launch factory (LaunchTokenFactory) to one EVM chain.

  python scripts/deploy_plain.py base plan   # compile + checks, sends nothing
  python scripts/deploy_plain.py base send   # deploy for real (once per chain)

Compiles contracts/ with the official solc 0.8.24 + OpenZeppelin v5.0.2 source
(downloaded once into /opt/ferzan/evm-tools). The deployer key lives in
/opt/ferzan/dbc-keys (outside git). Treasury = PLATFORM_TREASURY_EVM.
The treasury address and fee are baked in forever for that deployment.
"""
import json, os, subprocess, sys, time
from pathlib import Path

from eth_account import Account
from web3 import Web3

CHAINS = {
    "base": {"rpc": "https://mainnet.base.org", "chain_id": 8453, "fee_wei": 3 * 10**15, "sym": "ETH",
             "env": "FACTORY_BASE_PLAIN", "explorer": "https://basescan.org/address/"},
    "bsc": {"rpc": "https://bsc-dataseed.binance.org", "chain_id": 56, "fee_wei": 15 * 10**15, "sym": "BNB",
            "env": "FACTORY_BSC_PLAIN", "explorer": "https://bscscan.com/address/"},
}
TOOLS = Path("/opt/ferzan/evm-tools")
SOLC = TOOLS / "solc-0.8.24"
LIB = TOOLS / "lib"  # holds @openzeppelin/contracts (v5.0.2)
KEYS = Path("/opt/ferzan/dbc-keys")
RECORD = KEYS / "evm-factories.json"
HERE = Path(__file__).resolve().parent.parent  # Launch Bot folder


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
        sys.exit("ABORT: compiler/OpenZeppelin missing in /opt/ferzan/evm-tools (run the setup step first)")
    src = HERE / "contracts" / "LaunchTokenFactory.sol"
    tok = (HERE / "contracts" / "LaunchToken.sol").read_text()
    if "Ownable" in tok:
        sys.exit("ABORT: LaunchToken.sol still has the Ownable owner role - apply the no-owner fix first")
    res = subprocess.run(
        [str(SOLC), "--optimize", "--optimize-runs", "200", "--evm-version", "paris",
         "--base-path", str(HERE), "--include-path", str(LIB),
         "--combined-json", "abi,bin", str(src)],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        sys.exit("ABORT: compile failed\n" + res.stderr[-1500:])
    data = json.loads(res.stdout)["contracts"]
    key = next(k for k in data if k.endswith(":LaunchTokenFactory"))
    abi = data[key]["abi"]
    return (json.loads(abi) if isinstance(abi, str) else abi), "0x" + data[key]["bin"]


def deployer():
    KEYS.mkdir(parents=True, exist_ok=True, mode=0o700)
    f = KEYS / "evm-deployer.json"
    if f.exists():
        return Account.from_key(json.loads(f.read_text())["key"])
    acct = Account.create()
    f.write_text(json.dumps({"key": acct.key.hex(), "address": acct.address}))
    os.chmod(f, 0o600)
    return acct


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in CHAINS:
        sys.exit("usage: deploy_plain.py base|bsc [plan|send]")
    chain, mode = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "plan")
    c = CHAINS[chain]
    env = env_file()
    treasury = (env.get("PLATFORM_TREASURY_EVM") or "").strip()
    if not Web3.is_address(treasury):
        sys.exit("ABORT: PLATFORM_TREASURY_EVM missing/invalid in /opt/ferzan/.env")
    treasury = Web3.to_checksum_address(treasury)
    record = json.loads(RECORD.read_text()) if RECORD.exists() else {}
    w3 = Web3(Web3.HTTPProvider(c["rpc"], request_kwargs={"timeout": 30}))
    if w3.eth.chain_id != c["chain_id"]:
        sys.exit(f"ABORT: RPC is on chain {w3.eth.chain_id}, expected {c['chain_id']}")
    if chain in record and w3.eth.get_code(record[chain]) not in (b"", b"\x00"):
        print(f"Already deployed on {chain}: {record[chain]}\n{c['env']}={record[chain]}")
        return

    abi, bytecode = compile_factory()
    acct = deployer()
    bal = w3.eth.get_balance(acct.address)
    Factory = w3.eth.contract(abi=abi, bytecode=bytecode)
    ctor = Factory.constructor(treasury, c["fee_wei"])
    gas = int(ctor.estimate_gas({"from": acct.address}) * 1.2) if bal > 0 else 1_200_000
    gas_price = w3.eth.gas_price
    cost = gas * gas_price
    print(f"Chain          : {chain} ({c['chain_id']})")
    print(f"Deployer wallet: {acct.address}  balance {Web3.from_wei(bal, 'ether')} {c['sym']}")
    print(f"Treasury       : {treasury}")
    print(f"Launch fee     : {Web3.from_wei(c['fee_wei'], 'ether')} {c['sym']} (fixed in the contract)")
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
    f = w3.eth.contract(address=addr, abi=abi)
    ok = f.functions.platformTreasury().call() == treasury and f.functions.launchFeeWei().call() == c["fee_wei"]
    record[chain] = addr
    RECORD.write_text(json.dumps(record, indent=1))
    print(f"DEPLOYED: {c['explorer']}{addr}")
    print(f"On-chain check (treasury + fee): {'OK' if ok else 'MISMATCH!'}")
    print(f"{c['env']}={addr}")


if __name__ == "__main__":
    main()
