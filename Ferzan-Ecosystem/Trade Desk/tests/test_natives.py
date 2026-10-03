import time
import unittest

import natives


class Natives(unittest.TestCase):
    def setUp(self):
        natives._CACHE.clear()
        natives._PENDING.clear()

    def test_parallel_failed_read_is_unknown_not_zero(self):
        def slow():
            time.sleep(0.2)
            return 1.5

        def boom():
            raise RuntimeError("rpc down")

        r = {"sol": ("A", slow), "eth": ("B", lambda: 0.0), "bsc": ("C", boom)}
        t0 = time.time()
        natives.prime(r)
        out = natives.collect(r, wait=2)
        self.assertLess(time.time() - t0, 0.6)  # ran together, not one after another
        self.assertEqual(out, {"sol": 1.5, "eth": 0.0, "bsc": None})

    def test_cache_and_address_key(self):
        calls = []

        def rd():
            calls.append(1)
            return 2.0

        natives.prime({"sol": ("A", rd)})
        natives.collect({"sol": ("A", rd)})
        natives.prime({"sol": ("A", rd)})  # fresh: no second call
        natives.collect({"sol": ("A", rd)})
        self.assertEqual(len(calls), 1)
        natives.prime({"sol": ("B", rd)})  # another wallet address: read again
        natives.collect({"sol": ("B", rd)})
        self.assertEqual(len(calls), 2)

    def test_slow_read_does_not_hold_the_bag(self):
        r = {"ton": ("T", lambda: (time.sleep(1.0), 3.0)[1])}
        natives.prime(r)
        t0 = time.time()
        out = natives.collect(r, wait=0.2)
        self.assertLess(time.time() - t0, 0.6)
        self.assertIsNone(out["ton"])

    def test_header_and_text(self):
        v = {"sol": 0.4972, "base": 0.04276, "eth": 0.0, "bsc": 0.12, "ton": None}
        line = natives.header_line(v)
        self.assertIn("0.4972 SOL", line)
        self.assertIn("0.0428 ETH Base", line)
        self.assertIn("0.12 BNB", line)
        self.assertIn("1 unread", line)
        txt = natives.balances_text(v, {"solana": 120.0, "ethereum": 2680.0, "binancecoin": 600.0}, "Main")
        self.assertIn("Total ≈ $", txt)
        self.assertIn("Couldn't read: TON", txt)


if __name__ == "__main__":
    unittest.main()
