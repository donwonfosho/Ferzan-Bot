import os, sys, unittest
from unittest import mock
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def goplus(blob):
    r = mock.Mock(); r.json.return_value = {"result": {"0xabc": blob}}
    return mock.patch("requests.get", return_value=r)


class Honeypot(unittest.TestCase):
    def line(self, blob):
        import evm_security
        with goplus(blob):
            return evm_security.security_line("eth", "0xabc")

    def test_missing_verdict_is_not_called_clean(self):
        out = self.line({"holder_count": "5"})
        self.assertIn("unavailable", out)
        self.assertNotIn("✅", out)
        self.assertIn("buy ?%", out)

    def test_clean_needs_an_explicit_zero(self):
        out = self.line({"is_honeypot": "0", "buy_tax": "0", "sell_tax": "0"})
        self.assertIn("✅ No honeypot flag", out)
        self.assertIn("buy 0%", out)

    def test_honeypot_still_flagged(self):
        self.assertIn("HONEYPOT RISK", self.line({"is_honeypot": "1"}))

    def test_flags_without_verdict(self):
        out = self.line({"is_mintable": "1"})
        self.assertIn("honeypot check unavailable", out)


class Decimals(unittest.TestCase):
    def setUp(self):
        import ton_signer
        self.t = ton_signer
        self.t._DECIMALS.clear()

    def resp(self, code, body):
        r = mock.Mock(); r.status_code = code; r.json.return_value = body
        return r

    def test_second_source_used_when_first_fails(self):
        seq = [self.resp(500, {}), self.resp(200, {"metadata": {"decimals": "6"}})]
        with mock.patch.object(self.t.requests, "get", side_effect=seq):
            self.assertEqual(self.t._jetton_decimals("EQa"), 6)
        self.assertEqual(self.t._DECIMALS["EQa"], 6)

    def test_guess_is_not_remembered(self):
        with mock.patch.object(self.t.requests, "get", side_effect=RuntimeError("down")):
            self.assertEqual(self.t._jetton_decimals("EQb"), 9)
        self.assertNotIn("EQb", self.t._DECIMALS)

    def test_first_source(self):
        with mock.patch.object(self.t.requests, "get", return_value=self.resp(200, {"asset": {"decimals": 6}})):
            self.assertEqual(self.t._jetton_decimals("EQc"), 6)


if __name__ == "__main__":
    unittest.main()
