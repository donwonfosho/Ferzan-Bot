import os, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import db
import price_fetcher
import referral_pay as rp
import signer
import withdraw

DEST = "11111111111111111111111111111112"


class Pay(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        self.env = mock.patch.dict(os.environ, {"REFERRAL_AUTOPAY": "1", "REFERRAL_PAYOUT_SECRET": "x"}, clear=False)
        self.env.start()
        kp = mock.Mock(); kp.pubkey.return_value = "PAYOUT"
        self.patches = [
            mock.patch.object(signer, "keypair_from_secret", return_value=kp),
            mock.patch.object(withdraw, "sol_balance", return_value=5_000_000_000),
            mock.patch.object(price_fetcher, "get_price_usd", return_value=100.0),
            mock.patch.object(withdraw, "validate_address", return_value=(True, DEST)),
        ]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()
        self.env.stop(); db.DB_PATH = self.old_path; self.tmp.cleanup()

    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {"REFERRAL_AUTOPAY": "0"}):
            self.assertEqual(rp.try_pay(1, 10, DEST)[0], "declined")

    def test_pays_the_right_amount_once(self):
        with mock.patch.object(withdraw, "send_sol", return_value=(True, "https://solscan.io/tx/abc")) as s:
            v, _ = rp.try_pay(1, 10, DEST)
        self.assertEqual(v, "paid")
        self.assertEqual(s.call_args[0][2], 100_000_000)  # $10 at $100/SOL = 0.1 SOL
        with mock.patch.object(withdraw, "send_sol") as s2:
            self.assertEqual(rp.try_pay(1, 10, DEST)[0], "declined")  # same user inside 24h
            s2.assert_not_called()

    def test_limits(self):
        with mock.patch.object(withdraw, "send_sol") as s:
            self.assertEqual(rp.try_pay(1, 51, DEST)[0], "declined")  # over single max
            s.assert_not_called()
        with mock.patch.object(withdraw, "send_sol", return_value=(True, "ok")):
            for uid in range(10, 14):
                self.assertEqual(rp.try_pay(uid, 50, DEST)[0], "paid")  # 4 x 50 = 200 daily cap
        with mock.patch.object(withdraw, "send_sol") as s:
            self.assertEqual(rp.try_pay(99, 5, DEST)[0], "declined")
            s.assert_not_called()

    def test_failed_send_declines_and_does_not_count(self):
        with mock.patch.object(withdraw, "send_sol", return_value=(False, "expired")):
            self.assertEqual(rp.try_pay(1, 10, DEST)[0], "declined")
        self.assertEqual(rp.paid_since(), 0.0)

    def test_unconfirmed_or_crashed_send_is_unsure_and_counts(self):
        with mock.patch.object(withdraw, "send_sol", return_value=(None, "pending")):
            self.assertEqual(rp.try_pay(1, 10, DEST)[0], "unsure")
        with mock.patch.object(withdraw, "send_sol", side_effect=RuntimeError("boom")):
            self.assertEqual(rp.try_pay(2, 10, DEST)[0], "unsure")
        self.assertEqual(rp.paid_since(), 20.0)

    def test_low_wallet_and_no_price_decline(self):
        with mock.patch.object(withdraw, "sol_balance", return_value=50_000_000), mock.patch.object(withdraw, "send_sol") as s:
            self.assertEqual(rp.try_pay(1, 10, DEST)[0], "declined"); s.assert_not_called()
        with mock.patch.object(price_fetcher, "get_price_usd", return_value=0), mock.patch.object(withdraw, "send_sol") as s:
            self.assertEqual(rp.try_pay(1, 10, DEST)[0], "declined"); s.assert_not_called()


if __name__ == "__main__":
    unittest.main()
