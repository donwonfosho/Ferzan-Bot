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


class TonCurveLookup(unittest.TestCase):
    """The Trade Bot finds a Ferzan TON curve coin from its coin address or its curve address."""

    def setUp(self):
        import sqlite3, tempfile, types
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        c = sqlite3.connect(self.tmp.name)
        c.execute("CREATE TABLE curves (chain TEXT, curve TEXT, token TEXT, name TEXT, symbol TEXT, price REAL, "
                  "mcap REAL, real_eth TEXT, graduated INTEGER)")
        self.curve, self.token = "EQ" + "C" * 46, "EQ" + "T" * 46
        c.execute("INSERT INTO curves VALUES ('ton', ?, ?, 'Herman', 'HTM', 0.000002, 2000.0, '3000000000000000000', 0)",
                  (self.curve, self.token))
        c.commit(); c.close()
        fake = types.ModuleType("ton_signer")
        fake._index_db = lambda: self.tmp.name
        fake._raw = lambda a: a.lower()
        sys.modules["ton_signer"] = fake
        self.p = mock.patch.object(pf, "get_price_usd", return_value=3.0)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        sys.modules.pop("ton_signer", None)
        os.unlink(self.tmp.name)

    def test_by_coin_and_by_curve(self):
        for addr in (self.token, self.curve):
            s = pf._ferzan_ton_curve_snap(addr)
            self.assertIsNotNone(s)
            self.assertEqual((s.symbol, s.chain, s.dex, s.token_address), ("HTM", "ton", "ferzan-curve", self.token))
            self.assertAlmostEqual(s.price_usd, 0.000006)
            self.assertAlmostEqual(s.liquidity_usd, 9.0)

    def test_unknown_address_is_none(self):
        self.assertIsNone(pf._ferzan_ton_curve_snap("EQ" + "Z" * 46))

    def test_load_market_routes_ton(self):
        with mock.patch.object(pf, "search_dex", return_value=None):
            s = pf.load_market(self.token)
        self.assertEqual(s.symbol, "HTM")
