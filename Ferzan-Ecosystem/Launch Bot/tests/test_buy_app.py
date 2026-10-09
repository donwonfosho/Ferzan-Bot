"""Buy Bot Mini App backend: sign-in check, live admin check, settings writes, untrack, audit (no network).

  cd "Launch Bot" && python -m unittest tests.test_buy_app
"""
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

TOKEN = "777:BUYTEST"
ADMIN, OTHER, CHAT = 111, 222, -1001


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

    m.HTTPException, m.APIRouter = HTTPException, APIRouter
    sys.modules["fastapi"] = m


_stub_fastapi()


def init_data(uid, token=TOKEN, age=0):
    pairs = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": uid, "first_name": "Al"}), "query_id": "q"}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


SCHEMA = """
CREATE TABLE watches (chat_id INTEGER, chain TEXT, ca TEXT, pool TEXT, last_ts INTEGER DEFAULT 0, min_usd REAL DEFAULT 15,
 emoji TEXT DEFAULT '🟢', tg_url TEXT, discord_url TEXT, x_url TEXT, whale_usd REAL DEFAULT 0, sell_alerts INTEGER DEFAULT 0,
 PRIMARY KEY (chat_id, chain, ca));
CREATE TABLE chat_flags (chat_id INTEGER PRIMARY KEY, tape INTEGER DEFAULT 1, mute_until INTEGER DEFAULT 0, raid_pin INTEGER DEFAULT 0, last_recap INTEGER DEFAULT 0);
CREATE TABLE buy_log (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, ca TEXT, usd REAL, ts INTEGER, buyer TEXT, kind TEXT DEFAULT 'buy');
"""
CA1 = "0x" + "ab" * 20
CA2 = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"


class BuyApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "b.db")
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        for chain, ca in (("base", CA1), ("solana", CA2)):
            con.execute("INSERT INTO watches(chat_id,chain,ca,pool,last_ts) VALUES(?,?,?,?,?)", (CHAT, chain, ca, "p", int(time.time())))
        con.execute("INSERT INTO watches(chat_id,chain,ca,pool,last_ts) VALUES(-1002,'base',?, 'p', 1)", (CA1,))
        con.execute("INSERT INTO buy_log(chat_id,ca,usd,ts) VALUES(?,?,?,?)", (CHAT, CA1, 120.5, int(time.time())))
        con.execute("INSERT INTO buy_log(chat_id,ca,usd,ts,kind) VALUES(?,?,?,?,'sell')", (CHAT, CA1, 900, int(time.time())))
        con.commit()
        con.close()
        self.env = mock.patch.dict(os.environ, {"BUYBOT_TOKEN": TOKEN, "BUYBOT_DB": self.db})
        self.env.start()
        import buy_app as B
        self.B = B
        B._rate.clear()
        B._groups_cache.clear()
        B._title_cache.clear()
        self.calls = []
        self.admins = {(CHAT, ADMIN), (-1002, 333)}

        def fake_tg(method, **p):
            self.calls.append((method, p))
            if method == "getChatMember":
                return {"status": "administrator" if (p["chat_id"], p["user_id"]) in self.admins else "member"}
            if method == "getChat":
                return {"title": f"Group {p['chat_id']}"}
            return True
        self.p = mock.patch.object(B, "_tg", fake_tg)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def row(self, sql, *a):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, a).fetchone()
        finally:
            con.close()

    def set(self, key, value, uid=ADMIN, chat=CHAT):
        return self.B.set_option(self.B.SetBody(initData=init_data(uid), chat_id=chat, key=key, value=value))

    def test_bad_sign_in_refused(self):
        for bad in ("", "x", init_data(ADMIN, token="1:OTHER"), init_data(ADMIN, age=30 * 3600)):
            with self.assertRaises(self.B.HTTPException) as c:
                self.B.group(self.B.GroupBody(initData=bad, chat_id=CHAT))
            self.assertEqual(c.exception.status_code, 401)

    def test_non_admin_and_other_group_admin_refused(self):
        for uid in (OTHER, 333):
            with self.assertRaises(self.B.HTTPException) as c:
                self.set("min", 50, uid=uid)
            self.assertEqual(c.exception.status_code, 403)
        self.assertEqual(self.row("SELECT COUNT(*) FROM sqlite_master WHERE name='miniapp_audit'")[0], 0)  # refused before anything was written
        self.assertEqual(self.row("SELECT min_usd FROM watches WHERE chat_id=?", CHAT)[0], 15)

    def test_groups_only_where_admin(self):
        out = self.B.groups(self.B.AppBody(initData=init_data(ADMIN)))
        self.assertEqual([g["chat_id"] for g in out["groups"]], [CHAT])

    def test_min_accepts_a_custom_amount_for_every_token_in_the_group_only(self):
        st = self.set("min", "75.5")
        self.assertEqual(st["min_usd"], 75.5)
        self.assertEqual({r[0] for r in sqlite3.connect(self.db).execute("SELECT min_usd FROM watches WHERE chat_id=?", (CHAT,))}, {75.5})
        self.assertEqual(self.row("SELECT min_usd FROM watches WHERE chat_id=-1002")[0], 15)
        self.assertEqual(self.set("min", "$1,000")["min_usd"], 1000.0)

    def test_min_rejects_nonsense(self):
        for bad in ("", "abc", "0", "-5", "0.5", "1e999", "nan", "inf", 10_000_000, "5; DROP TABLE watches"):
            with self.assertRaises(self.B.HTTPException, msg=str(bad)):
                self.set("min", bad)

    def test_whale_custom_and_default(self):
        self.assertEqual(self.set("whale", 2500)["whale_usd"], 2500.0)
        self.assertEqual(self.set("whale", 0)["whale_usd"], 0.0)
        with self.assertRaises(self.B.HTTPException):
            self.set("whale", -1)

    def test_emoji_rules(self):
        self.assertEqual(self.set("emoji", "🚀")["emoji"], "🚀")
        for bad in ("", "abc", "<b>", "&", "x" * 20, "🚀" * 9):
            with self.assertRaises(self.B.HTTPException, msg=bad):
                self.set("emoji", bad)

    def test_switches(self):
        self.assertFalse(self.set("tape", False)["tape"])
        self.assertEqual(self.row("SELECT tape FROM chat_flags WHERE chat_id=?", CHAT)[0], 0)
        self.assertTrue(self.set("tape", True)["tape"])
        self.assertTrue(self.set("sell", True)["sell"])
        self.assertTrue(self.set("pin", True)["pin"])
        st = self.set("mute", True)
        self.assertTrue(st["muted"])
        self.assertFalse(self.set("mute", False)["muted"])

    def test_links_only_from_allowed_sites(self):
        self.assertEqual(self.set("link_tg", "t.me/velkar")["links"]["link_tg"], "https://t.me/velkar")
        self.assertEqual(self.set("link_x", "https://x.com/velkar")["links"]["link_x"], "https://x.com/velkar")
        self.assertEqual(self.set("link_tg", "")["links"]["link_tg"], "")
        for key, bad in (("link_tg", "https://evil.com/t.me"), ("link_tg", "https://t.me.evil.com/x"), ("link_x", "javascript:alert(1)"),
                         ("link_discord", "https://discord.gg/a b"), ("link_tg", "http://t.me@evil.com"), ("link_x", "https://x.com/\"onclick")):
            with self.assertRaises(self.B.HTTPException, msg=bad):
                self.set(key, bad)

    def test_unknown_setting_and_sql_in_key_refused(self):
        for k in ("min_usd=1; --", "tg_url", "emoji; DROP TABLE watches", ""):
            with self.assertRaises(self.B.HTTPException):
                self.set(k, 1)
        self.assertIsNotNone(self.row("SELECT 1 FROM watches LIMIT 1"))

    def test_every_change_is_audited_with_admin_id(self):
        self.set("min", 50)
        self.assertEqual(self.row("SELECT admin_id, action, detail FROM miniapp_audit"), (ADMIN, "min", "$50"))

    def test_untrack_removes_one_token_only(self):
        out = self.B.untrack(self.B.UntrackBody(initData=init_data(ADMIN), chat_id=CHAT, chain="base", ca=CA1))
        self.assertTrue(out["left"])
        self.assertEqual([t["chain"] for t in out["tokens"]], ["solana"])
        self.assertIsNotNone(self.row("SELECT 1 FROM watches WHERE chat_id=-1002"))
        with self.assertRaises(self.B.HTTPException) as c:
            self.B.untrack(self.B.UntrackBody(initData=init_data(ADMIN), chat_id=CHAT, chain="base", ca=CA1))
        self.assertEqual(c.exception.status_code, 404)
        with self.assertRaises(self.B.HTTPException) as c:
            self.B.untrack(self.B.UntrackBody(initData=init_data(OTHER), chat_id=CHAT, chain="solana", ca=CA2))
        self.assertEqual(c.exception.status_code, 403)

    def test_group_view_counts_buys_not_sells(self):
        out = self.B.group(self.B.GroupBody(initData=init_data(ADMIN), chat_id=CHAT))
        self.assertEqual((out["buys_today"], out["volume_today"]), (1, 120.5))
        self.assertEqual(len(out["tokens"]), 2)
        self.assertEqual(len(out["recent"]), 2)

    def test_untracked_group_is_refused(self):
        self.admins.add((-77, ADMIN))
        with self.assertRaises(self.B.HTTPException) as c:
            self.set("min", 50, chat=-77)
        self.assertEqual(c.exception.status_code, 404)

    def test_write_rate_limit(self):
        for _ in range(60):
            self.set("tape", True)
        with self.assertRaises(self.B.HTTPException) as c:
            self.set("tape", True)
        self.assertEqual(c.exception.status_code, 429)


if __name__ == "__main__":
    unittest.main()
