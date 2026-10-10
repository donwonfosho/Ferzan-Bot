import os, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.pop("PROMO_LIVE", None)
import ferzan_promo as fp
import ferzan_media as fm


class PromoSet(unittest.TestCase):
    def test_every_promo_has_its_image(self):
        for i in range(len(fp.PROMOS)):
            self.assertIsNotNone(fm.img(f"promo_{i + 1:02d}.jpg"), f"promo_{i + 1:02d}.jpg missing")

    def test_video_promo_has_video_and_poster(self):
        self.assertIsNotNone(fm.vid("promo_44.mp4"))
        self.assertIsNotNone(fm.img("promo_44.jpg"))
        self.assertGreaterEqual(len(fp.PROMOS), 44)

    def test_no_duplicate_copy_and_x_fits(self):
        tgs = [p[0] for p in fp.PROMOS]; xs = [p[1] for p in fp.PROMOS]
        self.assertEqual(len(set(tgs)), len(tgs)); self.assertEqual(len(set(xs)), len(xs))
        for n in range(len(fp.GENERAL_TAGS)):
            for _, x, tags in fp.PROMOS[38:]:
                out = fp.with_tags(x, tags, fp.GENERAL_TAGS[n])
                self.assertLessEqual(fp.x_len(out), 280, out)
                self.assertIn("#", out)

    def test_new_copy_is_distinct_per_image(self):
        new = [p[0] for p in fp.PROMOS[38:]]
        firsts = {t.split(" ")[0] for t in new}
        self.assertEqual(len(firsts), len(new))  # a different opening hook each time


class VideoPosting(unittest.TestCase):
    def test_video_failure_falls_back_to_image_then_text(self):
        calls = []
        fm_tg_photo, fm_post = fm.tg_photo, fm.requests.post
        fm.tg_photo = lambda chat, text, image: calls.append(("photo", image.name if image else None)) or True
        class R: status_code = 500
        fm.requests.post = lambda *a, **k: R()
        os.environ["LAUNCHBOT_TOKEN"] = "x"
        try:
            self.assertTrue(fm.tg_video("@c", "hi", fm.vid("promo_44.mp4"), fm.img("promo_44.jpg")))
            self.assertEqual(calls, [("photo", "promo_44.jpg")])
        finally:
            fm.tg_photo, fm.requests.post = fm_tg_photo, fm_post
            os.environ.pop("LAUNCHBOT_TOKEN", None)

    def test_post_uses_video_when_present(self):
        seen = []
        orig = fm.tg_video, fm.tg_photo
        fm.tg_video = lambda chat, t, v, p: seen.append(("video", v.name)) or True
        fm.tg_photo = lambda chat, t, p: seen.append(("photo", p.name if p else None)) or True
        os.environ["FERZAN_ADMIN_IDS"] = "1"
        try:
            s = {"done": [], "x_log": []}
            fp.post(s, "t1", "text", None, image="promo_44.jpg", video="promo_44.mp4")
            fp.post(s, "t2", "text", None, image="promo_43.jpg", video="promo_43.mp4")
            self.assertEqual(seen, [("video", "promo_44.mp4"), ("photo", "promo_43.jpg")])
        finally:
            fm.tg_video, fm.tg_photo = orig
            os.environ.pop("FERZAN_ADMIN_IDS", None)



