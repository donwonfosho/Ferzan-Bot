import os, sys, unittest
from unittest import mock
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import feecollect


class FeeLedger(unittest.TestCase):
    def run_skim(self, send_result):
        rows = []
        with mock.patch.object(feecollect, "enabled", return_value=True), \
                mock.patch.object(feecollect, "exempt", return_value=False), \
                mock.patch.object(feecollect, "live_bps", return_value=100), \
                mock.patch("fees.fee_wallets", return_value={"sol": "Dest" + "1" * 40}), \
                mock.patch("signer.sol_usd", return_value=100.0), \
                mock.patch("withdraw.send_sol", return_value=send_result), \
                mock.patch.object(feecollect.db, "add_fee", side_effect=lambda *a, **k: rows.append((a, k))):
            out = feecollect.skim_buy(1, 100.0, "sol", sol_secret="s")
        return out, rows

    def test_confirmed_fee_is_counted(self):
        out, rows = self.run_skim((True, "sig"))
        self.assertTrue(out[0])
        self.assertEqual(rows[0][0][1], "live_buy")
        self.assertGreater(rows[0][0][4], 0)

    def test_unconfirmed_fee_is_not_called_failed_or_counted(self):
        out, rows = self.run_skim((None, "not confirmed"))
        self.assertEqual(out, (False, ""))
        self.assertEqual(rows[0][0][1], "live_buy_unconfirmed")
        self.assertEqual(rows[0][0][4], 0.0)
        self.assertIn("unconfirmed", rows[0][1]["note"])

    def test_clean_failure_is_failed(self):
        out, rows = self.run_skim((False, "no sol"))
        self.assertEqual(rows[0][0][1], "live_buy_failed")


class DeployPlain(unittest.TestCase):
    def test_pending_deploy_is_recorded_before_waiting(self):
        with open(os.path.join(HERE, "..", "Launch Bot", "scripts", "deploy_plain.py")) as fh:
            s = fh.read()
        self.assertLess(s.index('record[chain + "_pending"] = txh.hex()'), s.index("wait_for_transaction_receipt"))
        self.assertIn("an earlier run sent a deploy", s)
        self.assertIn("os.O_EXCL, 0o600", s)
        self.assertNotIn("os.chmod(f, 0o600)", s)


if __name__ == "__main__":
    unittest.main()
