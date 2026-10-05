import os
import sys
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import redact  # noqa: E402


class Scrub(unittest.TestCase):
    def test_query_keys(self):
        t = "403 for https://x.io/v2?apikey=ABCDEF123&chainid=1 and ?api-key=zzz999"
        o = redact.scrub(t)
        self.assertNotIn("ABCDEF123", o)
        self.assertNotIn("zzz999", o)
        self.assertIn("chainid=1", o)

    def test_bot_token(self):
        self.assertNotIn("AAH", redact.scrub("https://api.telegram.org/bot123456789:AAHdkfjskdfjhsdkfjhsdkfj/sendMessage"))

    def test_env_value(self):
        os.environ["TEST_SECRET_KEY"] = "supersecretvalue123"
        self.assertNotIn("supersecretvalue123", redact.scrub("failed at supersecretvalue123 host"))

    def test_path_key(self):
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456",
                         redact.scrub("POST https://rpc.example.com/abcdefghijklmnopqrstuvwxyz123456 failed"))

    def test_plain_text_untouched(self):
        self.assertEqual(redact.scrub("Insufficient funds for gas"), "Insufficient funds for gas")



class BotGates(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "bot.py"), encoding="utf-8") as f:
            self.s = f.read()

    def test_operator_fails_closed(self):
        self.assertIn("return bool(ops) and user_id in ops", self.s)

    def test_sponsor_operator_only_and_validated(self):
        i = self.s.index("async def sponsor_cmd")
        body = self.s[i:i + 1800]
        self.assertIn("_is_operator(update.effective_user.id)", body)
        self.assertIn("https://", body)

    def test_imports_private_only(self):
        for fn in ("importsol_cmd", "importevm_cmd"):
            i = self.s.index("async def " + fn)
            self.assertIn('!= "private"', self.s[i:i + 500])


if __name__ == "__main__":
    unittest.main()
