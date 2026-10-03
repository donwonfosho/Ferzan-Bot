import pathlib
import tempfile
import unittest

import db


class HideToken(unittest.TestCase):
    def test_hide_and_unhide(self):
        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "t.db"
        db.init_db()
        db.set_flag(7, "hide:0xabc", True)
        db.set_flag(7, "hide:0xdef", True)
        db.set_flag(7, "onboarded", True)
        db.set_flag(8, "hide:0xabc", True)
        self.assertTrue(db.flag_on(7, "hide:0xabc", 0))
        self.assertEqual(db.clear_hidden(7), 2)
        self.assertFalse(db.flag_on(7, "hide:0xabc", 0))
        self.assertTrue(db.flag_on(7, "onboarded", 0))  # other flags untouched
        self.assertTrue(db.flag_on(8, "hide:0xabc", 0))  # other users untouched
        self.assertEqual(db.clear_hidden(7), 0)


if __name__ == "__main__":
    unittest.main()
