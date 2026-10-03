import os, sys, unittest
from unittest import mock
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ton_signer as t


class Retry(unittest.TestCase):
    def test_lagging_node_before_send_is_retried(self):
        calls = []

        async def coro():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("code 651: cannot load block")
            return "ok"

        with mock.patch("time.sleep"):
            self.assertEqual(t._run_retry(lambda: coro()), "ok")
        self.assertEqual(len(calls), 3)

    def test_never_retries_after_broadcast(self):
        calls = []

        async def coro():
            calls.append(1)
            t._BCAST["n"] += 1
            raise RuntimeError("cannot load block")

        with mock.patch("time.sleep"), self.assertRaises(RuntimeError):
            t._run_retry(lambda: coro())
        self.assertEqual(len(calls), 1)

    def test_real_errors_are_not_retried(self):
        calls = []

        async def coro():
            calls.append(1)
            raise ValueError("bad key")

        with self.assertRaises(ValueError):
            t._run_retry(lambda: coro())
        self.assertEqual(len(calls), 1)

    def test_message_is_not_buy_specific(self):
        self.assertNotIn("buy again", t._friendly_err(RuntimeError("651")))


if __name__ == "__main__":
    unittest.main()
