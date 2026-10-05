import asyncio, logging, os, re, sys, tempfile, time, types, unittest
from pathlib import Path
from unittest import mock
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import db
import evm_signer as ev
import sendstate

TXH = "0x" + "cd" * 32
META = {"rpc": "http://x", "chain_id": 8453, "explorer_tx": "https://basescan.org/tx/{txid}"}


def sliced():
    src = Path(HERE, "bot.py").read_text()
    a = src.index("PENDING_FILL_MAX_AGE_S =")
    b = src.index("async def pending_fill_job")
    c = src.index("\nasync def live_exit_job")
    return src[a:c]


class Signer(unittest.TestCase):
    def test_unconfirmed_is_reported_once(self):
        with mock.patch.object(ev, "_confirm_tx", return_value=None):
            ok, msg = ev._await_fill(META, TXH, "ok", "BASE buy")
        self.assertFalse(ok)
        self.assertEqual(ev.take_unconfirmed(), (8453, TXH))
        self.assertIsNone(ev.take_unconfirmed())

    def test_confirmed_and_reverted_report_nothing(self):
        for st in (True, False):
            with mock.patch.object(ev, "_confirm_tx", return_value=st):
                ev._await_fill(META, TXH, "ok", "BASE buy")
            self.assertIsNone(ev.take_unconfirmed())

    def test_receipt_status(self):
        with mock.patch.dict(ev.CHAINS, {"base": META}, clear=False), \
                mock.patch.object(ev, "_rpc", return_value={"result": {"status": "0x1"}}):
            self.assertTrue(ev.receipt_status(8453, TXH))
        with mock.patch.dict(ev.CHAINS, {"base": META}, clear=False), \
                mock.patch.object(ev, "_rpc", return_value={"result": {"status": "0x0"}}):
            self.assertFalse(ev.receipt_status(8453, TXH))
        with mock.patch.dict(ev.CHAINS, {"base": META}, clear=False), \
                mock.patch.object(ev, "_rpc", return_value={"result": None}):
            self.assertIsNone(ev.receipt_status(8453, TXH))
        self.assertIsNone(ev.receipt_status(999999, TXH))


class Settle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = db.DB_PATH
        db.DB_PATH = Path(self.tmp.name) / "t.db"
        db.init_db()
        self.sent = []
        ns = {"asyncio": asyncio, "time": time, "db": db, "sendstate": sendstate, "evm_signer": ev,
              "logger": logging.getLogger("t"), "ContextTypes": types.SimpleNamespace(DEFAULT_TYPE=object),
              "_log_trade_safe": lambda *a, **k: self.logged.append(a)}
        self.logged = []
        self.fee = types.SimpleNamespace(enabled=lambda: False, exempt=lambda u: False, live_bps=lambda *a: 100)
        ns["feecollect"] = self.fee
        ns["user_wallets"] = types.SimpleNamespace(secrets=lambda u: ("s", "e"))
        exec(sliced(), ns)
        self.ns = ns

        class Bot:
            async def send_message(inner, chat_id, text):
                self.sent.append((chat_id, text))
        self.ctx = types.SimpleNamespace(bot=Bot())

    def tearDown(self):
        db.DB_PATH = self.old
        self.tmp.cleanup()

    def add(self, **k):
        d = dict(user_id=7, mint="0x" + "11" * 20, chain="base", chain_id=8453, tx_hash=TXH, usd=25.0, label="BASE")
        d.update(k)
        return db.add_pending_fill(**d)

    def run_job(self, st):
        with mock.patch.object(ev, "receipt_status", return_value=st):
            asyncio.run(self.ns["pending_fill_job"](self.ctx))

    def test_duplicate_hash_tracked_once(self):
        self.assertTrue(self.add())
        self.assertFalse(self.add())
        self.assertEqual(len(db.pending_fills_open()), 1)

    def test_late_success_records_basis_once(self):
        self.add()
        sendstate.mark(7, "0x" + "11" * 20, "buy")
        self.run_job(True)
        self.run_job(True)
        self.assertAlmostEqual(db.get_live_cost(7, "0x" + "11" * 20) if hasattr(db, "get_live_cost") else 25.0, 25.0)
        self.assertEqual(len(self.logged), 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("landed late", self.sent[0][1])
        self.assertFalse(sendstate.held(7, "0x" + "11" * 20, "buy"))
        self.assertEqual(db.pending_fills_open(), [])

    def test_basis_value(self):
        self.add()
        self.run_job(True)
        with db.get_conn() as c:
            row = c.execute("SELECT cost_usd FROM live_basis WHERE user_id=7").fetchone()
        self.assertAlmostEqual(row["cost_usd"], 25.0)

    def test_revert_records_nothing(self):
        self.add()
        self.run_job(False)
        self.assertEqual(self.logged, [])
        with db.get_conn() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM live_basis WHERE user_id=7").fetchone())
        self.assertIn("reverted", self.sent[0][1])
        self.assertEqual(db.pending_fills_open(), [])

    def test_still_unknown_keeps_waiting_then_expires(self):
        self.add()
        self.run_job(None)
        self.assertEqual(len(db.pending_fills_open()), 1)
        self.assertEqual(self.sent, [])
        with db.get_conn() as c:
            c.execute("UPDATE pending_fills SET created_at = ?", (int(time.time()) - 7 * 3600,))
            c.commit()
        self.run_job(None)
        self.assertEqual(db.pending_fills_open(), [])
        self.assertIn("probably dropped", self.sent[0][1])

    def test_no_basis_when_not_recorded_for_extra_wallets(self):
        self.add(record_basis=False)
        self.run_job(True)
        with db.get_conn() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM live_basis WHERE user_id=7").fetchone())
        self.assertEqual(len(self.logged), 1)

    def test_live_buy_wires_it_up(self):
        src = Path(HERE, "bot.py").read_text()
        self.assertIn("db.add_pending_fill(uid, mint", src)
        self.assertIn("jq.run_repeating(pending_fill_job", src)
        self.assertLess(src.index("evm_signer.take_unconfirmed()  # drop"), src.index("late = None if ok"))


if __name__ == "__main__":
    unittest.main()
