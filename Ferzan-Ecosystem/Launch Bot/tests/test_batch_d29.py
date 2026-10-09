"""One launch date for everything: FERZAN_LAUNCH_AT. Unset means nothing launch-related runs."""
import calendar, os, sys, tempfile, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.pop("PROMO_LIVE", None)
import ferzan_when as fw

NOV13 = calendar.timegm((2026, 11, 13, 21, 0, 0))  # Fri Nov 13 2026, 4:00 PM EST


class When(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(fw.parse("2026-11-13T21:00:00Z"), NOV13)
        self.assertEqual(fw.parse("2026-11-13T16:00:00-05:00"), NOV13)
        self.assertEqual(fw.parse(str(NOV13)), NOV13)
        for bad in ("", "soon", "2026-13-45", "123", "99999999999999"):
            self.assertEqual(fw.parse(bad), fw.NOT_SET, bad)   # a typo means no date, never a wrong one
        self.assertFalse(fw.is_set(fw.NOT_SET))
        self.assertTrue(fw.is_set(NOV13))

    def test_label_is_eastern_standard_time(self):
        self.assertEqual(fw.label_et(NOV13), "Friday Nov 13, 4:00 PM Eastern")
        self.assertEqual(fw.label_et(fw.NOT_SET), "date to be announced")

    def test_env_wins_and_unset_is_off(self):
        with mock.patch.dict(os.environ, {"FERZAN_LAUNCH_AT": "2026-11-13T21:00:00Z"}):
            self.assertEqual(fw.launch_at(), NOV13)
        with mock.patch.dict(os.environ, {"FERZAN_LAUNCH_AT": ""}), mock.patch.object(fw, "_env", return_value=""):
            self.assertEqual(fw.launch_at(), fw.NOT_SET)


class EverythingFollowsIt(unittest.TestCase):
    def test_promo_with_no_date_has_no_countdown_ramp_or_quiet_window(self):
        import ferzan_promo as fp
        with mock.patch.object(fp, "LAUNCH_AT", fw.NOT_SET):
            posted = []
            with mock.patch.object(fp, "post", lambda *a, **k: posted.append(a[1])):
                fp.countdown({"done": [], "x_log": []}, 1_790_000_000)
                fp.countdown({"done": [], "x_log": []}, NOV13)
            self.assertEqual(posted, [])
            self.assertFalse(fp.ramp(NOV13 - 3600))

    def test_promo_countdown_names_the_set_date(self):
        import ferzan_promo as fp
        posted = []
        with mock.patch.object(fp, "LAUNCH_AT", NOV13), mock.patch.object(fp, "post", lambda s, key, text, x, **k: posted.append(text)):
            fp.countdown({"done": [], "x_log": []}, NOV13 - 7 * 86400 + 60)
        self.assertEqual(len(posted), 1)
        self.assertIn("Friday Nov 13, 4:00 PM Eastern", posted[0])
        self.assertNotIn("Oct 15", posted[0])

    def test_flagship_refuses_without_a_date(self):
        import ferzan_flagship as ff
        said = []
        with mock.patch.object(ff, "LAUNCH_AT", fw.NOT_SET), mock.patch.object(ff, "notify", said.append), \
                mock.patch.object(ff, "load", side_effect=AssertionError("must not even load state")):
            self.assertEqual(ff.launch(), 1)
        self.assertIn("no launch date is set", said[0])

    def test_flagship_refuses_the_old_date_window(self):
        import ferzan_flagship as ff
        said = []
        with mock.patch.object(ff, "LAUNCH_AT", NOV13), mock.patch.object(ff, "notify", said.append), \
                mock.patch("time.time", return_value=calendar.timegm((2026, 10, 15, 20, 0, 5))):
            self.assertEqual(ff.launch(), 1)
        self.assertIn("not launch time", said[0])

    def test_flywheel_skips_buying_until_a_date_passes(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ferzan_flywheel.py"), encoding="utf-8").read()
        self.assertIn("LAUNCH_AT = ferzan_when.launch_at()", src)
        self.assertIn('"skipBuy": now < LAUNCH_AT + 3600', src)

    def test_no_old_date_left_in_live_code(self):
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
        for f in ("ferzan_promo.py", "ferzan_flagship.py", "ferzan_flywheel.py", "ferzan_refill.py", "ferzan_watchdog.py", "launch_bot.py", "api.py"):
            src = open(os.path.join(here, f), encoding="utf-8").read()
            self.assertNotIn("(2026, 10, 15", src, f)
            self.assertNotIn("1792094400", src, f)

    def test_countdown_graphics_carry_the_new_date(self):
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "make_promo_images.py"), encoding="utf-8").read()
        self.assertIn("Friday, November 13", src)


if __name__ == "__main__":
    unittest.main()
