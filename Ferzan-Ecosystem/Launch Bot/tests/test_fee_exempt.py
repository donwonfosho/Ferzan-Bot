import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ferzan_perks as fp


class Exempt(unittest.TestCase):
    def test_listed_team_member_pays_no_launch_fee(self):
        with mock.patch.dict(os.environ, {"FEE_EXEMPT_USER_IDS": "111, 222;333"}):
            self.assertEqual(fp.launch_fee_lamports("W", 50_000_000, 222)[0], 0)
            self.assertIn("team", fp.launch_fee_lamports("W", 50_000_000, 333)[1].lower())

    def test_everyone_else_pays(self):
        with mock.patch.dict(os.environ, {"FEE_EXEMPT_USER_IDS": "111"}), \
                mock.patch.object(fp, "perks", return_value={"launch_fee_off_pct": 0}):
            self.assertEqual(fp.launch_fee_lamports("W", 50_000_000, 999)[0], 50_000_000)
            self.assertEqual(fp.launch_fee_lamports("W", 50_000_000)[0], 50_000_000)  # site launches: no id

    def test_bad_ids_never_exempt(self):
        with mock.patch.dict(os.environ, {"FEE_EXEMPT_USER_IDS": "111"}):
            for bad in (0, -1, None, "abc"):
                self.assertFalse(fp.fee_exempt(bad))


if __name__ == "__main__":
    unittest.main()
