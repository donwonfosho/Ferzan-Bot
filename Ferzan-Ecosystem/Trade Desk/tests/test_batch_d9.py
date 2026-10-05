import os, re, sys, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def src():
    with open(os.path.join(HERE, "signer.py")) as fh:
        return fh.read()


class SendSolEncoding(unittest.TestCase):
    def test_uses_base64_not_hex(self):
        s = src()
        body = s[s.index("def send_sol"):s.index("def sell_sol")]
        self.assertIn('"encoding": "base64"', body)
        self.assertNotIn('"hex"', body)
        self.assertNotIn(".hex()", body)

    def test_base64_round_trips(self):
        import base64
        raw = bytes(range(200))
        self.assertEqual(base64.b64decode(base64.b64encode(raw).decode()), raw)


class TipReporting(unittest.TestCase):
    def test_fallback_routes_do_not_report_a_tip(self):
        s = src()
        i = s.index("def _swap_send_sender")
        body = s[i:i + 1800]
        self.assertEqual(body.count('opts.pop("tip_paid", None)'), 2)  # build failed, and Sender refused
        j = s.index("priority fee (Jito didn't land, resent)")
        self.assertIn('opts.pop("tip_paid", None)', s[j:j + 200])


if __name__ == "__main__":
    unittest.main()
