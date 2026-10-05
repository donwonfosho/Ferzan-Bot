import os, re, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["LAUNCH_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
from unittest import mock
for _m in ("solders", "solders.pubkey", "solders.keypair", "solders.signature", "solders.transaction", "solders.hash", "solders.system_program", "solders.instruction", "solders.message"):
    sys.modules.setdefault(_m, mock.MagicMock())
import launch_bot_db as ldb
import meteora_launch as ml

ROOT = Path(__file__).resolve().parent.parent
API = (ROOT / "api.py").read_text()
BOT = (ROOT / "launch_bot.py").read_text()


class FailGuard(unittest.TestCase):
    def setUp(self):
        ldb.init_db()
        with ldb._get_conn() as c:
            cols = [r[1] for r in c.execute("PRAGMA table_info(launch_requests)")]
            for rid, st in (("a", "built"), ("b", "submitted"), ("c", "confirmed")):
                c.execute("INSERT INTO launch_requests (id, telegram_user_id, chat_id, chain, mode, name, symbol, total_supply, status, created_at, updated_at) "
                          "VALUES (?,1,1,'solana','meteora','n','S','1',?,'2026-01-01','2026-01-01')", (rid, st))

    def status(self, rid):
        with ldb._get_conn() as c:
            return c.execute("SELECT status FROM launch_requests WHERE id=?", (rid,)).fetchone()[0]

    def test_only_unsent_can_fail(self):
        self.assertTrue(ldb.fail_if_open("a", "rejected"))
        self.assertFalse(ldb.fail_if_open("b", "late error"))
        self.assertFalse(ldb.fail_if_open("c", "forged"))
        self.assertEqual((self.status("a"), self.status("b"), self.status("c")), ("failed", "submitted", "confirmed"))


class Units(unittest.TestCase):
    def test_exponent_never_read_as_whole_sol(self):
        self.assertEqual(ml._dev_buy_lamports("5e-05"), 0)
        self.assertEqual(ml._dev_buy_lamports("0.00005"), 50_000)
        self.assertEqual(ml._dev_buy_lamports("0.5 SOL"), 500_000_000)

    def test_dev_buy_stored_without_exponent(self):
        self.assertNotIn('f"{amount:g}" if amount > 0', BOT.split("def _set_devbuy")[1][:1500])

    def test_minutes_window_regex(self):
        self.assertIn('re.fullmatch(r"\\d+", text)', BOT)

    def test_share_page_does_not_shadow_sharer(self):
        blk = API.split("def share_page")[1].split("\n@app.")[0]
        self.assertNotIn("        r = c.execute", blk)
        self.assertIn("crow", blk)

    def test_payout_wallet_is_evm_only(self):
        self.assertIn('"solana") and _re.fullmatch(r"0x[0-9a-fA-F]{40}"', API)


class ConfirmOnce(unittest.TestCase):
    def setUp(self):
        ldb.init_db()
        with ldb._get_conn() as c:
            c.execute("DELETE FROM launch_requests")
            for rid in ("r1", "r2", "r3"):
                c.execute("INSERT INTO launch_requests (id, telegram_user_id, chat_id, chain, mode, name, symbol, total_supply, status, created_at, updated_at) "
                          "VALUES (?,1,1,'base','bonding_curve','n','S','1','built','2026-01-01','2026-01-01')", (rid,))

    def test_first_wins_second_is_already(self):
        self.assertEqual(ldb.confirm_once("r1", "0xAB", "0xTok"), "ok")
        self.assertEqual(ldb.confirm_once("r1", "0xAB", "0xTok"), "already")

    def test_same_tx_or_token_cannot_confirm_another_request(self):
        self.assertEqual(ldb.confirm_once("r1", "0xAB", "0xTok"), "ok")
        self.assertEqual(ldb.confirm_once("r2", "0xab", ""), "duplicate")      # same tx, different case
        self.assertEqual(ldb.confirm_once("r3", "0xzz", "0xTOK"), "duplicate")  # same token
        self.assertEqual(ldb.confirm_once("r3", "0xzz", "0xOther"), "ok")

    def test_empty_values_never_collide(self):
        self.assertEqual(ldb.confirm_once("r1", "", ""), "ok")
        self.assertEqual(ldb.confirm_once("r2", "", ""), "ok")

    def test_unknown_request(self):
        self.assertEqual(ldb.confirm_once("nope", "0x1", "0x2"), "missing")


class CompleteWiring(unittest.TestCase):
    def test_complete_uses_atomic_confirm_and_never_trusts_caller_token(self):
        blk = API.split("def complete_request")[1].split("\n# ---- SITE_LAUNCH")[0]
        self.assertIn("db.confirm_once(", blk)
        self.assertNotIn('db.update_status(\n        request_id, "confirmed"', blk)
        self.assertIn("_checked", blk)

    def test_evm_launch_bound_to_request_name_and_symbol(self):
        self.assertIn("eth_getTransactionByHash", API.split("def _verify_site_launch")[1].split("class SolBroadcastBody")[0])

    def test_solana_mint_pinned_for_every_source(self):
        self.assertIn('"sol_mints"', API)
        self.assertNotIn('if _ex0.get("source") == "tradebot_wallet":  # /complete only accepts THIS coin', API)


if __name__ == "__main__":
    unittest.main()
