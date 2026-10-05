import os, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import db
import sendstate
import sniper

TON = "EQ" + "A" * 46


class SnipeThroughFullPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        self.sid = db.add_snipe(user_id=5, query=TON, chain="ton", usd=5, min_liq=0, min_score=0, max_age_h=None, require_long=0)
        self.calls = []

    def tearDown(self):
        sniper.LIVE_BUY = None
        db.DB_PATH = self.old
        self.tmp.cleanup()
        sendstate.clear(5, TON)

    def status(self):
        with db.get_conn() as c:
            return c.execute("SELECT status FROM snipes WHERE id=?", (self.sid,)).fetchone()[0]

    def run_with(self, result):
        def hook(uid, card, mint, usd):
            self.calls.append((uid, mint, usd))
            return result
        sniper.LIVE_BUY = hook
        card = mock.Mock(); card.snapshot.token_address = TON
        with mock.patch.object(sniper, "inspect_target", return_value=card), \
                mock.patch.object(sniper, "gates_for", return_value=(True, "ok")), \
                mock.patch("user_wallets.secrets", return_value=("s", "e")), \
                mock.patch("signer.max_usd", return_value=50.0):
            return sniper.try_fill(db.active_snipes()[0])

    def test_non_evm_non_solana_mint_goes_to_the_full_path(self):
        st, _ = self.run_with((True, "Live TON buy"))
        self.assertEqual((st, self.status()), ("filled", "filled"))
        self.assertEqual(self.calls, [(5, TON, 5.0)])

    def test_unclear_result_is_held_and_not_retried(self):
        st, _ = self.run_with((False, "TON swap was accepted but not confirmed yet. Check before retrying."))
        self.assertEqual((st, self.status()), ("unconfirmed", "unconfirmed"))
        self.assertTrue(sendstate.held(5, TON, "buy"))

    def test_clean_refusal_stays_armed(self):
        st, _ = self.run_with((False, "Blocked: honeypot"))
        self.assertEqual((st, self.status()), ("armed", "armed"))

    def test_bot_wires_the_hook(self):
        s = open(os.path.join(HERE, "bot.py")).read()
        self.assertIn("sniper.LIVE_BUY = _snipe_buy", s)
        self.assertIn('fee_kind="snipe"', s[s.index("def _snipe_buy"):s.index("def _snipe_buy") + 400])


if __name__ == "__main__":
    unittest.main()
