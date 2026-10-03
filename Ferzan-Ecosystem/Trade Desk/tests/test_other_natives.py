import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import natives
import portfolio


class OtherNatives(unittest.TestCase):
    def setUp(self):
        natives._CACHE.clear()
        natives._PENDING.clear()

    def test_lists_only_held_chains_prices_them_and_counts_failures(self):
        def bal(chain, addr):
            if chain == "bsc":
                return 0.12, "BNB"
            if chain == "arb":
                raise RuntimeError("rpc down")
            return 0.0, "ETH"

        with mock.patch.object(portfolio.db, "get_ton_addr", return_value="UQ-ton"), \
                mock.patch.object(portfolio.evm_signer, "native_balance", side_effect=bal), \
                mock.patch("ton_signer._http_balance_nano", return_value=3_000_000_000), \
                mock.patch("tron_signer._trx_balance", return_value=0), \
                mock.patch.object(portfolio, "_price", side_effect=lambda g: {"binancecoin": 600.0, "the-open-network": 2.0}.get(g)):
            out, miss = portfolio._other_natives(1, "0x" + "ab" * 20)
        got = {o["key"]: o for o in out}
        self.assertEqual(set(got), {"bsc", "ton"})
        self.assertAlmostEqual(got["bsc"]["usd"], 72.0)
        self.assertEqual(got["ton"]["amount"], 3.0)
        self.assertEqual(miss, 1)  # Arbitrum could not be read: counted, never shown as empty


if __name__ == "__main__":
    unittest.main()
