"""Public status: normal / degraded / down, no leaks, never 'normal' when it could not check.

  cd "Launch Bot" && python -m unittest tests.test_status_check
"""
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import status_check as sc  # noqa: E402


def up(_name):
    return True


class StatusTests(unittest.TestCase):
    def test_all_up_is_normal(self):
        r = sc.build(active=up, solana=lambda u: True, now=1)
        self.assertEqual(r["status"], "normal")
        self.assertEqual(r["updated"], 1)

    def test_one_service_down_is_degraded(self):
        r = sc.build(active=lambda n: n != "ferzan-webapp", solana=lambda u: True)
        self.assertEqual(r["status"], "degraded")
        self.assertEqual([c["status"] for c in r["components"] if c["name"] == "Trade Bot"], ["down"])

    def test_solana_down_is_degraded(self):
        self.assertEqual(sc.build(active=up, solana=lambda u: False)["status"], "degraded")

    def test_trade_and_launch_both_down_is_down(self):
        down = {"ferzan-trade", "ferzan-trade-api", "ferzan-webapp", "ferzan-launch"}
        r = sc.build(active=lambda n: n not in down, solana=lambda u: True)
        self.assertEqual(r["status"], "down")

    def test_cannot_check_is_unknown_never_normal(self):
        r = sc.build(active=lambda n: None, solana=lambda u: None)
        by = {c["name"]: c["status"] for c in r["components"]}
        self.assertEqual(by["Trade Bot"], "unknown")
        self.assertEqual(by["Solana network"], "unknown")
        self.assertNotIn("down", by.values())

    def test_unknown_parts_do_not_hide_a_real_failure(self):
        r = sc.build(active=lambda n: None if n == "ferzan-launch" else (n != "ferzan-trade"), solana=lambda u: None)
        self.assertEqual(r["status"], "degraded")

    def test_no_names_or_urls_leak(self):
        r = sc.build(active=up, solana=lambda u: True, rpc_url="https://secret-rpc.example/KEY123")
        text = str(r)
        for bad in ("ferzan-", "secret-rpc", "KEY123", "http"):
            self.assertNotIn(bad, text)

    def test_wired_into_api(self):
        src = (HERE / "api.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/status")', src)
        self.assertIn("status_check", src)


if __name__ == "__main__":
    unittest.main()
