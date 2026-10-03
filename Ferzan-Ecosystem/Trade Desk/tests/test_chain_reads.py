"""A failed node read must never look like an empty wallet, on any chain."""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import requests

import evm_signer as ev
import signer as sg
import tron_signer as tr


class Resp:
    def __init__(self, code=200, data=None, text=""):
        self.status_code, self._d, self.content, self.text = code, data if data is not None else {}, b"x", text

    def json(self):
        return self._d


class Evm(unittest.TestCase):
    def test_error_answers_raise_instead_of_zero(self):
        err = {"jsonrpc": "2.0", "error": {"code": -32005, "message": "rate limited"}}
        with mock.patch.object(ev, "_rpc", return_value=err):
            with self.assertRaises(RuntimeError):
                ev._erc20_balance("http://x", "0xtok", "0x" + "1" * 40)
            with self.assertRaises(RuntimeError):
                ev._nonce("http://x", "0xabc")
            with self.assertRaises(RuntimeError):
                ev.native_balance("base", "0xabc")

    def test_real_zero_is_still_zero(self):
        with mock.patch.object(ev, "_rpc", return_value={"result": "0x"}):
            self.assertEqual(ev._erc20_balance("http://x", "0xtok", "0x" + "1" * 40), 0)
        with mock.patch.object(ev, "_rpc", return_value={"result": "0x0"}):
            self.assertEqual(ev.native_balance("base", "0xabc")[0], 0.0)

    def test_single_endpoint_chain_gets_a_second_try(self):
        calls = []

        def post(url, **kw):
            calls.append(url)
            return Resp(429) if len(calls) == 1 else Resp(200, {"result": "0x10"})

        with mock.patch.object(ev.requests, "post", side_effect=post), mock.patch("time.sleep"):
            body = ev._rpc("https://rpc.mainnet.chain.robinhood.com/unlisted-single", "eth_blockNumber", [])
        self.assertEqual(body["result"], "0x10")
        self.assertEqual(len(calls), 2)

    def test_backup_endpoint_must_prove_the_chain(self):
        ev._FB_VERIFIED.clear()
        arb = ev.CHAINS["arb"]["rpc"]
        wrong = Resp(200, {"result": "0x1"})  # Ethereum answering for Arbitrum
        right = Resp(200, {"result": hex(int(ev.CHAINS["arb"]["chain_id"]))})
        with mock.patch.object(ev.requests, "post", return_value=wrong):
            self.assertFalse(ev._backup_ok(arb, "https://evil.example/arb"))
        with mock.patch.object(ev.requests, "post", return_value=right):
            self.assertTrue(ev._backup_ok(arb, "https://good.example/arb"))

    def test_chains_that_had_one_endpoint_now_have_backups(self):
        for cid in ("arb", "avax", "op", "pol", "linea", "sonic", "pulse"):
            self.assertGreaterEqual(len(ev._rpc_urls(ev.CHAINS[cid]["rpc"])), 2, cid)


class Solana(unittest.TestCase):
    def post(self, data, code=200):
        return mock.patch.object(sg, "_rpc_post", return_value=Resp(code, data))

    def test_balance_rpc_error_raises(self):
        with self.post({"error": {"message": "max usage reached"}}, 429):
            with self.assertRaises(RuntimeError):
                sg.sol_balance_lamports("addr")

    def test_zero_balance_is_zero(self):
        with self.post({"result": {"context": {}, "value": 0}}):
            self.assertEqual(sg.sol_balance_lamports("addr"), 0)

    def test_token_balance_and_holdings_raise_on_error(self):
        with self.post({"error": {"message": "busy"}}):
            with self.assertRaises(RuntimeError):
                sg._token_raw_balance("mint", kp=types.SimpleNamespace(pubkey=lambda: "k"))
            with self.assertRaises(RuntimeError):
                sg.holdings_pub("addr", strict=True)

    def test_empty_account_is_empty(self):
        with self.post({"result": {"value": []}}):
            self.assertEqual(sg._token_raw_balance("mint", kp=types.SimpleNamespace(pubkey=lambda: "k")), 0)
            self.assertEqual(sg.holdings_pub("addr", strict=True), [])


