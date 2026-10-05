import re, unittest
from pathlib import Path
SRC = (Path(__file__).resolve().parent.parent / "buy_bot.py").read_text()


def _ns(price=0.0):
    import time
    ns = {"time": time, "_DEX_WRAPPED": {"bsc": "0xbb"}, "_dex_usd": lambda a: price}
    a = SRC.index("_BNB_CACHE = {")
    exec(SRC[a:SRC.index("def _tier_label")], ns)
    return ns


class Quote(unittest.TestCase):
    def test_each_chain_pays_in_its_own_coin(self):
        q = _ns(600.0)["_native_quote"]
        self.assertEqual(q("sol", 150.0, 3000.0), (150.0, "SOL"))
        self.assertEqual(q("solana", 150.0, 3000.0), (150.0, "SOL"))
        self.assertEqual(q("bsc", 150.0, 3000.0), (600.0, "BNB"))
        self.assertEqual(q("bnb", 150.0, 3000.0), (600.0, "BNB"))
        for c in ("eth", "base", "arb", "evm"):
            self.assertEqual(q(c, 150.0, 3000.0), (3000.0, "ETH"))

    def test_bnb_price_unknown_is_zero_not_eth(self):
        self.assertEqual(_ns(0.0)["_native_quote"]("bsc", 150.0, 3000.0), (0.0, "BNB"))


class Wiring(unittest.TestCase):
    def test_paid_hash_is_case_folded_and_verification_off_the_loop(self):
        self.assertIn("tx = tx.lower()", SRC)
        self.assertIn("await asyncio.to_thread(_verify_sol_tx, tx)", SRC)
        self.assertIn("await asyncio.to_thread(_verify_evm_tx, tx, chain)", SRC)

    def test_no_eth_price_used_for_bnb(self):
        self.assertNotIn('sym = "SOL" if is_sol else "ETH"', SRC)


class Ads(unittest.TestCase):
    def test_ads_are_off_until_they_work(self):
        self.assertIn('FERZAN_ADS_LIVE", "0"', SRC)
        self.assertIn("if not ADS_LIVE:", SRC.split("async def _start_ads_flow")[1][:400])


if __name__ == "__main__":
    unittest.main()
