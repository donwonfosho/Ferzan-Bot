import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import trust


class LaunchDate(unittest.TestCase):
    def test_no_date_means_to_be_announced(self):
        with mock.patch.dict(os.environ, {"FERZAN_LAUNCH_AT": ""}), mock.patch.object(trust, "_env_launch", return_value=""):
            self.assertFalse(trust.is_scheduled())
            self.assertEqual(trust.countdown(), "date to be announced")
            self.assertEqual(trust.label_et(), "")

    def test_set_date(self):
        with mock.patch.object(trust, "_env_launch", return_value="2026-11-13T21:00:00Z"):
            at = trust.launch_at()
            self.assertEqual(at, 1794603600)
            self.assertEqual(trust.label_et(), "Friday Nov 13, 4:00 PM ET")
            self.assertEqual(trust.countdown(at - 90_000), "1d 1h 0m")
            self.assertEqual(trust.countdown(at + 1), "live now")

    def test_typo_is_no_date(self):
        with mock.patch.object(trust, "_env_launch", return_value="soon"):
            self.assertFalse(trust.is_scheduled())


if __name__ == "__main__":
    unittest.main()
