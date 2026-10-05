import asyncio, importlib, logging, os, sqlite3, sys, tempfile, types, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp()
os.environ.update(
    SUBSCRIPTION_DB_PATH=f"{TMP}/subs.db", MM_DB_PATH=f"{TMP}/mm.db", DB_PATH=f"{TMP}/ferzan.db",
    CREDENTIALS_DB_PATH=f"{TMP}/cred.db", PLATFORM_TREASURY_EVM="0x" + "ab" * 20,
    FERZAN_ADMIN_IDS="1", CREDENTIALS_ENCRYPTION_KEY="x",
)
for name in ("telegram", "telegram.ext", "eth_account", "eth_utils", "ccxt", "ccxt.async_support"):
    sys.modules.pop(name, None)
sys.modules["telegram"] = mock.MagicMock()
sys.modules["telegram.ext"] = mock.MagicMock()
fake_eth = types.ModuleType("eth_account")
class _Acct:
    address = "0x" + "11" * 20
    @staticmethod
    def from_key(_k): return _Acct()
    def sign_transaction(self, tx): raise AssertionError("must not sign in these tests")
fake_eth.Account = _Acct
sys.modules["eth_account"] = fake_eth
fake_utils = types.ModuleType("eth_utils"); fake_utils.to_checksum_address = lambda a: a
sys.modules["eth_utils"] = fake_utils
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "liq"))
import liq_bot as lb  # noqa: E402
import basestonk_mm as bm  # noqa: E402
import subscription  # noqa: E402

TREASURY = "0x" + "ab" * 20
WALLET = "0x" + "11" * 20
TXH = "0x" + "ab" * 32


class Msg:
    def __init__(self): self.sent, self.deleted = [], False
    async def reply_text(self, text, **k): self.sent.append(text)
    async def delete(self): self.deleted = True


class Chat:
    def __init__(self, msg): self.msg = msg; self.type = "private"
    async def send_message(self, text, **k): self.msg.sent.append(text)


def upd(uid):
    m = Msg(); u = mock.MagicMock()
    u.effective_user.id = uid; u.effective_message = m; u.effective_chat = Chat(m)
    return u, m


def ctx(*args):
    c = mock.MagicMock(); c.args = list(args); return c


def run(coro): return asyncio.run(coro)


class Paid(unittest.TestCase):
    def setUp(self):
        subscription.init_db()
        with subscription._get_conn() as c:
            c.execute("DROP TABLE IF EXISTS redeemed_tx"); c.execute("DELETE FROM subscriptions")
        self.tx = {"to": TREASURY, "from": WALLET, "value": hex(int(0.02e18))}
        self.patches = [
            mock.patch.object(lb, "_fetch_tx", lambda h: ({"status": "0x1"}, dict(self.tx))),
            mock.patch.object(lb, "_eth_usd", lambda: 3000.0),
            mock.patch.object(bm, "get_linked_evm_address", lambda uid: WALLET),
        ]
        for p in self.patches: p.start()

    def tearDown(self):
        for p in self.patches: p.stop()

    def pay(self, uid, h=TXH):
        u, m = upd(uid); run(lb.paid_cmd(u, ctx(h))); return m.sent[-1]

    def test_a_good_payment_unlocks(self):
        self.assertIn("MM unlocked", self.pay(5))
        self.assertTrue(subscription.is_premium(5))

    def test_letter_case_cannot_redeem_the_same_payment_twice(self):
        self.assertIn("MM unlocked", self.pay(5, TXH))
        self.assertIn("already redeemed", self.pay(6, "0x" + "AB" * 32))
        self.assertFalse(subscription.is_premium(6))

    def test_old_mixed_case_rows_still_block(self):
        subscription.init_db()
        with subscription._get_conn() as c:
            c.execute("CREATE TABLE IF NOT EXISTS redeemed_tx (tx_hash TEXT PRIMARY KEY, user_id INTEGER)")
            c.execute("INSERT INTO redeemed_tx VALUES (?,?)", ("0x" + "AB" * 32, 9))
        self.assertIn("already redeemed", self.pay(5))

    def test_someone_elses_payment_is_refused(self):
        self.tx["from"] = "0x" + "22" * 20
        self.assertIn("different address", self.pay(5))
        self.assertFalse(subscription.is_premium(5))

    def test_no_wallet_no_credit(self):
        with mock.patch.object(bm, "get_linked_evm_address", lambda uid: None):
            self.assertIn("can't tie", self.pay(5))

    def test_escape_hatch_allows_any_sender(self):
        self.tx["from"] = "0x" + "22" * 20
        with mock.patch.object(lb, "PAID_ANY_SENDER", True):
            self.assertIn("MM unlocked", self.pay(5))

    def test_malformed_hash_never_touches_the_network(self):
        with mock.patch.object(lb, "_fetch_tx", side_effect=AssertionError("called")):
            for bad in ("0x123", "nonsense", "0x" + "z" * 64, "0x" + "a" * 65):
                self.assertIn("doesn't look like", self.pay(5, bad))

    def test_rpc_error_does_not_leak_the_endpoint(self):
        with mock.patch.object(lb, "_fetch_tx", side_effect=RuntimeError("https://base.example/v2/SECRETKEY123")):
            out = self.pay(5)
        self.assertNotIn("SECRETKEY123", out)

    def test_short_payment_refused(self):
        self.tx["value"] = hex(int(0.001e18))
        self.assertIn("short of", self.pay(5))

    def test_failed_grant_gives_the_payment_back(self):
        with mock.patch.object(subscription, "grant_premium", side_effect=RuntimeError("disk")):
            self.assertIn("Nothing was used up", self.pay(5))
        self.assertFalse(lb._tx_claimed(TXH))
        self.assertIn("MM unlocked", self.pay(5))   # can try again


