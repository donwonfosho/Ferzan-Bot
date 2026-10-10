import os, re, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import fundlinks as f

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A = {"sol": "SoLaddr1", "evm": "0xAbC", "ton": "UQton", "trx": "Ttron"}


class Funds(unittest.TestCase):
    def test_buy_link_carries_address_and_code(self):
        o = f.options("base", A)
        self.assertIn("walletAddress=0xAbC", o["buy"][0]["url"])
        self.assertIn("currencyCode=eth_base", o["buy"][0]["url"])
        self.assertTrue(o["buy"][0]["url"].startswith("https://buy.moonpay.com/"))

    def test_sell_link_has_no_address(self):
        for it in f.options("eth", A)["sell"]:
            self.assertNotIn("walletAddress", it["url"])
            self.assertTrue(it["url"].startswith("https://sell.moonpay.com/"))

    def test_missing_address_gives_no_buy_link(self):
        o = f.options("trx", {**A, "trx": ""})
        self.assertEqual(o["buy"], [])

    def test_each_chain_uses_its_own_address_kind(self):
        self.assertIn("SoLaddr1", f.options("sol", A)["buy"][0]["url"])
        self.assertIn("UQton", f.options("ton", A)["buy"][0]["url"])
        self.assertIn("Ttron", f.options("trx", A)["buy"][0]["url"])

    def test_bridge_only_chains_buy_on_base_then_bridge(self):
        os.environ.pop("FERZAN_FUND_CODES", None)
        for c in ("arc", "hood", "monad"):
            o = f.options(c, A)
            self.assertTrue(o["bridge_only"] and not o["buy"] and not o["sell"])
            self.assertIn("currencyCode=eth_base", o["via"]["buy"][0]["url"])
            self.assertIn("walletAddress=0xAbC", o["via"]["buy"][0]["url"])

    def test_unroutable_chains_are_marked_unsupported(self):
        for c in ("pulse", "stable"):
            self.assertTrue(f.options(c, A)["unsupported"])

    def test_env_code_makes_a_chain_direct(self):
        os.environ["FERZAN_FUND_CODES"] = "arc:usdc=usdc_arc, hood:eth=eth_robinhood, bad, x:y=z"
        try:
            o = f.options("arc", A)
            self.assertFalse(o["bridge_only"])
            self.assertIn("currencyCode=usdc_arc", o["buy"][0]["url"])
            self.assertFalse(f.options("hood", A)["bridge_only"])
            self.assertTrue(f.options("monad", A)["bridge_only"])
        finally:
            os.environ.pop("FERZAN_FUND_CODES")

    def test_bot_watcher_wired_and_bridge_chains_exist(self):
        bot = open(os.path.join(ROOT, "bot.py")).read()
        self.assertIn("_fund_watch", bot)
        self.assertIn('data.startswith("fd:w:")', bot)
        import bridge
        for c in f.BRIDGE_ONLY:
            if c not in f.NO_ROUTE:
                self.assertIn(c, bridge.CHAINS, c)

    def test_signature_only_with_both_keys(self):
        os.environ.pop("MOONPAY_SK", None); os.environ.pop("MOONPAY_PK", None); os.environ.pop("MOONPAY_KEY", None)
        self.assertNotIn("signature", f.moonpay_buy("eth", "0x1"))
        os.environ["MOONPAY_PK"] = "pk_test"; os.environ["MOONPAY_SK"] = "sk_test"
        try:
            self.assertIn("signature=", f.moonpay_buy("eth", "0x1"))
        finally:
            os.environ.pop("MOONPAY_PK"); os.environ.pop("MOONPAY_SK")

    def test_surfaces_wired(self):
        bot = open(os.path.join(ROOT, "bot.py")).read()
        self.assertIn('callback_data="go:cash"', bot)
        self.assertNotIn("Buy gas", bot)
        self.assertTrue(re.search(r'data\.startswith\("fd:"\)', bot))
        self.assertIn("/api/funds", open(os.path.join(ROOT, "webapp.py")).read())
        self.assertIn("fundBuyBtn", open(os.path.join(ROOT, "webapp/index.html")).read())

    def test_every_active_chain_is_covered(self):
        import chains
        covered = set(f.CHAINS) | set(f.BRIDGE_ONLY) | {"sol"}
        missing = [c for c in chains.ACTIVE if c not in covered]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
