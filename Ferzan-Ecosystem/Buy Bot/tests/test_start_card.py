import ast, os, re, unittest

SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "buy_bot.py"), encoding="utf-8").read()


def _const(name):
    for n in ast.parse(SRC).body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == name for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError(name + " not found")


class BuyStart(unittest.TestCase):
    def test_welcome_is_short_and_current(self):
        t = _const("START_TEXT")
        self.assertLessEqual(len(t), 1000)
        self.assertIn("/setup", t)
        self.assertIn("10 chains", t)
        self.assertNotIn("@ferzaneco", t)

    def test_full_command_list_is_kept_and_under_telegram_limit(self):
        h = _const("FULL_HELP")
        self.assertLess(len(h), 4096)
        for cmd in ("/setup", "/add base 0xCA 25", "/raid", "/trending", "/sellalerts"):
            self.assertIn(cmd, h)

    def test_help_command_and_button_show_the_full_list(self):
        self.assertRegex(SRC, r"async def help_cmd[^\n]*\n    await update\.effective_message\.reply_text\(FULL_HELP\)")
        self.assertIn('pattern=r"^bb:cmds$"', SRC)
        self.assertIn('callback_data="bb:cmds"', SRC)

    def test_deep_link_still_handled_before_the_welcome(self):
        body = SRC.split("async def start(update")[1].split("async def setup_start")[0]
        self.assertLess(body.index('arg.startswith("trk_")'), body.index("START_TEXT"))


    def test_app_button_private_only_and_only_when_served(self):
        tree = ast.parse(SRC)
        kb = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_start_kb")
        self.assertEqual([a.arg for a in kb.args.args], ["private"])
        self.assertIn("if private and _app_url()", ast.get_source_segment(SRC, kb))
        self.assertIn("web_app=WebAppInfo", SRC)
        self.assertIn("_start_kb(private)", SRC)

    def test_tape_off_is_not_turned_back_on(self):
        # `int(row[0] or 1)` read a stored 0 (OFF) as 1 (ON), so /tape off never stopped the posts.
        self.assertNotIn("int(row[0] or 1)", SRC)
        self.assertIn("1 if row[0] is None else int(row[0])", SRC)


if __name__ == "__main__":
    unittest.main()
