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

    def test_app_button_only_in_private_chat_and_only_when_served(self):
        tree = ast.parse(SRC)
        fns = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in ("_app_url", "_start_kb", "_menu_kb")]
        self.assertEqual(len(fns), 3)
        self.assertIn("web_app=WebAppInfo", SRC)
        kb = next(n for n in fns if n.name == "_start_kb")
        self.assertEqual([a.arg for a in kb.args.args], ["private"])
        src = ast.get_source_segment(SRC, kb)
        self.assertIn("if private and _app_url()", src)
        self.assertIn("_start_kb(private)", SRC)


if __name__ == "__main__":
    unittest.main()
