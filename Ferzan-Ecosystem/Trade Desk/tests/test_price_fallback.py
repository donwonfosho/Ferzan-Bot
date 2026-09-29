import sys, os, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import requests
import price_fetcher as pf


class R:
    def __init__(self, code=200, data=None):
        self.status_code, self._d = code, data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._d


def dex(addr, px, liq):
    return {"pairs": [{"baseToken": {"address": addr}, "priceUsd": str(px), "liquidity": {"usd": liq}},
                      {"baseToken": {"address": addr}, "priceUsd": "1", "liquidity": {"usd": 10}}]}


class Fallback(unittest.TestCase):
    def setUp(self):
        pf._LAST.clear()

    def get(self, cg, dx):
        def fake(url, **kw):
            return cg if "coingecko" in url else dx
        return mock.patch.object(pf.requests, "get", side_effect=fake)

    def test_coingecko_ok(self):
        with self.get(R(200, {"binancecoin": {"usd": 600.0}}), R()):
            self.assertEqual(pf.get_price_usd("binancecoin"), 600.0)

    def test_403_falls_back_to_dexscreener_best_liquidity(self):
        a = pf._WRAPPED["binancecoin"]
        with self.get(R(403), R(200, dex(a, 612.5, 5_000_000))):
            self.assertEqual(pf.get_price_usd("binancecoin"), 612.5)

    def test_thin_pool_rejected(self):
        a = pf._WRAPPED["solana"]
        with self.get(R(403), R(200, dex(a, 99, 1000))):
            with self.assertRaises(pf.PriceFetchError):
                pf.get_price_usd("solana")

    def test_last_good_used_then_expires(self):
        with self.get(R(200, {"ethereum": {"usd": 3000.0}}), R()):
            pf.get_price_usd("ethereum")
        with self.get(R(403), R(500)):
            self.assertEqual(pf.get_price_usd("ethereum"), 3000.0)
            pf._LAST["ethereum"] = (pf._LAST["ethereum"][0] - 4000, 3000.0)
            with self.assertRaises(pf.PriceFetchError):
                pf.get_price_usd("ethereum")

    def test_unknown_coin_raises_never_guesses(self):
        with self.get(R(403), R(500)):
            with self.assertRaises(pf.PriceFetchError):
                pf.get_price_usd("some-unknown-coin")

    def test_batch_fills_gaps(self):
        a = pf._WRAPPED["binancecoin"]
        with self.get(R(403), R(200, dex(a, 610.0, 9_000_000))):
            out = pf.get_prices_usd(["binancecoin"])
        self.assertEqual(out, {"binancecoin": 610.0})


if __name__ == "__main__":
    unittest.main()
