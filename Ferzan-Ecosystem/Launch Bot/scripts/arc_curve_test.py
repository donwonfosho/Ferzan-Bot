"""
Live graduation test for the Arc curve factory (FerzanCurveFactoryUsdc), run once after deploying it.

  python scripts/arc_curve_test.py plan   # checks and costs only, sends nothing
  python scripts/arc_curve_test.py send   # launches a 1-USDC test curve, buys it to graduation, checks the pool

Uses the deployer wallet. Cost: the factory's launch fee (paid to the Ferzan treasury) + about 1.02 USDC that
ends up as locked liquidity in the test coin's pool + a few cents of gas. The point is to prove, on the real
chain, that graduation moves the raised USDC into the Uniswap v2 pair through USDC's ERC-20 view.
"""
import json
import sys
import time
from pathlib import Path

from web3 import Web3

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deploy_v3 as d  # noqa: E402

CURVE_ABI = [
    {"name": "buy", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "minTokensOut", "type": "uint256"}, {"name": "referrer", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "graduated", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "bool"}]},
    {"name": "pool", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"name": "gradTarget", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
]
PAIR_ABI = [{"name": "getReserves", "type": "function", "stateMutability": "view", "inputs": [],
             "outputs": [{"name": "", "type": "uint112"}, {"name": "", "type": "uint112"}, {"name": "", "type": "uint32"}]},
            {"name": "token0", "type": "function", "stateMutability": "view", "inputs": [], "outputs": [{"name": "", "type": "address"}]}]
BAL_ABI = [{"name": "balanceOf", "type": "function", "stateMutability": "view",
            "inputs": [{"name": "", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]}]


def send(w3, acct, tx):
    tx.update({"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address), "chainId": 5042,
               "gasPrice": int(w3.eth.gas_price * 1.25)})
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.3)
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    rc = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw), timeout=180)
    if rc.status != 1:
        sys.exit(f"ABORT: transaction failed on-chain: {rc.transactionHash.hex()}")
    return rc


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "plan"
    c = d.CHAINS["arc"]
    rpc = d.env_file().get("ARC_RPC_URL") or c["rpc"]
    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    if w3.eth.chain_id != 5042:
        sys.exit("ABORT: not connected to Arc")
    record = json.loads(d.RECORD.read_text()) if d.RECORD.exists() else {}
    fac_addr = record.get("arc_curve_v3")
    if not fac_addr:
        sys.exit("ABORT: no Arc curve factory recorded - deploy it first")
    abi, _ = d.compile_factory("curve", c)
    fac = w3.eth.contract(address=fac_addr, abi=abi)
    fee = fac.functions.launchFeeWei().call()
    acct = d.deployer()
    bal = w3.eth.get_balance(acct.address)
    buy = 105 * 10**16  # 1.05 USDC: covers the 1 USDC target + 1% fee; the curve refunds any extra
    need = fee + buy + 2 * 10**17
    print(f"Factory : {fac_addr}  (launch fee {fee / 1e18:g} USDC)")
    print(f"Deployer: {acct.address}  balance {bal / 1e18:.4f} USDC, test needs about {need / 1e18:.2f} USDC")
    if bal < need:
        print(f"\nNEXT: send about {(need - bal) / 1e18 + 0.1:.2f} USDC on Arc to {acct.address}, then run plan again.")
        return
    if mode != "send":
        print("\nPlan OK - nothing sent. Run with 'send' to do the live graduation test.")
        return

    p = ("Ferzan Arc Test", "FZTEST", 10**27, 10**18, 0, 0, [], [])
    rc = send(w3, acct, fac.functions.launch(p).build_transaction({"value": fee}))
    ev = fac.events.CurveLaunched().process_receipt(rc)
    if not ev:
        sys.exit("ABORT: no CurveLaunched event")
    curve_addr, token = ev[0]["args"]["curve"], ev[0]["args"]["token"]
    curve = w3.eth.contract(address=curve_addr, abi=CURVE_ABI)
    pool = curve.functions.pool().call()
    print(f"Launched: token {token}  curve {curve_addr}  pool {pool}")
    usdc = w3.eth.contract(address=Web3.to_checksum_address(c["weth"]), abi=BAL_ABI)
    before_native = w3.eth.get_balance(curve_addr)
    send(w3, acct, curve.functions.buy(0, "0x0000000000000000000000000000000000000000").build_transaction({"value": buy}))
    time.sleep(2)
    grad = curve.functions.graduated().call()
    r0, r1, _ = w3.eth.contract(address=pool, abi=PAIR_ABI).functions.getReserves().call()
    pool_usdc = usdc.functions.balanceOf(pool).call()
    left_native = w3.eth.get_balance(curve_addr)
    print(f"Graduated          : {grad}")
    print(f"Pool reserves      : {r0} / {r1}")
    print(f"Pool USDC (ERC-20) : {pool_usdc / 1e6:.6f} USDC")
    print(f"Curve native left  : {left_native / 1e18:.12f} USDC (dust only; was {before_native / 1e18:g} before the buy)")
    ok = grad and r0 > 0 and r1 > 0 and pool_usdc >= 9 * 10**5 and left_native < 10**13
    print("\nARC CURVE TEST: PASSED" if ok else "\nARC CURVE TEST: FAILED - do not switch Arc curves on")
    print(f"Test coin (hide it from the board afterwards): {token}")


if __name__ == "__main__":
    main()
