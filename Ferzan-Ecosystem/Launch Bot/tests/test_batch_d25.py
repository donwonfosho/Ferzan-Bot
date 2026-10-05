import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import launch_reserved as r


class Reserved(unittest.TestCase):
    def test_brand_and_lookalikes_blocked(self):
        for n, s in [("Ferzan", "ABC"), ("FERZAN token", "X1"), ("Fer zan", "ZZ"), ("F3rzan", "ZZ"), ("Ferzann", "ZZ"),
                     ("Ferzna", "ZZ"), ("Ferzam Coin", "ZZ"), ("Moon", "FERZAN"), ("Moon", "$FERZ4N"), ("The Ferzan Project", "TFP")]:
            self.assertIsNotNone(r.problem(n, s), (n, s))

    def test_majors_and_stables_blocked(self):
        for s in ("USDC", "usdt", "$SOL", "ETH", "WBTC"):
            self.assertIsNotNone(r.problem("Anything", s), s)

    def test_normal_names_allowed(self):
        for n, s in [("Moon Cat", "MCAT"), ("Pepe Reborn", "PEPE2"), ("Fern", "FERN"), ("Ferrari Fan", "FFAN"),
                     ("Zanzibar", "ZANZ"), ("Solar Panel", "SLR"), ("Tether Cat", "TCAT")]:
            self.assertIsNone(r.problem(n, s), (n, s))

    def test_env_extra(self):
        os.environ["LAUNCH_RESERVED"] = "ELON,TRUMP"
        try:
            self.assertIsNotNone(r.problem("x", "ELON"))
        finally:
            del os.environ["LAUNCH_RESERVED"]

    def test_wired_into_site_and_bot_not_flagship(self):
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        self.assertIn("_reserved.problem(name, symbol)", open(os.path.join(here, "api.py")).read())
        lb = open(os.path.join(here, "launch_bot.py")).read()
        self.assertGreaterEqual(lb.count("_reserved.problem("), 3)
        self.assertNotIn("launch_reserved", open(os.path.join(here, "ferzan_flagship.py")).read())


if __name__ == "__main__":
    unittest.main()