class SkipList(unittest.TestCase):
    def setUp(self):
        os.environ.pop("PROMO_SKIP", None)

    def tearDown(self):
        os.environ.pop("PROMO_SKIP", None)

    def test_default_skips_older_duplicates_and_keeps_new_ones(self):
        sk = fp.skipped()
        self.assertEqual(sk, set(range(1, 51)))                 # the old 50 are retired
        self.assertTrue(set(range(51, 87)).isdisjoint(sk))     # the new 17 portrait graphics are the rotation
        seen, p = [], 0
        for _ in range(len(fp.PROMOS) - len(sk)):
            i = fp.next_promo(p); seen.append(i + 1); p = i + 1
        self.assertEqual(len(set(seen)), len(seen))   # one full pass, no repeats
        self.assertTrue(sk.isdisjoint(seen))

    def test_pointer_inside_a_skipped_run_moves_on(self):
        os.environ["PROMO_SKIP"] = "5,6,7"
        self.assertEqual(fp.next_promo(4) + 1, 8)

    def test_none_and_all_skipped(self):
        os.environ["PROMO_SKIP"] = "none"
        self.assertEqual(fp.next_promo(1), 1)
        os.environ["PROMO_SKIP"] = ",".join(str(n) for n in range(1, len(fp.PROMOS) + 1))
        self.assertEqual(fp.next_promo(3), 3)  # never stuck

    def test_wrap_around_end(self):
        os.environ["PROMO_SKIP"] = "86"
        self.assertEqual(fp.next_promo(85) + 1, 1)

    def test_old_launch_date_graphic_stays_out(self):
        self.assertIn(49, fp.skipped(0))
        self.assertIn(49, fp.skipped(fp.LAUNCH_AT + 60))
        os.environ["PROMO_SKIP"] = "none"
        self.assertEqual(fp.skipped(0), set())

    def test_rotation_size(self):
        self.assertEqual(len(fp.PROMOS), 86)
        self.assertEqual(len(fp.skipped(0)), 50)


class CarefulX(unittest.TestCase):
    def setUp(self):
        import x_poster
        self.xp = x_poster
        for k in ("X_PAUSED", "X_UNLIMITED", "PROMO_X_PER_DAY"):
            os.environ.pop(k, None)
        self.orig_file = x_poster.PAUSE_FILE
        x_poster.PAUSE_FILE = "/nonexistent/x-paused"

    def tearDown(self):
        self.xp.PAUSE_FILE = self.orig_file
        for k in ("X_PAUSED", "X_UNLIMITED", "PROMO_X_PER_DAY"):
            os.environ.pop(k, None)

    def test_spacing_and_daily_cap(self):
        now = 1_000_000.0
        self.assertTrue(fp.x_allowed({"x_log": []}, "promo:1", now))
        self.assertFalse(fp.x_allowed({"x_log": [now - 3600]}, "promo:2", now))        # 1h after the last one
        self.assertTrue(fp.x_allowed({"x_log": [now - 5 * 3600]}, "promo:2", now))     # 5h after
        three = [now - 20 * 3600, now - 15 * 3600, now - 9 * 3600]
        self.assertFalse(fp.x_allowed({"x_log": three}, "promo:3", now))               # 3 already today
        os.environ["PROMO_X_PER_DAY"] = "8"
        self.assertFalse(fp.x_allowed({"x_log": three}, "promo:3", now))               # env cannot raise the ceiling
        os.environ["X_UNLIMITED"] = "1"
        self.assertTrue(fp.x_allowed({"x_log": three}, "promo:3", now))

    def test_pause_and_minor_countdowns(self):
        now = 1_000_000.0
        os.environ["X_PAUSED"] = "1"
        self.assertFalse(fp.x_allowed({"x_log": []}, "promo:1", now))
        os.environ.pop("X_PAUSED")
        self.assertTrue(fp.x_allowed({"x_log": []}, "countdown:604800", now))          # 7 days: yes
        self.assertFalse(fp.x_allowed({"x_log": []}, "countdown:300", now))            # 5 minutes: Telegram only

    def test_x_gets_the_fuller_copy_when_it_fits(self):
        full = fp.x_text_for("Short and detailed enough.\nhttps://ferzan-factory.com", "tiny", "#a #b", "#c")
        self.assertIn("detailed", full)
        long_tg = "x" * 400
        self.assertEqual(fp.x_text_for(long_tg, "tiny", "#a", "#c").split("\n")[0], "tiny")
        for tg, x, tags in fp.PROMOS:
            self.assertLessEqual(fp.x_len(fp.x_text_for(tg, x, tags, "#crypto")), 280)

    def test_event_posts_capped_and_paused(self):
        self.assertEqual(self.xp.event_cap(), 3)
        os.environ["X_MAX_POSTS_PER_DAY"] = "10"
        try:
            self.assertEqual(self.xp.event_cap(), 4)
        finally:
            os.environ.pop("X_MAX_POSTS_PER_DAY")
        os.environ["X_PAUSED"] = "1"
        self.assertTrue(self.xp.paused())


if __name__ == "__main__":
    unittest.main()
