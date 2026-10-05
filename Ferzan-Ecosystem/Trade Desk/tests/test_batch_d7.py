import os, sys, tempfile, unittest
from pathlib import Path
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


class PaperRace(unittest.TestCase):
    def setUp(self):
        import db
        self.db = db
        self.old = db.DB_PATH
        db.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"
        db.init_db()
        with db.get_conn() as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(positions)")]
            vals = {"user_id": 1, "symbol": "X", "query": "x", "entry": 1.0, "qty": 10.0, "side": "long", "opened_at": 1}
            vals = {k: v for k, v in vals.items() if k in cols}
            c.execute(f"INSERT INTO positions ({','.join(vals)}) VALUES ({','.join('?' * len(vals))})", list(vals.values()))
            c.commit()
            self.pid = c.execute("SELECT id FROM positions").fetchone()[0]

    def tearDown(self):
        self.db.DB_PATH = self.old

    def test_only_one_close_wins(self):
        self.assertTrue(self.db.claim_position_close(self.pid, 1))
        self.assertFalse(self.db.claim_position_close(self.pid, 1))

    def test_other_users_cannot_close(self):
        self.assertFalse(self.db.claim_position_close(self.pid, 2))

    def test_partial_sell_needs_unchanged_size(self):
        self.assertTrue(self.db.set_position_qty_if(self.pid, 1, 10.0, 5.0))
        self.assertFalse(self.db.set_position_qty_if(self.pid, 1, 10.0, 5.0))  # second tap saw the old size
        self.assertTrue(self.db.set_position_qty_if(self.pid, 1, 5.0, 2.5))

    def test_closed_position_cannot_shrink(self):
        self.db.claim_position_close(self.pid, 1)
        self.assertFalse(self.db.set_position_qty_if(self.pid, 1, 10.0, 5.0))


class Wiring(unittest.TestCase):
    def test_panic_and_error_handler(self):
        s = open(os.path.join(HERE, "bot.py")).read()
        i = s.index("async def panic_cb")
        body = s[i:i + 3200]
        self.assertIn("age <= 600", body)
        self.assertIn("try:\n                status, detail = await _sell_everywhere", body)
        self.assertIn("app.add_error_handler(_on_error)", s)
        self.assertIn('callback_data=f"pnc:go:{int(time.time())}"', s)

    def test_paper_close_claims_before_crediting(self):
        s = open(os.path.join(HERE, "trading.py")).read()
        i = s.index("def paper_close(")
        self.assertLess(s.index("claim_position_close", i), s.index("update_user(user_id, paper_cash", i))


if __name__ == "__main__":
    unittest.main()
