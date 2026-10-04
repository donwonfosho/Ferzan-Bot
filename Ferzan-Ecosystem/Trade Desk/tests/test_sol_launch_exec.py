"""Trade Bot wallet Solana launch helper: safety checks with the network and solders faked out.

  cd "Trade Desk" && python -m unittest tests.test_sol_launch_exec
"""
import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# a stand-in for solders.signature.Signature so these tests run without the real library
_sigmod = types.ModuleType("solders.signature")


class _Sig:
    def __init__(self, v=""):
        self.v = v

    @classmethod
    def default(cls):
        return cls("")

    def __eq__(self, o):
        return self.v == o.v

    def __str__(self):
        return self.v


_sigmod.Signature = _Sig
sys.modules.setdefault("solders", types.ModuleType("solders"))
sys.modules["solders.signature"] = _sigmod
import sol_launch_exec as ex  # noqa: E402

ADDR, MINT = "AddrAddrAddrAddrAddrAddrAddrAddrAddr1", "MintMintMintMintMintMintMintMintMint1"


class FakeTx:
    def __init__(self, signed=True):
        self.signatures = [_Sig(""), _Sig("mintsig")]
        self._signed = signed
        self.message = types.SimpleNamespace(recent_blockhash="bh")

    def partial_sign(self, kps, bh):
        if self._signed:
            self.signatures[0] = _Sig("walletsig" * 3)

    def __bytes__(self):
        return b"tx"


def call(fn, *a, **k):
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            fn(*a, **k)
        except SystemExit:
            pass
    return json.loads(buf.getvalue().strip().splitlines()[-1])


