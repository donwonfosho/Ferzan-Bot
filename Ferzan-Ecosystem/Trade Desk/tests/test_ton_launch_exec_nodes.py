"""The Trade Bot TON launch helper survives lagging public nodes (no network, nothing signed).

  cd "Trade Desk" && python -m unittest tests.test_ton_launch_exec_nodes
"""
import asyncio
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ton_launch_exec as ex  # noqa: E402
import ton_signer as t  # noqa: E402


class _Addr:
    def to_str(self, **k):
        return "EQminter"


async def _nosleep(*a, **k):
    return None


class Deployed(unittest.TestCase):
    def test_retries_on_other_nodes(self):
        n = {"c": 0}

        async def state(p, a):
            n["c"] += 1
            if n["c"] < 3:
                raise RuntimeError("cannot load block 651")
            return True, 1

        with mock.patch.object(ex, "_state", state), mock.patch("asyncio.sleep", _nosleep):
            self.assertTrue(asyncio.run(ex._deployed(None, _Addr())))

    def test_asks_tonapi_when_nodes_fail(self):
        async def state(p, a):
            raise RuntimeError("651")

        resp = types.SimpleNamespace(status_code=200, json=lambda: {"status": "active"})
        with mock.patch.object(ex, "_state", state), mock.patch("asyncio.sleep", _nosleep), \
                mock.patch("requests.get", return_value=resp):
            self.assertTrue(asyncio.run(ex._deployed(None, _Addr())))
        resp404 = types.SimpleNamespace(status_code=404, json=lambda: {})
        with mock.patch.object(ex, "_state", state), mock.patch("asyncio.sleep", _nosleep), \
                mock.patch("requests.get", return_value=resp404):
            self.assertFalse(asyncio.run(ex._deployed(None, _Addr())))

    def test_never_guesses(self):
        async def state(p, a):
            raise RuntimeError("651")

        with mock.patch.object(ex, "_state", state), mock.patch("asyncio.sleep", _nosleep), \
                mock.patch("requests.get", side_effect=OSError("down")):
            with self.assertRaises(RuntimeError):
                asyncio.run(ex._deployed(None, _Addr()))


class Info(unittest.TestCase):
    def _run(self, fast_addr, http_nano, lite=None):
        buf = io.StringIO()
        with mock.patch.object(ex, "_secret", return_value="s"), \
                mock.patch.object(t, "_ton_keypair_bytes", return_value=b"k" * 64), \
                mock.patch.object(t, "_offline_address", return_value=fast_addr), \
                mock.patch.object(t, "_http_balance_nano", return_value=http_nano), \
                mock.patch.object(t, "_run_retry", side_effect=lambda f, tries=3: lite), \
                redirect_stdout(buf):
            try:
                ex.info({"uid": 1, "need_nano": 600_000_000})
            except SystemExit:
                pass
        return json.loads(buf.getvalue().strip().splitlines()[-1])

    def test_http_balance_is_used_first(self):
        r = self._run("UQabc", 16_831_000_000)
        self.assertTrue(r["ok"])
        self.assertEqual(r["address"], "UQabc")
        self.assertAlmostEqual(r["balance_ton"], 16.831)
        self.assertTrue(r["enough"])

    def test_nodes_used_when_apis_silent(self):
        r = self._run("UQabc", None, lite=("UQabc", 200_000_000))
        self.assertTrue(r["ok"])
        self.assertFalse(r["enough"])  # 0.2 TON < 0.6 + 0.1 spare


if __name__ == "__main__":
    unittest.main()