class Admin(unittest.TestCase):
    def test_grantmm_bounds(self):
        subscription.init_db()
        for args in (("5", "-3"), ("5", "100000"), ("0",), ("-4", "5")):
            u, m = upd(1); run(lb.grantmm_cmd(u, ctx(*args)))
            self.assertNotIn("Granted", m.sent[-1], args)
        u, m = upd(1); run(lb.grantmm_cmd(u, ctx("5", "30")))
        self.assertIn("Granted", m.sent[-1])


class Wizard(unittest.TestCase):
    def btn(self, uid, data):
        q = mock.MagicMock(); q.from_user.id = uid; q.data = data
        edits = []
        async def edit(text, **k): edits.append(text)
        q.edit_message_text = edit
        u = mock.MagicMock(); u.callback_query = q
        run(lb._mm_wizard_button(u, mock.MagicMock(), uid, data)); return edits

    def test_out_of_order_or_odd_values_do_nothing(self):
        lb.MM_WIZ[7] = {"step": "budget", "chain": "base", "token": "0x" + "1" * 40}
        self.assertIn("expired", self.btn(7, "mmw:dur:30")[0])       # wrong step
        self.assertEqual(self.btn(7, "mmw:budget:99999"), [])         # not one of the offered amounts
        self.assertNotIn("budget_usd", lb.MM_WIZ[7])
        self.assertIn("expired", self.btn(7, "mmw:confirm")[0])

    def test_confirm_starts_once_and_rechecks_access(self):
        wiz = {"step": "confirm", "chain": "base", "token": "0x" + "1" * 40, "trade_usd": 2.0, "budget_usd": 10.0, "minutes": 15}
        lb.MM_WIZ[8] = dict(wiz)
        with mock.patch.object(subscription, "is_premium", lambda u: False):
            out = self.btn(8, "mmw:confirm")
        self.assertIn("expired", out[0]); self.assertNotIn(8, lb.MM_WIZ)
        lb.MM_WIZ[8] = dict(wiz)
        calls = []
        async def begin(*a): calls.append(a); return None
        with mock.patch.object(subscription, "is_premium", lambda u: True), mock.patch.object(lb, "_mm_begin", begin):
            self.btn(8, "mmw:confirm"); self.btn(8, "mmw:confirm")
        self.assertEqual(len(calls), 1)                               # second tap finds nothing to start


