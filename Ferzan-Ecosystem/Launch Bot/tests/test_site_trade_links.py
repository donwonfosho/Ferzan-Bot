"""Trade links in channel posts, X posts and launch cards point at the website, never at the mini-app host.

  cd "Launch Bot" && python -m unittest tests.test_site_trade_links
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import curve_indexer  # noqa: E402
import x_poster  # noqa: E402


class SiteTradeLinks(unittest.TestCase):
    row = {"name": "Coin", "symbol": "CN", "token": "0xabc", "curve": "0xDEF0000000000000000000000000000000000001"}

    def test_x_post_links_the_website_for_every_curve_chain(self):
        for chain in ("base", "bsc", "ethereum", "robinhood", "arc", "ton"):
            text = x_poster._fmt("launch", dict(self.row, chain=chain), "https://old.example/miniapp")
            self.assertIn(f"https://ferzan-factory.com/coin/{chain}/", text, chain)
            self.assertNotIn("curve.html", text)
            self.assertNotIn("old.example", text)

    def test_evm_curve_address_is_lowercased_in_the_link(self):
        text = x_poster._fmt("launch", dict(self.row, chain="base"), "")
        self.assertIn("/coin/base/0xdef0000000000000000000000000000000000001", text)

    def test_indexer_trade_link_is_the_website(self):
        url = curve_indexer._trade_url(dict(self.row, chain="bsc"))
        self.assertEqual(url, "https://ferzan-factory.com/coin/bsc/0xdef0000000000000000000000000000000000001")

    def test_api_posts_have_no_miniapp_curve_link_left(self):
        for name in ("api.py", "curve_indexer.py", "x_poster.py"):
            src = (HERE / name).read_text(encoding="utf-8")
            self.assertNotIn("/curve.html?chain=", src, name)


if __name__ == "__main__":
    unittest.main()
