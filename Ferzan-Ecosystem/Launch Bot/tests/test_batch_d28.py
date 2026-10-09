import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import ast, html


def _load():
    """Pull start_caption out of launch_bot.py by itself, so the test needs no Telegram libraries."""
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "launch_bot.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "start_caption")
    ns = {"_esc": lambda t: html.escape(str(t))}
    exec(compile(ast.Module([fn], []), "launch_bot", "exec"), ns)
    return ns["start_caption"]


class StartCaption(unittest.TestCase):
    def test_caption_is_current_and_fits_a_photo_caption(self):
        cap = _load()("Solana · Ethereum · BNB Chain")
        self.assertLessEqual(len(cap), 1024)
        self.assertIn("Live now: Solana · Ethereum · BNB Chain", cap)
        self.assertIn("never holds your keys", cap)
        self.assertIn("ferzan-factory.com", cap)
        for stale in ("pump.fun", "testnet", "Hood", "@ferzaneco"):
            self.assertNotIn(stale, cap)

    def test_html_in_chain_names_is_escaped(self):
        self.assertNotIn("<script>", _load()("<script>"))


if __name__ == "__main__":
    unittest.main()
