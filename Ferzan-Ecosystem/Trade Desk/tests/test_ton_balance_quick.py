import sys
import types
import unittest
from unittest import mock

sys.modules.setdefault("pytoniq_core", types.ModuleType("pytoniq_core"))
import ton_signer as t  # noqa: E402


class BalanceQuick(unittest.TestCase):
    def test_http_only_with_short_timeout(self):
        with mock.patch.object(t, "_ton_keypair_bytes", return_value=b"x" * 64), \
             mock.patch.object(t, "_offline_address", return_value="UQabc"), \
             mock.patch.object(t, "_http_balance_nano", return_value=2_500_000_000) as h:
            self.assertEqual(t.balance_quick("s"), 2.5)
            self.assertEqual(h.call_args.kwargs.get("timeout"), 4)

    def test_none_when_apis_silent(self):
        with mock.patch.object(t, "_ton_keypair_bytes", return_value=b"x" * 64), \
             mock.patch.object(t, "_offline_address", return_value="UQabc"), \
             mock.patch.object(t, "_http_balance_nano", return_value=None):
            self.assertIsNone(t.balance_quick("s"))


if __name__ == "__main__":
    unittest.main()
