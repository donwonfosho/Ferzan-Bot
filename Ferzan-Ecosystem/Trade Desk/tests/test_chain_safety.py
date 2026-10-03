import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import chain_safety as cs
import evm_security as es
import ton_signer as t


class R:
    def __init__(self, code, data): self.status_code, self._d = code, data
    def json(self): return self._d


class Ton(unittest.TestCase):
    def line(self, data, code=200):
        with mock.patch.object(cs.requests, "get", return_value=R(code, data)):
            return cs.ton_line("EQabc")

    def test_blacklist_is_danger(self):
        self.assertIn("🚨", self.line({"verification": "blacklist"}))

    def test_mintable_admin_is_flagged(self):
        out = self.line({"verification": "none", "mintable": True, "admin": {"address": "x"}})
        self.assertTrue(out.startswith("⚠️"))

    def test_clean_jetton_is_ok_but_unverified_is_said(self):
        out = self.line({"verification": "none", "mintable": False, "holders_count": 1200})
        self.assertTrue(out.startswith("✅"))
        self.assertIn("not on the TON verified list", out)

    def test_lookup_failure_gives_no_green_check(self):
        self.assertEqual(self.line({}, 500), "")
        with mock.patch.object(cs.requests, "get", side_effect=OSError("down")):
            self.assertEqual(cs.ton_line("EQabc"), "")


class Keys(unittest.TestCase):
    def test_tonapi_key_never_goes_to_toncenter(self):
        with mock.patch.dict(os.environ, {"TONAPI_KEY": "SECRET1", "TONCENTER_API_KEY": "SECRET2"}):
            self.assertEqual(t._headers()["Authorization"], "Bearer SECRET1")
            self.assertEqual(t._toncenter_headers(), {"X-API-Key": "SECRET2"})
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(t._toncenter_headers(), {})


class Tron(unittest.TestCase):
    def test_tron_honeypot_flagged(self):
        addr = "T" + "a" * 33
        body = {"result": {addr: {"is_honeypot": "1", "buy_tax": "0", "sell_tax": "0"}}}
        with mock.patch.object(es.requests, "get", return_value=R(200, body)):
            self.assertIn("HONEYPOT", es.security_line("trx", addr))

    def test_tron_unknown_gives_nothing(self):
        with mock.patch.object(es.requests, "get", return_value=R(200, {"result": {}})):
            self.assertEqual(es.security_line("trx", "T" + "a" * 33), "")


if __name__ == "__main__":
    unittest.main()