class Tron(unittest.TestCase):
    def test_post_retries_busy_answers(self):
        seq = [Resp(429), Resp(500), Resp(200, {"ok": 1})]
        with mock.patch.object(tr.requests, "post", side_effect=seq) as p, mock.patch.object(tr.time, "sleep"):
            self.assertEqual(tr._post("/wallet/x", {}), {"ok": 1})
        self.assertEqual(p.call_count, 3)

    def test_post_gives_up_by_raising(self):
        with mock.patch.object(tr.requests, "post", side_effect=requests.ConnectionError("down")), mock.patch.object(tr.time, "sleep"):
            with self.assertRaises(requests.RequestException):
                tr._post("/wallet/x", {})

    def test_broadcast_read_timeout_is_not_resent(self):
        with mock.patch.object(tr.requests, "post", side_effect=requests.ReadTimeout("slow")) as p, mock.patch.object(tr.time, "sleep"):
            with self.assertRaises(requests.ReadTimeout):
                tr._post("/wallet/broadcasttransaction", {}, retries=1)
        self.assertEqual(p.call_count, 1)

    def test_trx_balance(self):
        with mock.patch.object(tr, "_post", return_value={"Error": "rate limit"}):
            with self.assertRaises(RuntimeError):
                tr._trx_balance("41" + "a" * 40)
        with mock.patch.object(tr, "_post", return_value={}):  # never-activated account
            self.assertEqual(tr._trx_balance("41" + "a" * 40), 0)
        with mock.patch.object(tr, "_post", return_value={"balance": 5_000_000}):
            self.assertEqual(tr._trx_balance("41" + "a" * 40), 5_000_000)

    def test_const_strict_tells_busy_from_revert(self):
        with mock.patch.object(tr, "_post", return_value={"Error": "busy"}):
            self.assertEqual(tr._const("41" + "b" * 40, "41" + "a" * 40, "f()", ""), [])
            with self.assertRaises(RuntimeError):
                tr._const("41" + "b" * 40, "41" + "a" * 40, "f()", "", strict=True)
        revert = {"result": {"result": False}, "constant_result": [""]}
        with mock.patch.object(tr, "_post", return_value=revert):
            self.assertEqual(tr._const("41" + "b" * 40, "41" + "a" * 40, "f()", "", strict=True), [])

    def test_token_balance_uses_decimals(self):
        words = {"balanceOf(address)": [2_500_000], "decimals()": [6]}
        with mock.patch.object(tr, "_const", side_effect=lambda c, o, sig, p, strict=False: words[sig]):
            self.assertEqual(tr.token_balance("41" + "a" * 40, "41" + "b" * 40), 2.5)

    def test_guard_messages_are_honest(self):
        def boom():
            raise RuntimeError("TronGrid read failed: busy")

        def boom_after_send():
            tr._SENT.flag = True
            raise requests.ConnectionError("dropped")

        ok, msg = tr._guard(boom)
        self.assertFalse(ok)
        self.assertIn("nothing was sent", msg)
        ok, msg = tr._guard(boom_after_send)
        self.assertFalse(ok)
        self.assertIn("may already have gone out", msg)


def _stub_telegram():
    try:
        import telegram  # noqa: F401
        return
    except ImportError:
        pass

    class _Mod(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return mock.MagicMock()

    sys.modules["telegram"] = _Mod("telegram")
    for sub in ("ext", "constants", "error", "request", "helpers"):
        sys.modules["telegram." + sub] = _Mod("telegram." + sub)


class Exits(unittest.TestCase):
    def setUp(self):
        _stub_telegram()
        import bot
        self.bot = bot

    def wallets(self):
        return mock.patch.object(self.bot.user_wallets, "all_secrets", return_value=[(1, "Main", "SOLSECRET", "0x" + "11" * 32)])

    def test_ton_position_is_seen_by_the_exit_monitor(self):
        import ton_signer
        with self.wallets(), mock.patch.object(ton_signer, "jetton_holding", return_value=(671.0, "UQo")):
            out = self.bot._exit_holdings(7, "EQ" + "A" * 46)
        self.assertEqual([(h[2], h[3]) for h in out], [("ton", 671.0)])

    def test_tron_position_is_seen_by_the_exit_monitor(self):
        with self.wallets(), mock.patch.object(tr, "token_balance", return_value=42.0), \
                mock.patch.object(tr, "evm_key_to_tron", return_value=("T" + "a" * 33, "k")):
            out = self.bot._exit_holdings(7, "T" + "b" * 33)
        self.assertEqual([(h[2], h[3]) for h in out], [("trx", 42.0)])

    def test_unreadable_balance_aborts_the_cycle(self):
        import ton_signer
        with self.wallets(), mock.patch.object(ton_signer, "jetton_holding", side_effect=RuntimeError("cannot load block")):
            with self.assertRaises(RuntimeError):
                self.bot._exit_holdings(7, "EQ" + "A" * 46)

    def test_exit_sells_go_to_the_right_chain(self):
        import ton_signer
        with mock.patch.object(ton_signer, "sell_ton", return_value=(True, "sold ton")) as st, \
                mock.patch.object(tr, "sell_tron", return_value=(True, "sold trx")) as sr, \
                mock.patch.object(self.bot, "_slip_bps", return_value=500), \
                mock.patch.object(self.bot.feecollect, "sells_enabled", return_value=False):
            ok, msg, share = self.bot._exit_sell_all(7, "EQ" + "A" * 46, [("S", "E", "ton", 10.0)], 100)
            self.assertEqual((ok, share, st.call_count), (True, 1.0, 1))
            ok, msg, share = self.bot._exit_sell_all(7, "T" + "b" * 33, [("S", "E", "trx", 5.0)], 50)
            self.assertEqual((ok, share, sr.call_count), (True, 1.0, 1))


if __name__ == "__main__":
    unittest.main()