class Lookups(unittest.TestCase):
    def resp(self, status, body):
        r = mock.MagicMock(); r.status_code = status; r.json.return_value = body; return r

    def test_pools_prefer_the_token_itself_and_check_status(self):
        ca = "0x" + "c" * 40
        pairs = [
            {"baseToken": {"address": "0x" + "d" * 40}, "liquidity": {"usd": 9e9}},
            {"baseToken": {"address": ca.upper().replace("0X", "0x")}, "liquidity": {"usd": 5}},
            "junk",
        ]
        with mock.patch.object(lb.requests, "get", return_value=self.resp(200, {"pairs": pairs})):
            out = lb._pools(ca)
        self.assertEqual(len(out), 1); self.assertEqual(out[0]["liquidity"]["usd"], 5)
        with mock.patch.object(lb.requests, "get", return_value=self.resp(503, {})):
            with self.assertRaises(RuntimeError): lb._pools(ca)

    def test_odd_numbers_do_not_crash_the_card(self):
        self.assertEqual(lb._f("nan"), 0.0); self.assertEqual(lb._f("inf"), 0.0); self.assertEqual(lb._f(None), 0.0)
        pool = {"baseToken": {"name": "T", "symbol": "T"}, "chainId": "base", "liquidity": {"usd": "x"},
                "volume": {"h24": float("inf")}, "txns": {"h24": {"buys": "?", "sells": None}}}
        u, m = upd(1)
        with mock.patch.object(lb, "_pools", lambda ca: [pool]), mock.patch.object(lb, "_holder_count", lambda *a: None):
            run(lb.token_card(u, "0x" + "c" * 40))
        self.assertIn("Liq $0", m.sent[-1])

    def test_lookup_failure_shows_no_exception_text(self):
        u, m = upd(1)
        with mock.patch.object(lb, "_pools", side_effect=RuntimeError("https://x/?key=TOPSECRET")):
            run(lb.token_card(u, "0x" + "c" * 40))
        self.assertNotIn("TOPSECRET", m.sent[-1])

    def test_token_command_rejects_non_addresses(self):
        u, m = upd(1)
        with mock.patch.object(lb, "token_card", side_effect=AssertionError("called")):
            run(lb.token_cmd(u, ctx("../../etc/passwd")))
        self.assertIn("not a contract address", m.sent[-1])

    def test_covalent_key_is_a_header_and_never_logged(self):
        seen = {}
        def get(url, **k):
            seen.update(k); raise RuntimeError(f"boom {url}?key=COVKEY")
        with mock.patch.object(lb, "COVALENT_API_KEY", "COVKEY"), mock.patch.object(lb.requests, "get", get):
            with self.assertLogs("liqbot", level="WARNING") as cm:
                self.assertIsNone(lb._holder_count("base", "0x" + "c" * 40))
        self.assertNotIn("params", seen); self.assertIn("Bearer COVKEY", seen["headers"]["Authorization"])
        self.assertNotIn("COVKEY", "\n".join(cm.output))

    def test_slow_lookups_are_run_off_the_event_loop(self):
        src = (ROOT / "liq_bot.py").read_text()
        for needle in ("asyncio.to_thread(_pools", "asyncio.to_thread(_holder_count", "asyncio.to_thread(_fetch_referral_stats", "asyncio.to_thread(_fetch_tx"):
            self.assertIn(needle, src)


class SetKeys(unittest.TestCase):
    def go(self, *args, store=None):
        u, m = upd(3)
        with mock.patch.object(lb, "HAS_MM", True), \
             mock.patch.object(lb.credentials_db, "init_db", lambda: None), \
             mock.patch.object(lb.credentials_db, "store_credentials", store or (lambda *a: None)):
            run(lb.setkeys(u, ctx(*args)))
        return m

    def test_secret_message_is_deleted_and_not_echoed(self):
        m = self.go("binance", "BTC/USDT", "KEY", "SECRETVALUE")
        self.assertTrue(m.deleted); self.assertIn("stored encrypted", m.sent[-1]); self.assertNotIn("SECRETVALUE", " ".join(m.sent))

    def test_bad_exchange_not_stored_but_still_deleted(self):
        stored = []
        m = self.go("__class__", "BTC/USDT", "KEY", "SECRET", store=lambda *a: stored.append(a))
        self.assertEqual(stored, []); self.assertTrue(m.deleted); self.assertIn("Unknown exchange", m.sent[-1])

    def test_storage_failure_is_reported_without_the_secret(self):
        def boom(*a): raise RuntimeError("SECRETVALUE leaked here")
        m = self.go("binance", "BTC/USDT", "KEY", "SECRETVALUE", store=boom)
        self.assertTrue(m.deleted); self.assertNotIn("SECRETVALUE", " ".join(m.sent)); self.assertIn("Nothing was saved", m.sent[-1])


