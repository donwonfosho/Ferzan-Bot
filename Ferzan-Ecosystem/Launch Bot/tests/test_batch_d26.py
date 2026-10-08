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
        self.assertTrue({2, 7, 38} <= sk)
        self.assertTrue(set(range(45, 51)).isdisjoint(sk))   # all six newest stay in rotation before launch
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
        os.environ["PROMO_SKIP"] = "50"
        self.assertEqual(fp.next_promo(49) + 1, 1)

    def test_launch_date_graphic_retires_after_launch(self):
        before, after = fp.LAUNCH_AT - 60, fp.LAUNCH_AT + 60
        self.assertNotIn(49, fp.skipped(before))
        self.assertIn(49, fp.skipped(after))
        os.environ["PROMO_SKIP"] = "none"
        self.assertEqual(fp.skipped(after), set())

    def test_rotation_size(self):
        self.assertEqual(len(fp.PROMOS), 50)
        self.assertEqual(len(fp.skipped(fp.LAUNCH_AT - 60)), 19)


if __name__ == "__main__":
    unittest.main()
