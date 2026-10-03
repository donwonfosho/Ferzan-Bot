import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ton_signer as t


class R:
    def __init__(self, code=200, data=None):
        self.status_code, self._d = code, data or {}

    def json(self):
        return self._d


class JettonAmount(unittest.TestCase):
    def get(self, resp):
        return mock.patch.object(t.requests, "get", return_value=resp)

    def test_uses_decimals_from_the_answer(self):
        with self.get(R(200, {"balance": "2500000", "jetton": {"decimals": 6}})):
            self.assertEqual(t.jetton_amount_pub("UQx", "EQj"), 2.5)

    def test_no_jetton_wallet_is_zero(self):
        with self.get(R(404)):
            self.assertEqual(t.jetton_amount_pub("UQx", "EQj"), 0.0)

    def test_unreadable_is_none_not_zero(self):
        with self.get(R(500)):
            self.assertIsNone(t.jetton_amount_pub("UQx", "EQj"))
        with mock.patch.object(t.requests, "get", side_effect=OSError("down")):
            self.assertIsNone(t.jetton_amount_pub("UQx", "EQj"))

    def test_holding_falls_back_to_http_after_liteserver_failures(self):
        with mock.patch.object(t, "_ton_keypair_bytes", return_value=b"k" * 64), \
                mock.patch.object(t, "_run_async", side_effect=RuntimeError("cannot load block")), \
                mock.patch.object(t.time, "sleep"), \
                mock.patch.object(t, "_offline_address", return_value="UQowner"), \
                self.get(R(200, {"balance": "4000000000", "jetton": {"decimals": 9}})):
            self.assertEqual(t.jetton_holding("secret", "EQj"), (4.0, "UQowner"))

    def test_holding_raises_when_nothing_answers(self):
        with mock.patch.object(t, "_ton_keypair_bytes", return_value=b"k" * 64), \
                mock.patch.object(t, "_run_async", side_effect=RuntimeError("cannot load block")), \
                mock.patch.object(t.time, "sleep"), \
                mock.patch.object(t, "_offline_address", return_value=None):
            with self.assertRaises(RuntimeError):
                t.jetton_holding("secret", "EQj")

    def test_remembered_address_is_reused(self):
        seed = b"a" * 32 + b"b" * 32
        t._remember_addr(seed, "UQremembered")
        self.assertEqual(t._offline_address(seed), "UQremembered")


class MiniAppPositions(unittest.TestCase):
    def test_ton_position_with_pnl(self):
        import portfolio as p

        with mock.patch.object(p.db, "live_mints", return_value=["EQtok", "So11111111111111111111111111111111111111112"]), \
                mock.patch.object(p.db, "get_ton_addr", return_value="UQowner"), \
                mock.patch.object(p.db, "live_cost", return_value=1.5), \
                mock.patch.object(t, "jetton_amount_pub", return_value=1000.0), \
                mock.patch.object(p, "_market", return_value={"price": 0.003, "symbol": "FGRID", "chg24": 2.0, "url": "u"}):
            out = p._ton_tron_positions(7, "")
        self.assertEqual(len(out), 1)
        pos = out[0]
        self.assertEqual((pos["chain"], pos["symbol"], pos["amount"]), ("TON", "FGRID", 1000.0))
        self.assertAlmostEqual(pos["value"], 3.0)
        self.assertAlmostEqual(pos["pnl"], 1.5)
        self.assertAlmostEqual(pos["pnl_pct"], 100.0)

    def test_no_saved_address_means_no_ton_rows_and_no_crash(self):
        import portfolio as p

        with mock.patch.object(p.db, "live_mints", return_value=["EQtok"]), mock.patch.object(p.db, "get_ton_addr", return_value=""):
            self.assertEqual(p._ton_tron_positions(7, ""), [])


if __name__ == "__main__":
    unittest.main()