class TxChecks(unittest.TestCase):
    def tx(self, **k):
        t = {"to": "0x" + "5" * 40, "data": "0x04e45aaf" + "00" * 32, "value": "0", "chainId": 8453, "gas": 300000}
        t.update(k); return t

    def prob(self, tx, kind="swap", **k): return bm._tx_problem(tx, 8453, kind, **k)

    def test_a_normal_swap_passes(self):
        self.assertIsNone(self.prob(self.tx()))
        self.assertIsNone(self.prob(self.tx(value=str(10**15)), max_value=10**15))

    def test_value_chain_gas_and_shape(self):
        self.assertIn("ETH", self.prob(self.tx(value=str(10**18))))
        self.assertIn("ETH", self.prob(self.tx(value="-1")))
        self.assertIn("chain", self.prob(self.tx(chainId=1)))
        self.assertIn("gas", self.prob(self.tx(gas=10**8)))
        for bad in (self.tx(to="0x123"), self.tx(data="0x12"), self.tx(data="0xzz11223344"), None, "x"):
            self.assertIsNotNone(self.prob(bad))

    def test_swaps_cannot_be_plain_transfers(self):
        for sel in ("a9059cbb", "23b872dd", "095ea7b3", "2e1a7d4d"):
            self.assertIn("not a swap", self.prob(self.tx(data="0x" + sel + "00" * 64)))

    def test_approvals_must_be_approve_on_a_leg_token(self):
        weth = "0x" + "4" * 40
        good = self.tx(to=weth, data="0x095ea7b3" + "00" * 64)
        self.assertIsNone(self.prob(good, "approve", spend_tokens={weth}))
        self.assertIn("not an approve", self.prob(self.tx(to=weth), "approve", spend_tokens={weth}))
        self.assertIn("does not use", self.prob(self.tx(to="0x" + "9" * 40, data="0x095ea7b3" + "00" * 64), "approve", spend_tokens={weth}))

    def leg(self, prep, side="buy", pair=None):
        cfg = {"rpc": "r", "chain_id": 8453, "weth": "0x" + "4" * 40, "api_chain": "base"}
        sent = []
        def send(rpc, cid, key, tx): sent.append(tx); return True, "0xhash"
        with mock.patch.object(bm, "_pair_token", lambda *a: pair or cfg["weth"]), \
             mock.patch.object(bm.api, "prepare_trade", lambda *a, **k: prep), \
             mock.patch.object(bm, "_sign_and_send", send), mock.patch.object(bm, "_wait_receipt", lambda *a, **k: True):
            res = bm._basestonk_leg(cfg, "0x1", WALLET, "0x" + "6" * 40, side, 10**15)
        return res, sent

    def test_leg_refuses_a_swap_that_is_really_a_transfer(self):
        res, sent = self.leg({"tx": self.tx(data="0xa9059cbb" + "00" * 64)})
        self.assertFalse(res[0]); self.assertIn("not signed", res[1]); self.assertEqual(sent, [])

    def test_leg_refuses_a_swap_that_sends_too_much_eth(self):
        res, sent = self.leg({"tx": self.tx(value=str(10**18))})
        self.assertFalse(res[0]); self.assertEqual(sent, [])

    def test_leg_refuses_a_bad_approval(self):
        res, sent = self.leg({"approvalTx": self.tx(to="0x" + "9" * 40, data="0x095ea7b3" + "00" * 64), "tx": self.tx()})
        self.assertFalse(res[0]); self.assertIn("approval not signed", res[1]); self.assertEqual(sent, [])

    def test_leg_signs_a_normal_swap(self):
        res, sent = self.leg({"tx": self.tx()})
        self.assertTrue(res[0]); self.assertEqual(len(sent), 1)

    def test_eth_value_allowed_only_when_spending_native_on_a_buy(self):
        res, sent = self.leg({"tx": self.tx(value=str(10**15))}, "buy")             # pair == weth: ok up to amount_in
        self.assertTrue(res[0])
        res, sent = self.leg({"tx": self.tx(value=str(10**15))}, "sell")
        self.assertFalse(res[0])
        res, sent = self.leg({"tx": self.tx(value=str(10**15))}, "buy", pair="0x" + "7" * 40)
        self.assertFalse(res[0])


class Gas(unittest.TestCase):
    def test_absurd_gas_price_is_not_signed(self):
        with mock.patch.object(bm, "_gas_price", lambda rpc: int(500e9)), mock.patch.object(bm, "_rpc", side_effect=AssertionError("sent")):
            ok, why = bm._sign_and_send("r", 8453, "0x1", {"to": "0x" + "5" * 40, "data": "0x12345678"})
        self.assertFalse(ok); self.assertIn("gas price too high", why)

    def test_chain_id_and_gas_come_from_us(self):
        captured = {}
        class A(_Acct):
            def sign_transaction(self, tx): captured.update(tx); raise StopIteration
        with mock.patch.object(fake_eth.Account, "from_key", staticmethod(lambda k: A())), \
             mock.patch.object(bm, "_gas_price", lambda rpc: int(1e9)), mock.patch.object(bm, "_nonce", lambda *a: 0):
            with self.assertRaises(StopIteration):
                bm._sign_and_send("r", 8453, "0x1", {"to": "0x" + "5" * 40, "data": "0x12345678", "chainId": 1, "gas": 10**9})
        self.assertEqual(captured["chainId"], 8453); self.assertEqual(captured["gas"], bm.MAX_TX_GAS)


