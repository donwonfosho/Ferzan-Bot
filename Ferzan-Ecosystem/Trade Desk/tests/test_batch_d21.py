import os, sys, tempfile, unittest
from pathlib import Path
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import db


class FeedBuy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def test_flag_is_off_by_default_and_toggles(self):
        self.assertFalse(db.flag_on(5, "feed_auto_buy", 0))
        db.set_flag(5, "feed_auto_buy", True)
        self.assertTrue(db.flag_on(5, "feed_auto_buy", 0))
        db.set_flag(5, "feed_auto_buy", False)
        self.assertFalse(db.flag_on(5, "feed_auto_buy", 0))

    def test_endpoint_and_page_wired(self):
        w = Path(HERE, "webapp.py").read_text()
        self.assertIn('@app.post("/api/feedbuy")', w)
        self.assertIn('db.set_flag(uid, "feed_auto_buy", bool(body.get("on")))', w)
        # a change needs a fresh signed request, a read does not
        self.assertIn('max_age=ORDER_MAX_AGE_S if "on" in body else None', w[w.index("api_feedbuy"):])
        h = Path(HERE, "webapp", "index.html").read_text()
        self.assertIn('id="feedBtn"', h)
        self.assertIn('"/api/feedbuy"', h)

    def test_one_time_notice(self):
        b = Path(HERE, "bot.py").read_text()
        i = b.index('"feedbuy_notice"')
        self.assertLess(i, b.index('db.flag_on(uid, "feed_auto_buy", 0) and not _auto_trading_killed()'))
        self.assertIn('db.set_flag(uid, "feedbuy_notice", True)', b)


if __name__ == "__main__":
    unittest.main()
