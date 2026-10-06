import os, re, sqlite3, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import x_poster


class NoJupiterLinks(unittest.TestCase):
    row = {"name": "Solana Classic", "symbol": "SLC", "chain": "solana", "token": "5f32oKpxNNLL69nV4LEut7ENRszgkKmExMe9TKJWZfzn", "curve": "c"}

    def test_x_post_links_to_the_site(self):
        for kind in ("launch", "p90", "koth", "grad"):
            text = x_poster._fmt(kind, self.row, "https://launch.ferzaneco.com/miniapp")
            self.assertIn("Trade: https://ferzan-factory.com/coin/solana/" + self.row["token"], text)
            self.assertNotIn("jup.ag", text)

    def test_other_chains_unchanged(self):
        r = dict(self.row, chain="base", token="0xabc", curve="0xdef")
        self.assertIn("curve.html?chain=base&curve=0xdef", x_poster._fmt("launch", r, "https://b"))

    def test_no_jupiter_trade_links_left_in_user_facing_code(self):
        for f in ("x_poster.py", "curve_indexer.py", "api.py", "miniapp/solana.html"):
            src = open(os.path.join(HERE, "..", f), encoding="utf-8").read()
            self.assertNotRegex(src, r"jup\.ag/tokens|Buy on Jupiter", f)


if __name__ == "__main__":
    unittest.main()
