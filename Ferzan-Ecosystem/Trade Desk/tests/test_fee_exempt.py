import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import bridge
import feecollect


class Exempt(unittest.TestCase):
    def test_team_member_exempt_from_trade_and_bridge_fees(self):
        with mock.patch.dict(os.environ, {"FEE_EXEMPT_USER_IDS": "111,222"}):
            self.assertTrue(feecollect.exempt(222))
            self.assertFalse(feecollect.exempt(5))
            self.assertEqual(bridge.fee_bps(111), 0)

    def test_bridge_fee_still_charged_to_others(self):
        with mock.patch.dict(os.environ, {"FEE_EXEMPT_USER_IDS": "111", "BRIDGE_FEE_BPS": "25"}), \
                mock.patch.object(bridge, "wallet_addr", return_value=""):
            self.assertEqual(bridge.fee_bps(5), 25)


if __name__ == "__main__":
    unittest.main()
