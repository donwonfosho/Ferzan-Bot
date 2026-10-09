import os
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import launch_day as ld


class LaunchDay(unittest.TestCase):
    def setUp(self):
        ld._SHARED = {}

    def env(self, **kw):
        return mock.patch.dict(os.environ, kw, clear=True)

    def test_report_never_prints_values(self):
        secret = "SECRET-VALUE-12345"
        with self.env(TELEGRAM_BOT_TOKEN=secret, SOLANA_RPC_URL="https://rpc.test/?k=" + secret,
                      LAUNCH_DB_PATH="/nonexistent/x.db"), \
                mock.patch.object(ld, "_state", return_value=""), mock.patch.object(ld, "_http_ok", return_value=False):
            out = ld.report()
        self.assertNotIn(secret, out)
        self.assertIn("must-fix", out)

    def test_missing_admin_ids_is_must_fix(self):
        with self.env(LAUNCH_DB_PATH="/nonexistent/x.db"), mock.patch.object(ld, "_state", return_value=""), \
                mock.patch.object(ld, "_http_ok", return_value=True):
            rows = {label: s for s, label, _ in ld.checks()}
        self.assertEqual(rows["Admin alerts"], "bad")
        self.assertEqual(rows["Promo posts"], "warn")

    def test_launch_counts_and_stuck(self):
        f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        c = sqlite3.connect(f.name)
        c.execute("CREATE TABLE launch_requests (id TEXT, status TEXT, created_at TEXT)")
        c.executemany("INSERT INTO launch_requests VALUES (?, ?, datetime('now', ?))",
                      [("a", "confirmed", "-1 hours"), ("b", "failed", "-2 hours"), ("c", "pending", "-3 hours"),
                       ("d", "confirmed", "-40 hours")])
        c.commit(); c.close()
        by, stuck = ld._launch_counts(f.name)
        os.unlink(f.name)
        self.assertEqual((by, stuck), ({"confirmed": 1, "failed": 1, "pending": 1}, 1))

    def test_shared_env_file_counts(self):
        ld._SHARED = {"PLATFORM_TREASURY_EVM": "0xabc"}
        with self.env(LAUNCH_DB_PATH="/nonexistent/x.db"), mock.patch.object(ld, "_state", return_value=""), \
                mock.patch.object(ld, "_http_ok", return_value=True):
            rows = {label: s for s, label, _ in ld.checks()}
        self.assertEqual(rows["Fee wallet (EVM)"], "ok")

    def test_countdown_and_override(self):
        with self.env():  # no date set: nothing is scheduled
            self.assertEqual(ld.launch_at(), ld.DEFAULT_LAUNCH_AT)
            self.assertEqual(ld.countdown(), "date to be announced")
        with self.env(FERZAN_LAUNCH_AT="2026-11-13T21:00:00Z"):  # Fri Nov 13 2026, 4:00 PM EST
            at = ld.launch_at()
            self.assertEqual(at, 1794603600)
            self.assertEqual(ld.countdown(at + 5), "live now")
            self.assertEqual(ld.countdown(at - 90_000), "1d 1h 0m")


if __name__ == "__main__":
    unittest.main()
