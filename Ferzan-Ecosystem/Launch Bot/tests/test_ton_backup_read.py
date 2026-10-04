"""TON chain reads fall back to toncenter / tonapi when the liteserver answers "cannot load block" (651).

  cd "Launch Bot" && python -m unittest tests.test_ton_backup_read
"""
import base64
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import ton_launch as tl  # noqa: E402


class _Slice:
    def __init__(self, raw):
        self.raw = raw


def _fake_core():
    mod = types.ModuleType("pytoniq_core")

    class Cell:
        @staticmethod
        def one_from_boc(raw):
            return types.SimpleNamespace(begin_parse=lambda: _Slice(raw))

    class _B:
        def store_address(self, a):
            return self

        def end_cell(self):
            return types.SimpleNamespace(to_boc=lambda: b"\x0a\x0b")

    mod.Cell = Cell
    mod.Address = lambda a: a
    mod.begin_cell = lambda: _B()
    return mod


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


B64 = base64.b64encode(b"\x01\x02\x03\x04").decode()


class BackupRead(unittest.TestCase):
    def setUp(self):
        tl._LITE_BAD_UNTIL[0] = 0.0
        p = mock.patch.dict(sys.modules, {"pytoniq_core": _fake_core()})
        p.start()
        self.addCleanup(p.stop)

    def test_numbers_hex_decimal_negative(self):
        self.assertEqual(tl._num("0x1f"), 31)
        self.assertEqual(tl._num("12"), 12)
        self.assertEqual(tl._num("-0x10"), -16)

    def test_toncenter_numbers_and_cell(self):
        body = {"exit_code": 0, "stack": [{"type": "num", "value": "0x2710"}, {"type": "cell", "value": B64},
                                          {"type": "null"}]}
        with mock.patch("requests.post", return_value=_Resp(200, body)):
            out = tl._http_get_method("EQx", "get_curve", [5])
        self.assertEqual(out[0], 10000)
        self.assertEqual(out[1].raw, b"\x01\x02\x03\x04")
        self.assertIsNone(out[2])

    def test_falls_to_tonapi_when_toncenter_fails(self):
        body = {"success": True, "exit_code": 0, "stack": [{"type": "num", "num": "0x5"},
                                                           {"type": "slice", "slice": "01020304"}]}
        with mock.patch("requests.post", return_value=_Resp(429, {})), \
                mock.patch("requests.get", return_value=_Resp(200, body)):
            out = tl._http_get_method("EQx", "get_wallet_data")
        self.assertEqual(out[0], 5)
        self.assertEqual(out[1].raw, b"\x01\x02\x03\x04")

    def test_nonzero_exit_code_is_a_failure(self):
        with mock.patch("requests.post", return_value=_Resp(200, {"exit_code": 11, "stack": []})), \
                mock.patch("requests.get", return_value=_Resp(200, {"success": False, "exit_code": 11})):
            with self.assertRaises(RuntimeError):
                tl._http_get_method("EQx", "get_curve")

    def test_only_number_and_address_arguments(self):
        with self.assertRaises(ValueError):
            tl._http_get_method("EQx", "get_wallet_address", [object()])

    def test_address_argument_is_sent_as_a_slice(self):
        body = {"exit_code": 0, "stack": [{"type": "cell", "value": B64}]}
        with mock.patch("requests.post", return_value=_Resp(200, body)) as post:
            tl.run_get_method.__globals__["_LITE_BAD_UNTIL"][0] = 9e12  # skip the liteserver
            out = tl.run_get_method("EQminter", "get_wallet_address", ["lite-slice-stand-in"], http_args=["UQowner"])
        sent = post.call_args.kwargs["json"]["stack"]
        self.assertEqual(sent, [{"type": "slice", "value": "UQowner"}])
        self.assertEqual(out[0].raw, b"\x01\x02\x03\x04")

    def test_liteserver_failure_uses_backup_then_skips_liteserver(self):
        calls = {"lite": 0}

        def boom(coro, *a, **k):
            calls["lite"] += 1
            coro.close()
            raise RuntimeError("Liteserver crashed with 651 code")

        body = {"exit_code": 0, "stack": [{"type": "num", "value": "0x7"}]}
        with mock.patch("asyncio.run", side_effect=boom), mock.patch("time.sleep"), \
                mock.patch("requests.post", return_value=_Resp(200, body)):
            self.assertEqual(tl.run_get_method("EQx", "get_curve"), [7])
            first = calls["lite"]
            self.assertEqual(first, 2)  # two tries, then the backup
            self.assertEqual(tl.run_get_method("EQx", "get_curve"), [7])
            self.assertEqual(calls["lite"], first)  # skipped for a minute: no more slow liteserver tries

    def test_toncenter_second_try_sends_the_address_as_a_boc_slice(self):
        ok = {"exit_code": 0, "stack": [{"type": "num", "value": "0x1"}]}
        with mock.patch("requests.post", side_effect=[_Resp(500, {}), _Resp(200, ok)]) as post:
            out = tl._http_get_method("EQminter", "get_wallet_address", ["UQowner"])
        self.assertEqual(out, [1])
        first = post.call_args_list[0].kwargs["json"]["stack"][0]["value"]
        second = post.call_args_list[1].kwargs["json"]["stack"][0]["value"]
        self.assertEqual(first, "UQowner")
        self.assertEqual(second, base64.b64encode(b"\x0a\x0b").decode())

    def test_contract_error_is_not_retried_and_does_not_switch_the_node_off(self):
        calls = {"lite": 0}

        def boom(coro, *a, **k):
            calls["lite"] += 1
            coro.close()
            raise RuntimeError("exit code -13: contract is not active")

        with mock.patch("asyncio.run", side_effect=boom), mock.patch("time.sleep"), \
                mock.patch("requests.post", return_value=_Resp(200, {"exit_code": -13, "stack": []})), \
                mock.patch("requests.get", return_value=_Resp(404, {})):
            with self.assertRaises(RuntimeError):
                tl.run_get_method("EQx", "get_jetton_data")
        self.assertEqual(calls["lite"], 1)  # no second try
        self.assertEqual(tl._LITE_BAD_UNTIL[0], 0.0)  # and the node stays on

    def test_node_trouble_classifier(self):
        import asyncio
        self.assertTrue(tl._is_node_trouble(asyncio.TimeoutError()))
        self.assertTrue(tl._is_node_trouble(RuntimeError("Liteserver crashed with 651 code")))
        self.assertFalse(tl._is_node_trouble(RuntimeError("exit code -13")))

    def test_too_many_requests_waits_and_asks_again(self):
        ok = {"exit_code": 0, "stack": [{"type": "num", "value": "0x9"}]}
        with mock.patch("requests.post", side_effect=[_Resp(429, {}), _Resp(200, ok)]) as post, \
                mock.patch("time.sleep") as sl:
            self.assertEqual(tl._http_get_method("EQx", "get_curve"), [9])
        self.assertEqual(post.call_count, 2)
        sl.assert_called_once()

    def test_both_fail_raises(self):
        def boom(coro, *a, **k):
            coro.close()
            raise RuntimeError("651")

        with mock.patch("asyncio.run", side_effect=boom), mock.patch("time.sleep"), \
                mock.patch("requests.post", return_value=_Resp(500, {})), \
                mock.patch("requests.get", return_value=_Resp(500, {})):
            with self.assertRaises(RuntimeError):
                tl.run_get_method("EQx", "get_curve")


if __name__ == "__main__":
    unittest.main()
