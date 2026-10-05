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



class StartupResilience(unittest.TestCase):
    def test_polling_retries_on_telegram_blip(self):
        root = os.path.dirname(HERE)
        for rel in ("Launch Bot/launch_bot.py", "Trade Desk/bot.py", "Guardian Bot/guardian_bot.py",
                    "Liquidity Bot/liq_bot.py", "Buy Bot/buy_bot.py"):
            with open(os.path.join(root, rel), encoding="utf-8") as f:
                s = f.read()
            self.assertIn("bootstrap_retries=-1", s, rel)
            self.assertIn("'NetworkError'", s, rel)


if __name__ == "__main__":
    unittest.main()
