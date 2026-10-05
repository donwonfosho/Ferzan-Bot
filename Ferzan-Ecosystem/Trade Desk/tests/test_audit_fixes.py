import os, re, sys, tempfile, threading, unittest
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import db
import alert_targets

BOT = (Path(__file__).resolve().parent.parent / "bot.py").read_text()


class Claim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        with db.get_conn() as c:
            for i, amt in enumerate((3.0, 4.5), 1):
                c.execute("INSERT INTO referral_ledger (user_id, kind, share_usd, volume_usd, status, created_at) VALUES (7,'share',?, 100, 'open', '2026-01-01')", (amt,))
            c.commit()

    def tearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def test_second_claim_gets_nothing(self):
        a1, up1 = db.claim_referral_batch(7)
        a2, up2 = db.claim_referral_batch(7)
        self.assertAlmostEqual(a1, 7.5)
        self.assertGreater(up1, 0)
        self.assertEqual((a2, up2), (0.0, 0))

    def test_parallel_claims_pay_once(self):
        out = []
        ts = [threading.Thread(target=lambda: out.append(db.claim_referral_batch(7)[0])) for _ in range(6)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertAlmostEqual(sum(out), 7.5)


class Bounds(unittest.TestCase):
    def test_alert_target_rejects_nan_inf(self):
        for t in ("nan%", "inf%", "-inf%", "0%", "-100%"):
            self.assertIsInstance(alert_targets.parse_alert_target(t, 1.0, 1000000.0), str, t)

    def test_sl_tp_are_bounded(self):
        self.assertIn("0 < pct < 100", BOT)
        self.assertIn("0 < pct <= 10000", BOT)

    def test_bridge_quote_consumed_before_send(self):
        self.assertIn('context.user_data.pop("bridge_pack", None)', BOT)
        self.assertNotIn('pack = context.user_data.get("bridge_pack")', BOT)


if __name__ == "__main__":
    unittest.main()
