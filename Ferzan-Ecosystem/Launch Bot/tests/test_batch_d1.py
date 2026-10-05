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



class InternalGate(unittest.TestCase):
    def test_gate_rejects_proxied_and_compares_constant_time(self):
        with open(os.path.join(HERE, "api.py"), encoding="utf-8") as f:
            s = f.read()
        i = s.index("def _internal_ok")
        body = s[i:i + 1200]
        self.assertIn("compare_digest", body)
        self.assertIn("x-forwarded-for", body)
        self.assertIn("_public_rate_ok(request, \"call-credit\"", s)



class LaunchHardening(unittest.TestCase):
    def read(self, n):
        with open(os.path.join(HERE, n), encoding="utf-8") as f:
            return f.read()

    def test_launch_request_hides_telegram_ids(self):
        s = self.read("api.py")
        i = s.index('@app.get("/api/launch-requests/{request_id}")')
        body = s[i:i + 600]
        self.assertIn('pop("telegram_user_id"', body)
        self.assertIn('pop("chat_id"', body)

    def test_open_endpoints_are_rate_limited(self):
        s = self.read("api.py")
        for bucket in ("build-tx", "site-launch", "sol-fees", "call-credit"):
            self.assertIn(f'_public_rate_ok(request, "{bucket}"', s)
        self.assertIn('split(",")[-1]', s)  # the address our own proxy saw, not a client-supplied one

    def test_db_waits_for_locks(self):
        self.assertIn("busy_timeout", self.read("launch_bot_db.py"))

    def test_huge_time_and_nan_rejected(self):
        from launch_extras import parse_when
        when, err = parse_when("in 99999999999999 d", "UTC")
        self.assertIsNone(when)
        self.assertTrue(err)
        self.assertIn("is_finite", self.read("launch_app.py"))


if __name__ == "__main__":
    unittest.main()