class Start(unittest.TestCase):
    def test_rejects_odd_input(self):
        async def go():
            ok = "0x" + "1" * 40
            self.assertIn("ordinary numbers", bm.start(1, "base", ok, float("nan"), 10, 15, None))
            self.assertIn("ordinary numbers", bm.start(1, "base", ok, 2, float("inf"), 15, None))
            self.assertIn("0x contract", bm.start(1, "base", "0x" + "z" * 40, 2, 10, 15, None))
            self.assertIn("0x contract", bm.start(1, "base", "0x1234", 2, 10, 15, None))
        run(go())

    def test_linked_address_reads_without_the_key(self):
        c = sqlite3.connect(os.environ["DB_PATH"])
        c.execute("CREATE TABLE IF NOT EXISTS user_wallets (user_id INTEGER PRIMARY KEY, evm_pub TEXT, evm_key TEXT)")
        c.execute("INSERT OR REPLACE INTO user_wallets VALUES (42, ?, 'not-decryptable')", (WALLET,)); c.commit(); c.close()
        with mock.patch.object(bm, "FERZAN_DB_PATH", Path(os.environ["DB_PATH"])):
            self.assertEqual(bm.get_linked_evm_address(42), WALLET)
            self.assertIsNone(bm.get_linked_evm_address(43))


class Loop(unittest.TestCase):
    """The money loop, with every chain call replaced."""

    def drive(self, buy, sell, price=3000.0, minutes=30, budget=50.0, trade=2.0):
        trades, notes = [], []
        async def notify(t): notes.append(t)
        def do_trade(cfg, key, addr, token, side, amt, *a):
            trades.append(side); return (buy if side == "buy" else sell)(bm._active[9])
        async def main():
            bm._active[9] = {"stop": False}
            with mock.patch.object(bm, "get_linked_evm_key", lambda u: (WALLET, "0x1")), \
                 mock.patch.object(bm, "_native_usd", lambda cg: price), \
                 mock.patch.object(bm, "_native_balance_wei", lambda *a: int(1e18)), \
                 mock.patch.object(bm, "_erc20_balance", lambda *a: 10), \
                 mock.patch.object(bm, "_do_trade", do_trade), \
                 mock.patch.object(bm.asyncio, "sleep", mock.AsyncMock()):
                await bm._run_inner(9, "base", "0x" + "1" * 40, trade, budget, minutes, notify)
        run(main()); bm._active.pop(9, None)
        return trades, notes

    def test_no_live_price_means_no_trades(self):
        trades, notes = self.drive(lambda e: (True, "ok", "0x1"), lambda e: (True, "ok", "0x2"), price=0.0)
        self.assertEqual(trades, []); self.assertIn("live price", notes[0])

    def test_stops_after_two_failed_sells_even_if_buys_succeed(self):
        trades, notes = self.drive(lambda e: (True, "ok", "0x1"), lambda e: (False, "swap reverted", "0x2"))
        self.assertEqual(trades, ["buy", "sell", "buy", "sell"])
        self.assertTrue(any("sells failing" in n for n in notes))

    def test_stop_pressed_mid_round_still_sells(self):
        def buy(entry): entry["stop"] = True; return True, "ok", "0x1"
        trades, notes = self.drive(buy, lambda e: (True, "ok", "0x2"))
        self.assertEqual(trades, ["buy", "sell"])

    def test_unconfirmed_buys_still_count_against_the_budget(self):
        trades, notes = self.drive(lambda e: (False, "swap reverted, or didn't confirm in time", None), lambda e: (True, "ok", "0x"), budget=5.0)
        self.assertEqual(bm.status(9)["spent_usd"], 6.0)    # 3 unconfirmed rounds, each counted
        self.assertEqual(trades, ["buy"] * 3)

    def test_leftover_tokens_are_flagged_at_the_end(self):
        trades, notes = self.drive(lambda e: (True, "ok", "0x1"), lambda e: (False, "x", None))
        self.assertIn("still in your wallet", notes[-1])


class Wiring(unittest.TestCase):
    def test_source_wiring(self):
        bot = (ROOT / "liq_bot.py").read_text()
        self.assertIn("_paywall_text(uid)", bot)
        self.assertNotIn("Couldn't reach Base RPC: {exc}", bot)
        self.assertNotIn('f"Lookup failed: {exc}"', bot)
        mm = (ROOT / "liq/basestonk_mm.py").read_text()
        self.assertNotIn("or 3000.0", mm)


if __name__ == "__main__":
    unittest.main()
