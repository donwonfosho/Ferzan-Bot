import asyncio, os, sys, time, types, unittest
from unittest import mock
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import evm_signer as ev
import bridge
import ton_signer as ts

HOLDER = "0x0000000000001fF3684f28c67538d4D072C22734"
EVIL = "0x" + "ab" * 20
META = {"chain_id": 8453, "rpc": "http://x"}


class ZeroX(unittest.TestCase):
    def test_good_quote_passes(self):
        self.assertIsNone(ev._quote_tx_problem({"to": HOLDER, "value": "100", "gas": "300000"}, META, 100))
        self.assertIsNone(ev._quote_tx_problem({"to": HOLDER.lower(), "value": "0"}, META, 0))

    def test_wrong_destination_refused(self):
        self.assertIn("AllowanceHolder", ev._quote_tx_problem({"to": EVIL, "value": "0"}, META, 100))

    def test_too_much_value_or_gas_refused(self):
        self.assertIn("more native", ev._quote_tx_problem({"to": HOLDER, "value": "101"}, META, 100))
        self.assertIn("more native", ev._quote_tx_problem({"to": HOLDER, "value": "1"}, META, 0))
        self.assertIn("gas", ev._quote_tx_problem({"to": HOLDER, "value": "0", "gas": str(10_000_000)}, META, 5))

    def test_spender_pinned(self):
        self.assertIsNone(ev._spender_problem(HOLDER))
        self.assertIsNotNone(ev._spender_problem(EVIL))
        with mock.patch.dict(os.environ, {"EVM_ALLOWANCE_HOLDERS": EVIL}):
            self.assertIsNone(ev._spender_problem(EVIL))

    def test_sell_approves_exact_amount_not_unlimited(self):
        src = open(os.path.join(HERE, "evm_signer.py")).read()
        i = src.index("approve_data = ")
        self.assertIn("hex(int(bal))", src[i:i + 200])
        self.assertNotIn('("f" * 64)', src[i:i + 200])
        h = open(os.path.join(HERE, "hood.py")).read()
        self.assertNotIn('("f" * 64)', h)
        self.assertIn("_quote_tx_problem(tx, meta, wei)", src)
        self.assertIn("_quote_tx_problem(tx, meta, 0)", src)


class Bridge(unittest.TestCase):
    pack = {"src": "base", "amt": "0.01", "via": "relay"}

    def item(self, **k):
        d = {"to": EVIL, "data": "0xabcdef01", "value": str(10**16), "chainId": 8453}
        d.update(k)
        return d

    def test_normal_bridge_passes(self):
        self.assertIsNone(bridge._evm_items_problem([self.item()], self.pack, 8453))

    def test_other_chain_extra_value_transfer_refused(self):
        self.assertIn("different chain", bridge._evm_items_problem([self.item(chainId=1)], self.pack, 8453))
        self.assertIn("more than", bridge._evm_items_problem([self.item(value=str(10**18))], self.pack, 8453))
        self.assertIn("token", bridge._evm_items_problem([self.item(data="0xa9059cbb" + "0" * 64)], self.pack, 8453))
        self.assertIn("token", bridge._evm_items_problem([self.item(data="0x095ea7b3" + "0" * 64)], self.pack, 8453))
        self.assertIn("gas", bridge._evm_items_problem([self.item(gas=9_000_000)], self.pack, 8453))

    def test_values_add_up_across_steps(self):
        two = [self.item(value=str(6 * 10**15)), self.item(value=str(6 * 10**15))]
        self.assertIn("more than", bridge._evm_items_problem(two, self.pack, 8453))

    def test_debridge_fixed_fee_allowed(self):
        p = dict(self.pack, via="dln")
        self.assertIsNone(bridge._evm_items_problem([self.item(value=str(10**16 + 10**15))], p, 8453))

    def test_exec_checks_before_signing(self):
        src = open(os.path.join(HERE, "bridge.py")).read()
        self.assertLess(src.index("_evm_items_problem(items, pack"), src.index("evm_signer._broadcast("))


class FakeWallet:
    def __init__(self, seqs):
        self.seqs = list(seqs)
        self.address = types.SimpleNamespace(to_str=lambda **k: "EQtest")

    async def get_seqno(self):
        return self.seqs.pop(0) if len(self.seqs) > 1 else self.seqs[0]


class TonLag(unittest.TestCase):
    def setUp(self):
        ts._LAST_SIGNED.clear()
        self.sleeps = []

        async def nosleep(t):
            self.sleeps.append(t)
        self.p = mock.patch("asyncio.sleep", nosleep)
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def run_it(self, w, seq):
        return asyncio.run(ts._after_previous_send(None, w, seq))

    def test_no_previous_send_no_wait(self):
        self.assertEqual(self.run_it(FakeWallet([5]), 5), 5)
        self.assertEqual(self.sleeps, [])

    def test_stale_seqno_waits_for_the_node_to_catch_up(self):
        w = FakeWallet([5])
        ts._LAST_SIGNED[ts._addr_key(w)] = (5, time.monotonic())
        w.seqs = [5, 5, 6]
        self.assertEqual(self.run_it(w, 5), 6)

    def test_message_that_never_landed_keeps_its_seqno(self):
        w = FakeWallet([5])
        ts._LAST_SIGNED[ts._addr_key(w)] = (5, time.monotonic())
        t = [0.0]
        with mock.patch.object(time, "monotonic", side_effect=lambda: (t.__setitem__(0, t[0] + 10), t[0])[1]):
            self.assertEqual(self.run_it(w, 5), 5)

    def test_old_send_ignored(self):
        w = FakeWallet([5])
        ts._LAST_SIGNED[ts._addr_key(w)] = (5, time.monotonic() - 1000)
        self.assertEqual(self.run_it(w, 5), 5)
        self.assertEqual(self.sleeps, [])

    def test_already_advanced_no_wait(self):
        w = FakeWallet([6])
        ts._LAST_SIGNED[ts._addr_key(w)] = (5, time.monotonic())
        self.assertEqual(self.run_it(w, 6), 6)
        self.assertEqual(self.sleeps, [])


if __name__ == "__main__":
    unittest.main()
