import os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import db
import evm_signer as ev
import sendstate
import sniper

META = {"rpc": "http://x", "chain_id": 8453, "explorer_tx": "https://basescan.org/tx/{txid}"}
TXH = "0x" + "ab" * 32


class Unclear(unittest.TestCase):
    def test_every_signer_wording_is_recognised(self):
        for m in ("Not confirmed yet — check the link before retrying.",
                  "Curve buy sent but not confirmed within a minute. Check before retrying:\nlink",
                  "TRON sell sent but not confirmed within a minute. Check before retrying:\nlink",
                  "It MAY have gone through: check your wallet before retrying.",
                  "Sent, not confirmed yet: abc",
                  "BASE buy was sent but not confirmed yet - it MAY still land. Check the link before retrying:\nl"):
            self.assertTrue(sendstate.is_unclear(m), m)

    def test_clean_failures_are_not_unclear(self):
        for m in ("Failed on-chain: slippage", "Expired without landing (nothing spent).",
                  "Live buys are off.", "BASE buy reverted on chain - nothing changed hands and no fee was taken."):
            self.assertFalse(sendstate.is_unclear(m), m)

    def test_hold_and_clear(self):
        sendstate.mark(1, "MintA")
        self.assertTrue(sendstate.held(1, "minta"))
        self.assertFalse(sendstate.held(2, "minta"))
        sendstate.clear(1, "MintA")
        self.assertFalse(sendstate.held(1, "minta"))


class Snipe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        self.sid = db.add_snipe(user_id=5, query="0x" + "11" * 20, chain="base", usd=5, min_liq=0, min_score=0, max_age_h=None, require_long=0)

    def tearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def status(self):
        with db.get_conn() as c:
            return c.execute("SELECT status FROM snipes WHERE id=?", (self.sid,)).fetchone()[0]

    def test_claim_is_exclusive(self):
        self.assertTrue(db.claim_snipe(self.sid))
        self.assertFalse(db.claim_snipe(self.sid))
        self.assertEqual(db.active_snipes(), [])  # a claimed snipe is invisible to the scan job
        db.release_snipe(self.sid)
        self.assertEqual(len(db.active_snipes()), 1)

    def _run(self, buy_result):
        card = mock.Mock(); card.snapshot.token_address = "0x" + "11" * 20
        order = db.active_snipes()[0]
        with mock.patch.object(sniper, "inspect_target", return_value=card), \
                mock.patch.object(sniper, "gates_for", return_value=(True, "ok")), \
                mock.patch("user_wallets.secrets", return_value=("s", "e")), \
                mock.patch("signer.max_usd", return_value=50.0), \
                mock.patch("evm_signer.buy_evm", return_value=buy_result):
            return sniper.try_fill(order)

    def test_unclear_send_is_never_retried(self):
        sendstate.clear(5, "0x" + "11" * 20)
        st, _ = self._run((False, "Curve buy was sent but not confirmed yet - it MAY still land. Check the link before retrying:\nx"))
        self.assertEqual(st, "unconfirmed")
        self.assertEqual(self.status(), "unconfirmed")
        self.assertEqual(db.active_snipes(), [])
        sendstate.clear(5, "0x" + "11" * 20)

    def test_clean_failure_stays_armed(self):
        st, _ = self._run((False, "0x quote failed: busy"))
        self.assertEqual((st, self.status()), ("armed", "armed"))

    def test_fill(self):
        st, _ = self._run((True, "Live BASE buy"))
        self.assertEqual((st, self.status()), ("filled", "filled"))


class EvmSend(unittest.TestCase):
    def test_already_known_with_our_hash_is_success(self):
        with mock.patch.object(ev, "_send_raw_once", return_value={"error": {"message": "already known"}}), \
                mock.patch.object(ev, "_local_tx_hash", return_value=TXH), \
                mock.patch.object(ev, "_tx_seen", return_value=True):
            self.assertEqual(ev._send_raw(META, "0x00"), {"result": TXH})

    def test_nonce_too_low_but_hash_unknown_stays_an_error(self):
        err = {"error": {"message": "nonce too low"}}
        with mock.patch.object(ev, "_send_raw_once", return_value=err), \
                mock.patch.object(ev, "_local_tx_hash", return_value=TXH), \
                mock.patch.object(ev, "_tx_seen", return_value=False):
            self.assertEqual(ev._send_raw(META, "0x00"), err)

    def test_exception_after_node_has_it_is_success(self):
        with mock.patch.object(ev, "_send_raw_once", side_effect=RuntimeError("timeout")), \
                mock.patch.object(ev, "_local_tx_hash", return_value=TXH), \
                mock.patch.object(ev, "_tx_seen", return_value=True):
            self.assertEqual(ev._send_raw(META, "0x00"), {"result": TXH})

    def test_exception_with_nothing_seen_still_raises(self):
        with mock.patch.object(ev, "_send_raw_once", side_effect=RuntimeError("down")), \
                mock.patch.object(ev, "_local_tx_hash", return_value=TXH), \
                mock.patch.object(ev, "_tx_seen", return_value=False):
            with self.assertRaises(RuntimeError):
                ev._send_raw(META, "0x00")


class Receipt(unittest.TestCase):
    def test_success_reverted_unknown(self):
        with mock.patch.object(ev, "_confirm_tx", return_value=True):
            self.assertEqual(ev._await_fill(META, TXH, "OK", "Buy"), (True, "OK"))
        with mock.patch.object(ev, "_confirm_tx", return_value=False):
            ok, msg = ev._await_fill(META, TXH, "OK", "Buy")
            self.assertFalse(ok); self.assertIn("no fee was taken", msg); self.assertFalse(sendstate.is_unclear(msg))
        with mock.patch.object(ev, "_confirm_tx", return_value=None):
            ok, msg = ev._await_fill(META, TXH, "OK", "Buy")
            self.assertFalse(ok); self.assertTrue(sendstate.is_unclear(msg))

    def test_confirm_reads_receipt_status(self):
        with mock.patch.object(ev, "_rpc", return_value={"result": {"status": "0x1"}}):
            self.assertTrue(ev._confirm_tx(META, TXH))
        with mock.patch.object(ev, "_rpc", return_value={"result": {"status": "0x0"}}):
            self.assertFalse(ev._confirm_tx(META, TXH))
        with mock.patch.dict(os.environ, {"EVM_RECEIPT_WAIT_S": "5"}), \
                mock.patch.object(ev, "_rpc", return_value={"result": None}), \
                mock.patch("time.sleep"), mock.patch("time.monotonic", side_effect=[0, 0, 6]):
            self.assertIsNone(ev._confirm_tx(META, TXH))


if __name__ == "__main__":
    unittest.main()
