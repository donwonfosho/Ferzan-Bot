"""
TON launch setup and test-network run (nothing here touches mainnet).

  python scripts/ton_testnet_launch.py setup    # extract + pin the standard jetton code (from @ton-community/assets-sdk)
  python scripts/ton_testnet_launch.py wallet   # the droplet's TESTNET wallet address + balance (fund it from @testgiver_ton_bot)
  python scripts/ton_testnet_launch.py send     # launch a test coin on testnet with the exact messages the Mini App sends, then verify
"""
import asyncio, base64, json, os, re, subprocess, sys, tempfile
from pathlib import Path

os.environ["TON_NETWORK"] = "testnet"
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import ton_launch as tl  # noqa: E402

KEY = Path("/opt/ferzan/dbc-keys/ton-testnet-wallet.json")
PKG = "@ton-community/assets-sdk@0.0.5"


def setup():
    from pytoniq_core import Cell
    tl.CODE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["npm", "pack", "--silent", PKG], cwd=d, check=True, capture_output=True)
        tgz = next(Path(d).glob("*.tgz"))
        subprocess.run(["tar", "-xzf", str(tgz), "-C", d], check=True)
        for name, want in (("jetton-minter", tl.MINTER_CODE_HASH), ("jetton-wallet", tl.WALLET_CODE_HASH)):
            src = Path(d) / "package/dist/jetton/contracts/build" / f"{name}.js"
            m = re.search(r"codeBoc:\s*['\"]([A-Za-z0-9+/=]+)['\"]", src.read_text())
            if not m:
                sys.exit(f"ABORT: no code in {src}")
            cell = Cell.one_from_boc(base64.b64decode(m.group(1)))
            if cell.hash.hex() != want:
                sys.exit(f"ABORT: {name} hash {cell.hash.hex()} does not match the pinned {want}")
            (tl.CODE_DIR / f"{name}.b64").write_text(m.group(1))
            print(f"{name}: pinned OK ({cell.hash.hex()[:16]}...)")


async def _wallet(provider):
    from pytoniq import WalletV4R2
    if not KEY.exists():
        from solders.keypair import Keypair
        KEY.write_text(json.dumps({"secret_b64": base64.b64encode(bytes(Keypair())).decode(), "network": "testnet"}))
        os.chmod(KEY, 0o600)
    seed = base64.b64decode(json.loads(KEY.read_text())["secret_b64"])
    return await WalletV4R2.from_private_key(provider, seed)


async def run(mode):
    from pytoniq import LiteBalancer
    provider = LiteBalancer.from_testnet_config(trust_level=2)
    await provider.start_up()
    try:
        w = await _wallet(provider)
        addr = w.address.to_str(is_user_friendly=True, is_bounceable=False, is_test_only=True)
        st = await provider.get_account_state(w.address)
        bal = int(getattr(st, "balance", 0) or 0)
        print(f"Testnet wallet: {addr}  balance {bal / 1e9:.3f} test TON")
        if mode == "wallet":
            return
        if bal < 1_500_000_000:
            print("NEXT: get free test TON from @testgiver_ton_bot in Telegram (send it the address above), then run send again.")
            return
        os.environ.setdefault("PLATFORM_TREASURY_TON", addr)  # testnet: the fee goes back to the same wallet
        supply = 1_000_000_000 * 10**9
        tx = tl.build_unsigned_launch_tx("TESTNET", w.address.to_str(is_user_friendly=False), supply,
                                         "https://launch.ferzaneco.com/api/metadata/testnet")
        c = tx.cells
        try:
            from pytoniq_core import StateInit
        except ImportError:
            from pytoniq_core.tlb.account import StateInit
        si = StateInit(code=c["code"], data=c["data"])
        msgs = [w.create_wallet_internal_message(destination=c["minter"], value=tl.DEPLOY_TON, body=c["mint"], state_init=si),
                w.create_wallet_internal_message(destination=c["minter"], value=tl.ADMIN_TON, body=c["drop_admin"])]
        seqno = await w.get_seqno() if "active" in repr(st).lower() else 0
        import time
        body = w.raw_create_transfer_msg(private_key=w.private_key, seqno=seqno, wallet_id=w.wallet_id, messages=msgs,
                                         valid_until=int(time.time()) + 120)
        ext = w.create_external_msg(dest=w.address, state_init=w.state_init if seqno == 0 else None, body=body)
        await provider.raw_send_message(ext.serialize().to_boc())
        print(f"Sent. Test coin (minter): {tx.minter}\nhttps://testnet.tonviewer.com/{tx.minter}")
    finally:
        await provider.close_all()
    res = tl.verify_launch(tx.minter, supply, wait_s=120)
    print("TON TESTNET LAUNCH:", "PASSED" if res.get("ok") else f"FAILED ({res.get('error')})")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "wallet"
    if mode == "setup":
        setup()
    else:
        asyncio.run(run(mode))
