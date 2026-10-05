import os, re, sys, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def body(name, nxt):
    with open(os.path.join(HERE, "curve_indexer.py")) as fh:
        s = fh.read()
    return s[s.index(f"def {name}"):s.index(nxt)]


class NoNetworkUnderLock(unittest.TestCase):
    def check(self, fn, nxt):
        b = body(fn, nxt)
        w = b.index("with idx_conn() as c:")
        # the lines of the with-block are indented 8+; the loop sending the outbox is back at 4
        block = []
        for line in b[w:].splitlines()[1:]:
            if line.strip() and not line.startswith("        ") and not line.startswith("\t"):
                break
            block.append(line)
        self.assertFalse([l for l in block if "_tg(" in l], fn)
        self.assertIn("    for chat, text, kb in outbox:\n        _tg(chat, text, kb)", b)

    def test_graduation(self):
        self.check("send_graduation_alerts", "# ---------------------------------------------------------- growth alerts")

    def test_growth(self):
        self.check("send_growth_alerts", "# ------------------------------------------------------------------- main --")


if __name__ == "__main__":
    unittest.main()
