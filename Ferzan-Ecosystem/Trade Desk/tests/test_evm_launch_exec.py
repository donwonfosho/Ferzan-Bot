"""Trade Bot wallet EVM launch helper: every safety check, no network, nothing signed for real.

  cd "Trade Desk" && python -m unittest tests.test_evm_launch_exec
"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import evm_launch_exec as ex  # noqa: E402

ADDR = "0x" + "11" * 20
FACTORY = "0x" + "22" * 20


def call(fn, *a, **k):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            fn(*a, **k)
        except SystemExit:
            pass
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def good_tx(**kw):
    tx = {"from": ADDR, "to": FACTORY, "data": "0xabcdef0123", "value": 3 * 10**15, "gas": 500000, "gasPrice": 10**9,
          "chainId": 8453, "launch_fee_wei": str(10**15), "dev_buy_wei": str(2 * 10**15)}
    tx.update(kw)
    return tx


class CheckTx(unittest.TestCase):
    def setUp(self):
        self.meta = {"chain_id": 8453}

    def err(self, **kw):
        return call(ex._check_tx, good_tx(**kw), self.meta, ADDR, FACTORY, [FACTORY]).get("error")

    def test_good_tx_passes(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            v, g, d = ex._check_tx(good_tx(), self.meta, ADDR, FACTORY, [FACTORY])
        self.assertEqual((v, g), (3 * 10**15, 500000))

    def test_wrong_chain(self):
        self.assertIn("another chain", self.err(chainId=1))

    def test_not_factory(self):
        self.assertIn("not for the Ferzan factory", self.err(to="0x" + "33" * 20))

    def test_other_wallet(self):
        self.assertIn("another wallet", self.err(**{"from": "0x" + "44" * 20}))

    def test_no_calldata(self):
        self.assertIn("call data", self.err(data="0x"))

    def test_value_must_match_fee_plus_dev(self):
        self.assertIn("does not match", self.err(value=9 * 10**15))

    def test_value_cap(self):
        big = 10**19
        self.assertIn("safety cap", self.err(value=big, launch_fee_wei=str(big), dev_buy_wei="0"))

    def test_factory_must_be_pinned_by_the_launch_bot(self):
        e = call(ex._check_tx, good_tx(), self.meta, ADDR, FACTORY, ["0x" + "55" * 20]).get("error")
        self.assertIn("not one of the Ferzan factories", e)
        e = call(ex._check_tx, good_tx(), self.meta, ADDR, FACTORY, []).get("error")
        self.assertIn("not one of the Ferzan factories", e)

    def test_bad_gas(self):
        self.assertIn("gas limit", self.err(gas=10))


class Chains(unittest.TestCase):
    def test_unsupported_chain_refused(self):
        self.assertEqual(call(ex._meta, "arc").get("error"), "unsupported_chain")
        self.assertEqual(call(ex._meta, "solana").get("error"), "unsupported_chain")

    def test_robinhood_maps(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            cid, meta, _es = ex._meta("robinhood")
        self.assertEqual(cid, "hood")
        self.assertTrue(meta.get("chain_id"))


class FakeSigned:
    raw_transaction = bytes.fromhex("aabb")
    hash = bytes.fromhex("cc" * 32)


class FakeAcct:
    address = ADDR

    def sign_transaction(self, raw):
        self.last = raw
        return FakeSigned()


class FakeEs:
    _UNDERPRICED_HINTS = ("underpriced",)

    def __init__(self, bal=10**18, send=None, receipt=None):
        self.bal, self.sent, self.receipt = bal, 0, receipt
        self.chain_nonce = 7
        self._send = send

    def _rpc(self, rpc, method, params):
        if method == "eth_getBalance":
            return {"result": hex(self.bal)}
        if method == "eth_getTransactionReceipt":
            return {"result": self.receipt}
        return {}

    def _gas_price(self, rpc): return 10**9
    def _nonce_guarded(self, meta, addr): return 7
    def _nonce(self, rpc, addr): return self.chain_nonce
    def _addr(self, v): return v

    def _send_raw(self, meta, raw_hex, addr, nonce):
        self.sent += 1
        return self._send(self.sent) if self._send else {"result": "0x" + "cc" * 32}


class Launch(unittest.TestCase):
    def run_launch(self, es, tx=None, prelog=None):
        d = tempfile.mkdtemp()
        log = Path(d) / "l.json"
        if prelog is not None:
            log.write_text(json.dumps(prelog))
        args = {"uid": 1, "chain": "base", "request_id": "r1", "factory": FACTORY, "allowed_factories": [FACTORY], "tx": tx or good_tx()}
        clock = [1_800_000_000.0]  # a fake clock: waiting costs no real time
        with mock.patch.object(ex.time, "sleep", lambda sec: clock.__setitem__(0, clock[0] + sec)), \
             mock.patch.object(ex.time, "time", lambda: clock[0]), \
             mock.patch.object(ex, "LOG", log), \
             mock.patch.object(ex, "_wallet", return_value=(ADDR, "k", FakeAcct())), \
             mock.patch.object(ex, "_meta", return_value=("base", {"chain_id": 8453, "rpc": "x", "native": "ETH"}, es)):
            res = call(ex.launch, args)
        return res, json.loads(log.read_text()) if log.exists() else {}

    def test_success_records_and_reports(self):
        es = FakeEs(receipt={"status": "0x1"})
        res, log = self.run_launch(es)
        self.assertTrue(res["ok"])
        self.assertEqual(res["txhash"], "0x" + "cc" * 32)
        self.assertEqual(es.sent, 1)
        self.assertTrue(log["r1"]["broadcast"])

    def test_reverted_is_a_failure_not_pending(self):
        res, _ = self.run_launch(FakeEs(receipt={"status": "0x0"}))
        self.assertFalse(res["ok"])
        self.assertNotIn("pending", res)
        self.assertIn("reverted", res["error"])

    def test_low_balance_sends_nothing(self):
        es = FakeEs(bal=10**12, receipt={"status": "0x1"})
        res, log = self.run_launch(es)
        self.assertEqual(res["error"], "low_balance")
        self.assertEqual(es.sent, 0)
        self.assertEqual(log, {})

    def test_clear_rejection_clears_log_so_retry_is_possible(self):
        es = FakeEs(send=lambda n: {"error": {"message": "insufficient funds for gas * price + value"}})
        res, log = self.run_launch(es)
        self.assertFalse(res["ok"])
        self.assertNotIn("maybe_sent", res)
        self.assertEqual(log, {})

    def test_nonce_too_low_is_maybe_sent_and_keeps_the_record(self):
        es = FakeEs(send=lambda n: {"error": {"message": "nonce too low"}})
        res, log = self.run_launch(es)
        self.assertTrue(res["maybe_sent"])
        self.assertIn("r1", log)

    def test_replacement_underpriced_is_not_repriced_again(self):
        es = FakeEs(send=lambda n: {"error": {"message": "replacement transaction underpriced"}})
        res, log = self.run_launch(es)
        self.assertTrue(res["maybe_sent"])
        self.assertEqual(es.sent, 1)

    def test_unknown_answer_is_maybe_sent(self):
        es = FakeEs(send=lambda n: {"error": {"message": "something odd"}})
        res, log = self.run_launch(es)
        self.assertTrue(res["maybe_sent"])
        self.assertIn("r1", log)

    def test_lost_reply_is_maybe_sent_and_error_has_no_url(self):
        def boom(n):
            raise RuntimeError("HTTPSConnectionPool(host='x', port=443): Max retries exceeded with url: /v2/SECRETKEY (Caused by X)")
        res, _ = self.run_launch(FakeEs(send=boom))
        self.assertTrue(res["maybe_sent"])
        self.assertNotIn("SECRETKEY", json.dumps(res))

    def test_expired_unsent_retry_reuses_the_same_nonce(self):
        es = FakeEs(receipt={"status": "0x1"})
        es.chain_nonce = 7
        prev = {"r1": {"txhash": "0x" + "ab" * 32, "hashes": ["0x" + "ab" * 32], "nonce": 7, "broadcast": False, "at": 1}}
        with mock.patch.object(ex, "_wait_any", side_effect=[("", {}), ("0x" + "cc" * 32, {"status": "0x1"})]):
            res, _ = self.run_launch(es, prelog=prev)
        self.assertTrue(res["ok"])
        self.assertEqual(es.sent, 1)

    def test_expired_retry_refused_if_nonce_moved(self):
        es = FakeEs(receipt=None)
        es.chain_nonce = 8
        prev = {"r1": {"txhash": "0x" + "ab" * 32, "hashes": ["0x" + "ab" * 32], "nonce": 7, "broadcast": False, "at": 1}}
        with mock.patch.object(ex, "_wait_any", return_value=("", {})):
            res, _ = self.run_launch(es, prelog=prev)
        self.assertTrue(res["pending"])
        self.assertEqual(es.sent, 0)

    def test_gas_cost_cap(self):
        res, _ = self.run_launch(FakeEs(receipt={"status": "0x1"}), tx=good_tx(gas=15_000_000, gasPrice=10**11))
        self.assertIn("gas cost", res["error"])

    def test_underpriced_retries_then_succeeds(self):
        es = FakeEs(receipt={"status": "0x1"},
                    send=lambda n: {"error": {"message": "transaction underpriced"}} if n == 1 else {"result": "0x" + "cc" * 32})
        res, _ = self.run_launch(es)
        self.assertTrue(res["ok"])
        self.assertEqual(es.sent, 2)

    def test_second_call_for_same_request_never_resends(self):
        es = FakeEs(receipt={"status": "0x1"})
        prev = {"r1": {"txhash": "0x" + "cc" * 32, "broadcast": True, "at": 1}}
        res, _ = self.run_launch(es, prelog=prev)
        self.assertTrue(res["ok"])
        self.assertTrue(res["repeat"])
        self.assertEqual(es.sent, 0)

    def test_pending_repeat_with_broadcast_does_not_resend(self):
        es = FakeEs(receipt=None)
        prev = {"r1": {"txhash": "0x" + "cc" * 32, "broadcast": True, "at": 1}}
        with mock.patch.object(ex, "_wait_any", lambda *a, **k: ("", {})):
            res, _ = self.run_launch(es, prelog=prev)
        self.assertTrue(res["pending"])
        self.assertEqual(es.sent, 0)


if __name__ == "__main__":
    unittest.main()
