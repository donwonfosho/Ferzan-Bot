import ast
import pathlib
import unittest

SRC = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text()


def _load():
    tree = ast.parse(SRC)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_sellx_pct")
    ns = {"_fmt_amt": lambda v: f"{v:g}"}
    exec(compile(ast.Module([fn], []), "bot.py", "exec"), ns)
    return ns["_sellx_pct"]


class SellXPct(unittest.TestCase):
    def setUp(self):
        self.f = _load()

    def test_percent(self):
        self.assertEqual(self.f("pct", 40, 0, 0, 0)[0], 40)
        self.assertIsNone(self.f("pct", 150, 0, 0, 0)[0])

    def test_tokens(self):
        self.assertEqual(self.f("tok", 250, 1000, 1, 0)[0], 25)
        self.assertIsNone(self.f("tok", 2000, 1000, 1, 0)[0])  # more than held: refuse
        self.assertIsNone(self.f("tok", 5, 0, 1, 0)[0])  # nothing held

    def test_native(self):
        # bag worth $100, coin $10, want 2 coins = $20 -> 20%
        self.assertEqual(self.f("nat", 2, 100, 1.0, 10.0)[0], 20)
        self.assertIsNone(self.f("nat", 50, 100, 1.0, 10.0)[0])  # $500 > $100
        self.assertIsNone(self.f("nat", 1, 100, 0, 10.0)[0])  # no token price: never guess
        self.assertIsNone(self.f("nat", 1, 100, 1.0, 0)[0])  # no coin price


class PanelWiring(unittest.TestCase):
    def test_buttons_and_handlers_exist(self):
        for cb in ('sxa:pct:', 'sxa:nat:', 'sxa:tok:', 'sxa:lim:', 'sli:', 'xslip:', 'xgas:', 'bagh:', 'go:wallets'):
            self.assertIn(cb, SRC)
        self.assertIn('data.startswith("sxa:")', SRC)
        self.assertIn('"sellx"', SRC)

    def test_pending_prompt_expires(self):
        self.assertIn("> 120", SRC)


if __name__ == "__main__":
    unittest.main()
