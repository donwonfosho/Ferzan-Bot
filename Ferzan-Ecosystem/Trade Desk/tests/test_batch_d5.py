import os, sys, tempfile, unittest
from pathlib import Path
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


class FeedAutoBuy(unittest.TestCase):
    def setUp(self):
        import db
        self.db = db
        self.old = db.DB_PATH
        db.DB_PATH = Path(tempfile.mkdtemp()) / "t.db"

    def tearDown(self):
        self.db.DB_PATH = self.old

    def test_daily_limit(self):
        for _ in range(3):
            self.assertTrue(self.db.feed_autobuy_take(7, 3))
        self.assertFalse(self.db.feed_autobuy_take(7, 3))
        self.assertTrue(self.db.feed_autobuy_take(8, 3))  # another user has their own count

    def test_zero_means_never(self):
        self.assertFalse(self.db.feed_autobuy_take(1, 0))

    def test_old_entries_expire(self):
        import sqlite3
        self.db.feed_autobuy_take(5, 1)
        c = sqlite3.connect(str(self.db.DB_PATH)); c.execute("UPDATE feed_autobuy_log SET ts = ts - 90000"); c.commit(); c.close()
        self.assertTrue(self.db.feed_autobuy_take(5, 1))


class Wiring(unittest.TestCase):
    def test_feed_path_needs_its_own_flag_and_limit(self):
        s = open(os.path.join(HERE, "bot.py")).read()
        i = s.index('db.flag_on(uid, "auto_buy", 0) and db.flag_on(uid, "feed_auto_buy", 0)')
        self.assertIn("feed_autobuy_take", s[i:i + 600])
        self.assertIn('"feed_auto_buy": 0', s)
        self.assertIn('"feedbuy"', s)


if __name__ == "__main__":
    unittest.main()
