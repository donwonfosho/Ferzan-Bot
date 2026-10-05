import os, re, sys, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def read(rel):
    with open(os.path.join(HERE, rel)) as fh:
        return fh.read()


def clip_fn():
    s = read("solana_launch.py")
    i = s.index("def clip_utf8"); j = s.index("def _create_metadata_instruction")
    ns = {}
    exec(s[i:j], ns)
    return ns["clip_utf8"]


class Clip(unittest.TestCase):
    def test_ascii_and_short_untouched(self):
        c = clip_fn()
        self.assertEqual(c("Ferzan", 32), "Ferzan")
        self.assertEqual(c("", 10), "")
        self.assertEqual(c(None, 10), "")

    def test_never_exceeds_bytes_or_splits_a_character(self):
        c = clip_fn()
        for text in ("😀" * 20, "é" * 40, "日本語" * 20, "a😀" * 15):
            for limit in (10, 32):
                out = c(text, limit)
                self.assertLessEqual(len(out.encode()), limit)
                out.encode().decode()  # still valid UTF-8
                self.assertTrue(text.startswith(out))

    def test_uses_the_clip_for_metadata(self):
        s = read("solana_launch.py")
        self.assertIn("clip_utf8(name, 32)", s)
        self.assertIn("clip_utf8(symbol, 10)", s)
        self.assertNotIn("name[:32]", s)


class FeeWalletRequired(unittest.TestCase):
    def test_all_three_launch_paths_refuse_without_a_treasury(self):
        self.assertIn("fee_lamports > 0 and not treasury", read("solana_launch.py"))
        self.assertIn("_fee_lamports > 0 and not _treasury", read("meteora_launch.py"))
        self.assertIn("fee > 0 && !inp.treasury", read("dbc/build_launch.mjs"))

    def test_js_clip_present(self):
        s = read("dbc/build_launch.mjs")
        self.assertIn("clipUtf8(inp.name, 32)", s)
        self.assertIn("clipUtf8(inp.symbol, 10)", s)


if __name__ == "__main__":
    unittest.main()
