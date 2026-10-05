import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class QuoteSide(unittest.TestCase):
    PAIR = {"chainId": "base", "dexId": "x", "pairAddress": "0xp", "priceUsd": "0.5", "priceNative": "0.0002",
            "baseToken": {"address": "0xBASE", "symbol": "AAA", "name": "A"},
            "quoteToken": {"address": "0xQUOTE", "symbol": "BBB", "name": "B"},
            "fdv": 999, "priceChange": {"h24": 50}, "txns": {"h1": {"buys": 7, "sells": 3}}}

    def test_base_side_unchanged(self):
        import price_fetcher as pf
        s = pf.snapshot_from_pair(self.PAIR, "q", "0xbase")
        self.assertEqual((s.symbol, s.price_usd, s.token_address, s.fdv), ("AAA", 0.5, "0xBASE", 999))

    def test_quote_side_is_flipped(self):
        import price_fetcher as pf
        s = pf.snapshot_from_pair(self.PAIR, "q", "0xquote")
        self.assertEqual((s.symbol, s.token_address), ("BBB", "0xQUOTE"))
        self.assertAlmostEqual(s.price_usd, 0.5 / 0.0002)
        self.assertEqual((s.fdv, s.change_24h, s.buys_h1, s.sells_h1), (0.0, 0.0, 3, 7))

    def test_no_token_means_base(self):
        import price_fetcher as pf
        self.assertEqual(pf.snapshot_from_pair(self.PAIR, "q").symbol, "AAA")


class Bridge(unittest.TestCase):
    def test_amount_is_exact(self):
        import bridge
        self.assertEqual(bridge._amount_raw("eth", "0.1"), str(10**17))
        self.assertEqual(bridge._amount_raw("eth", "1.1"), str(11 * 10**17))
        self.assertEqual(bridge._amount_raw("sol", "0.000000001"), "1")
        for bad in ("nan", "inf", "0", "-1", "abc", "1e9"):
            with self.assertRaises(ValueError):
                bridge._amount_raw("eth", bad)

    def test_dln_from_evm_uses_the_dln_tx(self):
        import bridge
        seen = {}
        with mock.patch.object(bridge, "_exec_evm", side_effect=lambda u, p, d: seen.setdefault("d", d) and "ok"):
            bridge.execute(1, {"src": "eth", "via": "dln", "raw": {"tx": {"to": "0xabc", "data": "0x", "value": "5"}}})
        self.assertEqual(bridge._evm_items(seen["d"])[0]["to"], "0xabc")

    def test_relay_from_evm_unchanged(self):
        import bridge
        raw = {"steps": [{"items": [{"data": {"to": "0xr"}}]}]}
        with mock.patch.object(bridge, "_exec_evm", return_value="ok") as m:
            bridge.execute(1, {"src": "eth", "via": "relay", "raw": raw})
        self.assertIs(m.call_args[0][2], raw)


if __name__ == "__main__":
    unittest.main()
