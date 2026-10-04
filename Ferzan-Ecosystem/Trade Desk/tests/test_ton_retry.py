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


class TonBalanceRead(unittest.TestCase):
    """The wallet balance check before a curve trade survives bad nodes and falls back to HTTP."""

    class _Addr:
        def to_str(self, **k):
            return "UQtest"

    def test_retries_then_succeeds(self):
        import asyncio
        n = {"c": 0}

        class P:
            async def get_account_state(self, a):
                n["c"] += 1
                if n["c"] < 3:
                    raise RuntimeError("cannot load block")
                import types
                return types.SimpleNamespace(balance=5_000_000_000)

        async def nosleep(*a, **k):
            return None

        with mock.patch("asyncio.sleep", nosleep):
            self.assertEqual(asyncio.run(t._ton_balance(P(), self._Addr())), 5_000_000_000)

    def test_http_backup_when_all_nodes_fail(self):
        import asyncio

        class P:
            async def get_account_state(self, a):
                raise RuntimeError("651")

        async def nosleep(*a, **k):
            return None

        with mock.patch("asyncio.sleep", nosleep), mock.patch.object(t, "_http_balance_nano", return_value=7_000_000_000):
            self.assertEqual(asyncio.run(t._ton_balance(P(), self._Addr())), 7_000_000_000)

    def test_raises_when_nobody_answers(self):
        import asyncio

        class P:
            async def get_account_state(self, a):
                raise RuntimeError("651")

        async def nosleep(*a, **k):
            return None

        with mock.patch("asyncio.sleep", nosleep), mock.patch.object(t, "_http_balance_nano", return_value=None):
            with self.assertRaises(RuntimeError):
                asyncio.run(t._ton_balance(P(), self._Addr()))
