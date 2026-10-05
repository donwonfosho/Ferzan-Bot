import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


class MasterKey(unittest.TestCase):
    def setUp(self):
        import user_wallets
        self.uw = user_wallets
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / ".master"
        self.env = mock.patch.dict(os.environ, {"FERZAN_MASTER_KEY": ""})
        self.env.start()
        self.p = mock.patch.object(self.uw, "MASTER_PATH", self.path)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.env.stop()

    def test_refuses_to_replace_key_when_wallets_exist(self):
        with mock.patch.object(self.uw.db, "any_wallets", return_value=True):
            with self.assertRaises(RuntimeError):
                self.uw._fernet()
        self.assertFalse(self.path.exists())

    def test_first_key_is_private(self):
        with mock.patch.object(self.uw.db, "any_wallets", return_value=False):
            self.uw._fernet()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        first = self.path.read_text()
        with mock.patch.object(self.uw.db, "any_wallets", return_value=True):
            self.uw._fernet()  # reuses the file
        self.assertEqual(self.path.read_text(), first)



class SourceGuards(unittest.TestCase):
    def read(self, n):
        with open(os.path.join(HERE, n), encoding="utf-8") as f:
            return f.read()

    def test_curve_buy_needs_a_quote(self):
        s = self.read("evm_signer.py")
        self.assertIn("Couldn't get a price quote from the curve", s)
        self.assertNotIn("min_out = 0 if quoted <= 0", s)

    def test_tron_dup_counts_as_sent(self):
        self.assertIn("DUP_TRANSACTION_ERROR", self.read("tron_signer.py"))

    def test_tron_launch_fee_never_guessed(self):
        s = self.read("tron_launch_exec.py")
        self.assertNotIn('launchFeeSun()") or "0"', s)
        self.assertEqual(s.count("_launch_fee(ts, factory)"), 3)

    def test_wallet_creation_locked(self):
        self.assertIn("_CREATE_LOCK", self.read("user_wallets.py"))



class SpendCeiling(unittest.TestCase):
    def read(self, n):
        with open(os.path.join(HERE, n), encoding="utf-8") as f:
            return f.read()

    def test_no_5000_clamp_and_new_default(self):
        for n in ("signer.py", "evm_signer.py"):
            s = self.read(n)
            self.assertNotIn("min(5000.0", s, n)
            self.assertIn('"SIGNER_MAX_USD", "25000"', s, n)

    def test_defaults_do_not_use_the_ceiling_as_a_size(self):
        s = self.read("bot.py")
        self.assertNotIn("usd = float(signer.max_usd())", s)
        self.assertNotIn("usd=float(signer.max_usd())", s)
        self.assertNotIn("else float(signer.max_usd())", s)

    def test_large_buy_needs_second_tap(self):
        import time as _t
        s = self.read("bot.py")
        i = s.index("_LARGE_BUY_USD = ")
        j = s.index("def _live_buy_followup(")
        ns = {"os": os, "time": _t}
        exec(s[i:j], ns)
        ask = ns["_large_buy_ask"]
        self.assertEqual(ask(1, "MINT", 500), "")            # small: straight through
        self.assertIn("Nothing was sent", ask(1, "MINT", 3000))  # large: asks first
        self.assertEqual(ask(1, "MINT", 3000), "")           # same buy again: goes
        self.assertIn("Nothing was sent", ask(1, "MINT", 3000))  # and it asks again next time
        self.assertIn("Nothing was sent", ask(1, "MINT", 4000))  # a different size asks again


if __name__ == "__main__":
    unittest.main()
