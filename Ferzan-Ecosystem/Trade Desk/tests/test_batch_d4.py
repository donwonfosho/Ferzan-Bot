import os, re, sys, unittest
from unittest import mock
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def quote(*amts):
    return "0x" + "20".zfill(64) + hex(len(amts))[2:].zfill(64) + "".join(hex(a)[2:].zfill(64) for a in amts)


class HoodMinOut(unittest.TestCase):
    def test_min_out_is_quote_less_slippage(self):
        import hood, evm_signer
        with mock.patch.object(evm_signer, "_rpc", return_value={"result": quote(10**18, 1000)}):
            self.assertEqual(hood._min_out("rpc", 10**18, ["0xa", "0xb"]), 800)

    def test_no_quote_aborts(self):
        import hood, evm_signer
        for body in ({"error": {"message": "revert"}}, {"result": "0x"}, {"result": quote(10**18, 0)}):
            with mock.patch.object(evm_signer, "_rpc", return_value=body), self.assertRaises(RuntimeError):
                hood._min_out("rpc", 10**18, ["0xa", "0xb"])

    def test_swaps_no_longer_send_zero(self):
        s = open(os.path.join(HERE, "hood.py")).read()
        self.assertNotIn('+ "0".zfill(64)  # amountOutMin', s)
        self.assertEqual(s.count("_min_out("), 3)  # def + buy + sell


class LimitBackoff(unittest.TestCase):
    def test_backoff_and_quiet_failures(self):
        s = open(os.path.join(HERE, "bot.py")).read()
        i = s.index("async def buy_limit_job")
        body = s[i:i + 2600]
        self.assertIn("if nxt > time.time()", body)
        self.assertIn("if fails not in (1, 5)", body)
        self.assertIn("_BL_FAIL.pop(lid", body)


if __name__ == "__main__":
    unittest.main()
