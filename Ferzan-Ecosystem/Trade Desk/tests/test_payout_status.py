import os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import db
import price_fetcher
import referral_pay as rp
import signer
import withdraw


class PayoutStatus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        self.env = mock.patch.dict(os.environ, {"REFERRAL_AUTOPAY": "1", "REFERRAL_PAYOUT_SECRET": "x"}, clear=False)
        self.env.start()
        for k in ("REFERRAL_PAYOUT_LOW_USD", "REFERRAL_AUTOPAY_MAX_USD", "REFERRAL_AUTOPAY_RESERVE_SOL"):
            os.environ.pop(k, None)
        kp = mock.Mock(); kp.pubkey.return_value = "PAYOUTKEY"
        self.bal = 5_000_000_000
        self.patches = [
            mock.patch.object(signer, "keypair_from_secret", return_value=kp),
            mock.patch.object(withdraw, "sol_balance", side_effect=lambda _a: self.bal),
            mock.patch.object(price_fetcher, "get_price_usd", return_value=100.0),
        ]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.env.stop(); db.DB_PATH = self.old_path; self.tmp.cleanup()

    def _claim(self, uid, usd):
        with db.get_conn() as c:
            c.execute("INSERT INTO referral_ledger (user_id, created_at, volume_usd, share_usd, status, kind) VALUES (?,?,?,?,?,?)",
                      (uid, 1, 100.0, usd, "claimed", "share"))
            c.commit()

    def test_off_means_no_status(self):
        with mock.patch.dict(os.environ, {"REFERRAL_AUTOPAY": "0"}):
            self.assertIsNone(rp.payout_status())

    def test_healthy_wallet_is_not_low(self):
        st = rp.payout_status()   # 5 SOL - 0.02 reserve = 4.98 SOL = $498 vs the $50 default level
        self.assertFalse(st["low"])
        self.assertAlmostEqual(st["spend_usd"], 498.0, places=2)
        self.assertEqual(st["address"], "PAYOUTKEY")

    def test_low_when_under_one_max_payout(self):
        self.bal = 400_000_000   # 0.4 SOL - 0.02 = $38 < $50
        self.assertTrue(rp.payout_status()["low"])

    def test_low_when_claims_waiting_exceed_balance(self):
        self.bal = 1_000_000_000   # $98 spendable, above the $50 level...
        self._claim(1, 80.0); self._claim(2, 40.0)   # ...but $120 of claims are waiting
        st = rp.payout_status()
        self.assertEqual(st["waiting_usd"], 120.0)
        self.assertTrue(st["low"])

    def test_custom_level(self):
        self.bal = 1_000_000_000
        with mock.patch.dict(os.environ, {"REFERRAL_PAYOUT_LOW_USD": "200"}):
            self.assertTrue(rp.payout_status()["low"])

    def test_no_price_never_guesses(self):
        with mock.patch.object(price_fetcher, "get_price_usd", return_value=0):
            st = rp.payout_status()
        self.assertIsNone(st["low"]); self.assertIsNone(st["spend_usd"])
        self.assertIn("price unavailable", rp.status_text(st))

    def test_unreadable_wallet_reports_error_without_secret(self):
        with mock.patch.object(signer, "keypair_from_secret", side_effect=ValueError("bad")):
            st = rp.payout_status()
        self.assertEqual(st, {"error": "ValueError"})
        self.assertIn("could not be read", rp.status_text(st))

    def test_text_names_the_address_to_top_up_and_never_the_secret(self):
        txt = rp.status_text(rp.payout_status())
        self.assertIn("PAYOUTKEY", txt)
        with mock.patch.dict(os.environ, {"REFERRAL_PAYOUT_SECRET": "TOPSECRETVALUE"}):
            self.assertNotIn("TOPSECRETVALUE", rp.status_text(rp.payout_status()))

    def test_paid_claims_do_not_count_as_waiting(self):
        self._claim(1, 10.0)
        with db.get_conn() as c:
            c.execute("UPDATE referral_ledger SET status='paid'"); c.commit()
        self.assertEqual(db.claimed_unpaid_usd(), 0.0)


if __name__ == "__main__":
    unittest.main()