class Launch(unittest.TestCase):
    def _sleep(self, s):
        self.now += s

    def setUp(self):
        self.now = 1_000_000.0  # fake clock: waits cost no real time
        self.tmp = tempfile.mkdtemp()
        ex.LOG = Path(self.tmp) / "log.json"
        ex.common.LOG = ex.LOG
        self.p = [mock.patch.object(ex, "_wallet", lambda uid: (ADDR, object())),
                  mock.patch.object(ex, "_decode", lambda h, a, m: FakeTx(self.signed)),
                  mock.patch.object(ex, "_outflow", lambda tx, a: (self.spend, "")),
                  mock.patch.object(ex.time, "sleep", self._sleep),
                  mock.patch.object(ex.time, "time", lambda: self.now)]
        self.signed, self.spend = True, 150_000_000
        for p in self.p:
            p.start()
        self.sent = []
        self.bh_valid = False
        self.rpc_send = {"result": "ok"}
        self.status = ("confirmed", "")
        self.p += [mock.patch.object(ex, "_rpc", self._rpc), mock.patch.object(ex, "_status", lambda s: self.status)]
        for p in self.p[-2:]:
            p.start()

    def tearDown(self):
        mock.patch.stopall()

    def _rpc(self, method, params, timeout=25):
        if method == "isBlockhashValid":
            if isinstance(self.bh_valid, Exception):
                raise self.bh_valid
            return {"result": {"value": self.bh_valid}}
        if method == "sendTransaction":
            self.sent.append(params)
            if isinstance(self.rpc_send, Exception):
                raise self.rpc_send
            return self.rpc_send
        return {}

    def args(self, **kw):
        a = {"uid": 1, "request_id": "r1", "tx_hex": "00", "mint": MINT, "cap_lamports": 200_000_000, "dry": False}
        a.update(kw)
        return a

    def test_happy_path_sends_once(self):
        r = call(ex.launch, self.args())
        self.assertTrue(r["ok"] and r["mint"] == MINT)
        self.assertEqual(len(self.sent), 1)

    def test_over_cap_sends_nothing(self):
        self.spend = 300_000_000
        r = call(ex.launch, self.args())
        self.assertFalse(r["ok"])
        self.assertEqual(self.sent, [])

    def test_missing_or_huge_cap(self):
        self.assertFalse(call(ex.launch, self.args(cap_lamports=0))["ok"])
        self.assertFalse(call(ex.launch, self.args(cap_lamports=10**15))["ok"])
        self.assertEqual(self.sent, [])

    def test_unsigned_after_signing_sends_nothing(self):
        self.signed = False
        r = call(ex.launch, self.args())
        self.assertFalse(r["ok"])
        self.assertEqual(self.sent, [])

    def test_dry_run_sends_and_records_nothing(self):
        r = call(ex.launch, self.args(dry=True))
        self.assertTrue(r["dry"])
        self.assertEqual(self.sent, [])
        self.assertFalse(ex.LOG.exists() and json.loads(ex.LOG.read_text() or "{}"))

    def test_second_call_never_sends_again(self):
        call(ex.launch, self.args())
        self.status = ("pending", "")
        r = call(ex.launch, self.args())
        self.assertEqual(len(self.sent), 1)
        self.assertTrue(r.get("pending") or r.get("ok") is False)

    def test_lost_reply_is_maybe_sent_not_failed(self):
        self.rpc_send = TimeoutError("lost")
        self.status = ("unknown", "")
        r = call(ex.launch, self.args())
        self.assertTrue(r.get("maybe_sent"))
        self.assertEqual(len(self.sent), 1)

    def test_rejected_by_network_allows_retry(self):
        self.rpc_send = {"error": {"code": -32002, "message": "Blockhash not found"}}
        self.status = ("unknown", "")
        r = call(ex.launch, self.args())
        self.assertFalse(r["ok"])
        self.assertFalse(json.loads(ex.LOG.read_text() or "{}"))

    def _stranded(self):
        """a launch that was sent, then the network stopped answering"""
        self.rpc_send = {"result": "ok"}
        self.status = ("pending", "")
        call(ex.launch, self.args())
        self.now += 400

    def test_expired_needs_network_proof(self):
        self._stranded()
        self.status = ("unknown", "")
        self.bh_valid = False  # network says: never seen it, and the blockhash is dead
        r = call(ex.launch, self.args())
        self.assertTrue(r.get("expired"))
        self.assertEqual(len(self.sent), 1)
        again = call(ex.launch, self.args())  # and it stays expired: this request id never sends again
        self.assertTrue(again.get("expired"))
        self.assertEqual(len(self.sent), 1)

    def test_blockhash_still_valid_is_not_expired(self):
        self._stranded()
        self.status = ("unknown", "")
        self.bh_valid = True
        self.assertFalse(call(ex.launch, self.args()).get("expired"))

    def test_rpc_down_is_not_expired(self):
        self._stranded()
        self.bh_valid = RuntimeError("rpc down")
        with mock.patch.object(ex, "_status", side_effect=RuntimeError("rpc down")):
            r = call(ex.launch, self.args())
        self.assertFalse(r.get("expired"))
        self.assertTrue(r.get("pending"))

    def test_internal_error_after_send_is_maybe_sent(self):
        self.rpc_send = {"error": {"code": -32603, "message": "Internal error"}}
        self.status = ("unknown", "")
        r = call(ex.launch, self.args())
        self.assertTrue(r.get("maybe_sent"))
        self.assertTrue(json.loads(ex.LOG.read_text())["r1"]["signature"])

    def test_failed_on_chain_reports_failure(self):
        self.status = ("failed", "InstructionError")
        r = call(ex.launch, self.args())
        self.assertFalse(r["ok"])
        self.assertIn("failed on Solana", r["error"])

    def test_error_text_hides_hosts(self):
        self.assertNotIn("quiknode", ex._clean("HTTPSConnectionPool(host='abc.solana-mainnet.quiknode.pro', port=443)"))

    def test_error_text_hides_urls(self):
        self.assertNotIn("http", ex._clean("bad https://rpc.example/key123 here"))
        self.assertNotIn("key123", ex._clean("url: /v2/key123 failed"))


if __name__ == "__main__":
    unittest.main()
