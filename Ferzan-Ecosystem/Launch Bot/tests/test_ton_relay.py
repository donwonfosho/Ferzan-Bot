"""TON relay for the website: validation, key header, retry on 429, tonapi fallback (no network).

  cd "Launch Bot" && python -m unittest tests.test_ton_relay
"""
import base64
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def _stub_fastapi():
    try:
        import fastapi  # noqa: F401
        return
    except ImportError:
        pass
    m = types.ModuleType("fastapi")

    class HTTPException(Exception):
        def __init__(self, status_code, detail=None, headers=None):
            super().__init__(detail)
            self.status_code, self.detail = status_code, detail

    class APIRouter:
        def post(self, *a, **k):
            return lambda f: f
        get = post

    class Request:
        pass

    m.HTTPException, m.APIRouter, m.Request = HTTPException, APIRouter, Request
    sys.modules["fastapi"] = m


_stub_fastapi()
import ton_relay as R  # noqa: E402

UQ = "UQCUH5O7y33p4Y8GCZlqxEKTXY_sgnjW_LhK2cMGa4g-ZeoD"
BOC = base64.b64encode(b"\xb5\xee\x9c\x72" + b"\x01" * 20).decode()


class Req:
    headers = {"x-forwarded-for": "1.2.3.4"}
    client = None


class Resp:
    def __init__(self, status, js=None, text=""):
        self.status_code, self._js, self.text = status, js, text

    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js


class Relay(unittest.TestCase):
    def setUp(self):
        R._rate.clear(); R._cache.clear()
        self.sleep = mock.patch.object(R.time, "sleep", lambda s: None)
        self.sleep.start()
        self.env = mock.patch.object(R, "_env", lambda n: {"TONCENTER_API_KEY": "KEY123"}.get(n, ""))
        self.env.start()

    def tearDown(self):
        self.sleep.stop(); self.env.stop()

    def status(self, fn):
        with self.assertRaises(R.HTTPException) as c:
            fn()
        return c.exception.status_code

    def test_wallet_state_uses_the_key_and_reports_uninitialized_wallet(self):
        seen = {}

        def get(url, params=None, headers=None, timeout=0):
            seen.update(url=url, headers=headers, params=params)
            return Resp(200, {"ok": True, "result": {"account_state": "uninitialized", "seqno": None, "balance": "4000000000"}})
        with mock.patch.object(R.requests, "get", get):
            out = R.wallet_state(UQ, Req())
        self.assertEqual((out["state"], out["seqno"], out["balance"]), ("uninitialized", 0, 4000000000))
        self.assertEqual(seen["headers"], {"X-API-Key": "KEY123"})
        self.assertTrue(seen["url"].endswith("/getWalletInformation"))

    def test_wallet_state_retries_through_a_429(self):
        n = {"i": 0}

        def get(url, params=None, headers=None, timeout=0):
            n["i"] += 1
            return Resp(429, {"ok": False}) if n["i"] < 3 else Resp(200, {"ok": True, "result": {"account_state": "active", "seqno": 7}})
        with mock.patch.object(R.requests, "get", get):
            self.assertEqual(R.wallet_state(UQ, Req())["seqno"], 7)
        self.assertEqual(n["i"], 3)

    def test_wallet_state_is_cached_for_a_few_seconds(self):
        n = {"i": 0}

        def get(*a, **k):
            n["i"] += 1
            return Resp(200, {"ok": True, "result": {"account_state": "active", "seqno": 1}})
        with mock.patch.object(R.requests, "get", get):
            R.wallet_state(UQ, Req()); R.wallet_state(UQ, Req())
        self.assertEqual(n["i"], 1)

    def test_bad_addresses_are_refused(self):
        for a in ("", "hello", UQ + "x", "0x" + "ab" * 20, "UQ" + "!" * 46):
            self.assertEqual(self.status(lambda: R.wallet_state(a, Req())), 400, a)

    def test_send_goes_to_toncenter_once_when_it_works(self):
        calls = []

        def post(url, json=None, headers=None, timeout=0):
            calls.append((url, headers)); return Resp(200, {"ok": True, "result": {}})
        with mock.patch.object(R.requests, "post", post):
            out = R.send_boc(R.SendBody(boc=BOC), Req())
        self.assertEqual(out, {"ok": True, "via": "toncenter"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], {"X-API-Key": "KEY123"})

    def test_send_retries_429_then_succeeds_without_falling_back(self):
        n = {"i": 0}

        def post(url, json=None, headers=None, timeout=0):
            n["i"] += 1
            return Resp(429, {"ok": False}) if n["i"] == 1 else Resp(200, {"ok": True})
        with mock.patch.object(R.requests, "post", post):
            self.assertEqual(R.send_boc(R.SendBody(boc=BOC), Req())["via"], "toncenter")
        self.assertEqual(n["i"], 2)

    def test_send_falls_back_to_tonapi_when_toncenter_keeps_refusing(self):
        urls = []

        def post(url, json=None, headers=None, timeout=0):
            urls.append(url)
            return Resp(429, {"ok": False}) if "toncenter" in url else Resp(200, {})
        with mock.patch.object(R.requests, "post", post):
            self.assertEqual(R.send_boc(R.SendBody(boc=BOC), Req())["via"], "tonapi")
        self.assertEqual(sum("toncenter" in u for u in urls), 3)

    def test_a_real_rejection_is_not_retried_and_is_reported(self):
        urls = []

        def post(url, json=None, headers=None, timeout=0):
            urls.append(url)
            return Resp(200, {"ok": False, "error": "exit code 33"}) if "toncenter" in url else Resp(400, {"error": "bad seqno"})
        with mock.patch.object(R.requests, "post", post):
            with self.assertRaises(R.HTTPException) as c:
                R.send_boc(R.SendBody(boc=BOC), Req())
        self.assertEqual(c.exception.status_code, 502)
        self.assertIn("exit code 33", c.exception.detail)
        self.assertEqual(sum("toncenter" in u for u in urls), 1)

    def test_send_refuses_junk_and_oversize(self):
        for b in ("", "not base64!!", base64.b64encode(b"hello world").decode(), "A" * 9000):
            self.assertEqual(self.status(lambda: R.send_boc(R.SendBody(boc=b), Req())), 400, b[:12])

    def test_send_rate_limit(self):
        with mock.patch.object(R.requests, "post", lambda *a, **k: Resp(200, {"ok": True})):
            for _ in range(20):
                R.send_boc(R.SendBody(boc=BOC), Req())
            self.assertEqual(self.status(lambda: R.send_boc(R.SendBody(boc=BOC), Req())), 429)


if __name__ == "__main__":
    unittest.main()
