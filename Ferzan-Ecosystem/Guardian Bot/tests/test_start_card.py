import ast, os, re, unittest

SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "guardian_bot.py"), encoding="utf-8").read()


class StartCard(unittest.TestCase):
    def test_text_fits_a_caption_and_mentions_the_new_checks(self):
        fn = next(n for n in ast.parse(SRC).body if isinstance(n, ast.FunctionDef) and n.name == "_start_text")
        ns = {}
        exec(compile(ast.Module([fn], []), "g", "exec"), ns)
        t = ns["_start_text"]()
        self.assertLessEqual(len(t), 1024)
        self.assertIn("Contract guard", t)
        self.assertIn("New-member check", t)

    def test_buy_button_opens_the_buy_bot(self):
        m = re.search(r'"🟢 Ferzan Buy", url=f"https://t\.me/\{(\w+)\}"', SRC)
        self.assertTrue(m and m.group(1) == "BUY")


if __name__ == "__main__":
    unittest.main()
