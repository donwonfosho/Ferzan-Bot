import os, re, tempfile, threading, unittest
from pathlib import Path
import statefile

ROOT = Path(__file__).resolve().parent.parent


def src(n):
    return (ROOT / n).read_text()


class StateFile(unittest.TestCase):
    def test_missing_gives_default(self):
        d = tempfile.mkdtemp()
        self.assertEqual(statefile.read_json(f"{d}/x.json", dict), {})

    def test_corrupt_raises(self):
        d = tempfile.mkdtemp(); p = f"{d}/x.json"; Path(p).write_text("{broken")
        with self.assertRaises(statefile.StateCorrupt):
            statefile.read_json(p, dict)

    def test_atomic_write_roundtrip_and_mode(self):
        d = tempfile.mkdtemp(); p = f"{d}/s/x.json"
        statefile.write_json(p, {"a": 1})
        self.assertEqual(statefile.read_json(p, dict), {"a": 1})
        self.assertEqual(oct(os.stat(p).st_mode & 0o777), "0o600")
        self.assertFalse(Path(p + ".tmp").exists())

    def test_lock_blocks_second_holder(self):
        d = tempfile.mkdtemp(); p = f"{d}/x.json"
        with statefile.lock(p):
            with self.assertRaises(statefile.Busy):
                with statefile.lock(p):
                    pass
        with statefile.lock(p):
            pass


class Wiring(unittest.TestCase):
    def test_flywheel_marks_started_before_money_moves(self):
        s = src("ferzan_flywheel.py")
        self.assertLess(s.index('"started": time.time()'), s.index("flywheel.mjs"))
        self.assertIn("statefile.lock", s)
        self.assertIn("StateCorrupt", s)

    def test_refill_saves_after_each_send(self):
        s = src("ferzan_refill.py")
        self.assertLess(s.index("used[chain] += amount"), s.index("statefile.write_json(STATE, st)  # the daily cap"))
        self.assertIn("StateCorrupt", s)

    def test_promo_saves_before_posting(self):
        s = src("ferzan_promo.py")
        i = s.index("def post(")
        self.assertLess(s.index("save(s)", i), s.index("fm.tg_photo(os.environ", i))

    def test_flagship_marks_posted_before_sending(self):
        s = src("ferzan_flagship.py")
        self.assertLess(s.index('s["live_posted"] = True'), s.index("fm.tg_photo(chat, text, pic)"))
        self.assertEqual(s.count('s["live_posted"] = True'), 1)


if __name__ == "__main__":
    unittest.main()
