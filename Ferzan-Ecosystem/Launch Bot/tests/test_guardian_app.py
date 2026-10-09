"""Guardian Mini App backend: sign-in check, live admin check, settings writes, report actions (no network).

  cd "Launch Bot" && python -m pytest tests/test_guardian_app.py
"""
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

TOKEN = "123456:TESTTOKEN"
ADMIN, OTHER, CHAT = 111, 222, -1001

def _stub_fastapi():
    """Only the three names these routes use, so the tests run where fastapi is not installed."""
    try:
        import fastapi  # noqa: F401
        return
    except ImportError:
        pass
    import types

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
HAVE = True


def init_data(uid, token=TOKEN, age=0):
    pairs = {"auth_date": str(int(time.time()) - age), "user": json.dumps({"id": uid, "first_name": "Al"}), "query_id": "q"}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


SCHEMA = """
CREATE TABLE settings (chat_id INTEGER PRIMARY KEY, antiname INTEGER DEFAULT 1, antilink INTEGER DEFAULT 1, antimedia INTEGER DEFAULT 0,
 captcha_enabled INTEGER DEFAULT 0, captcha_mode TEXT DEFAULT 'button', caguard_enabled INTEGER DEFAULT 0, official_cas TEXT,
 lock_forwards INTEGER DEFAULT 0, lock_stickers INTEGER DEFAULT 0, slowmode_seconds INTEGER DEFAULT 0, warn_limit INTEGER DEFAULT 3);
CREATE TABLE known_chats (chat_id INTEGER PRIMARY KEY, title TEXT, last_seen INTEGER);
CREATE TABLE joins (chat_id INTEGER, user_id INTEGER, joined_ts INTEGER, PRIMARY KEY (chat_id, user_id));
CREATE TABLE warns (chat_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0, last_ts INTEGER DEFAULT 0, PRIMARY KEY (chat_id, user_id));
CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, reporter_id INTEGER, target_id INTEGER, message_id INTEGER, text TEXT, status TEXT DEFAULT 'open', resolved_by INTEGER, ts INTEGER);
CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, mod_id INTEGER, target_id INTEGER, action TEXT, reason TEXT, ts INTEGER);
CREATE TABLE config_audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, admin_id INTEGER, action TEXT, detail TEXT, ts INTEGER);
"""


@unittest.skipUnless(HAVE, "fastapi not installed")
class GuardianApp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "g.db")
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.execute("INSERT INTO known_chats VALUES(?,?,?)", (CHAT, "Velkar", int(time.time())))
        con.execute("INSERT INTO known_chats VALUES(?,?,?)", (-1002, "Other group", int(time.time())))
        con.execute("INSERT INTO reports(chat_id,reporter_id,target_id,text,ts) VALUES(?,?,?,?,?)", (CHAT, 5, 900, "scam link", int(time.time())))
        con.commit()
        con.close()
        self.env = mock.patch.dict(os.environ, {"GUARDIAN_TOKEN": TOKEN, "GUARDIAN_DB": self.db})
        self.env.start()
        import guardian_app as G
        self.G = G
        G._rate.clear()
        G._groups_cache.clear()
        self.calls = []
        self.admins = {(CHAT, ADMIN), (-1002, 333)}

        def fake_tg(method, **p):
            self.calls.append((method, p))
            if method == "getChatMember":
                ok = (p["chat_id"], p["user_id"]) in self.admins
                return {"status": "administrator" if ok else "member"}
            if method == "getChatMemberCount":
                return 2418
            return True
        self.p = mock.patch.object(G, "_tg", fake_tg)
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

    def set(self, key, value, uid=ADMIN, chat=CHAT, **kw):
        return self.G.set_option(self.G.SetBody(initData=init_data(uid), chat_id=chat, key=key, value=value))

    # sign-in
    def test_bad_signature_wrong_token_and_old_data_are_refused(self):
        for bad in ("", "garbage", init_data(ADMIN, token="1:OTHER"), init_data(ADMIN, age=30 * 3600)):
            with self.assertRaises(self.G.HTTPException) as c:
                self.G.group(self.G.GroupBody(initData=bad, chat_id=CHAT))
            self.assertEqual(c.exception.status_code, 401)

    def test_non_admin_cannot_read_or_change(self):
        with self.assertRaises(self.G.HTTPException) as c:
            self.G.group(self.G.GroupBody(initData=init_data(OTHER), chat_id=CHAT))
        self.assertEqual(c.exception.status_code, 403)
        with self.assertRaises(self.G.HTTPException) as c:
            self.set("antilink", False, uid=OTHER)
        self.assertEqual(c.exception.status_code, 403)
        self.assertIsNone(self.row("SELECT 1 FROM config_audit_log"))

    def test_admin_of_another_group_cannot_touch_this_one(self):
        with self.assertRaises(self.G.HTTPException) as c:
            self.set("antilink", False, uid=333)
        self.assertEqual(c.exception.status_code, 403)

    def test_groups_lists_only_where_admin(self):
        out = self.G.groups(self.G.AppBody(initData=init_data(ADMIN)))
        self.assertEqual([g["chat_id"] for g in out["groups"]], [CHAT])

    # settings
    def test_toggles_write_the_columns_the_bot_reads(self):
        for key, col in (("newcheck", "captcha_enabled"), ("antilink", "antilink"), ("caguard", "caguard_enabled"),
                         ("antiforward", "lock_forwards"), ("nostickers", "lock_stickers")):
            self.set(key, True)
            self.assertEqual(self.row(f"SELECT {col} FROM settings WHERE chat_id=?", CHAT)[0], 1, key)
            self.set(key, False)
            self.assertEqual(self.row(f"SELECT {col} FROM settings WHERE chat_id=?", CHAT)[0], 0, key)
        self.set("slow", True)
        self.assertEqual(self.row("SELECT slowmode_seconds FROM settings WHERE chat_id=?", CHAT)[0], 10)
        self.set("slow", False)
        self.assertEqual(self.row("SELECT slowmode_seconds FROM settings WHERE chat_id=?", CHAT)[0], 0)

    def test_captcha_mode_values_match_what_the_bot_decodes(self):
        for ui, stored in (("tap", "tap"), ("full", "button"), ("math", "math")):
            st = self.set("captcha_mode", ui)
            self.assertEqual(self.row("SELECT captcha_mode FROM settings WHERE chat_id=?", CHAT)[0], stored)
            self.assertEqual(st["captcha_mode"], ui)
        with self.assertRaises(self.G.HTTPException):
            self.set("captcha_mode", "nope")

    def test_warn_limit_only_2_3_5(self):
        self.assertEqual(self.set("warn_limit", 5)["warn_limit"], 5)
        for bad in (0, 1, 4, 99, "x"):
            with self.assertRaises(self.G.HTTPException):
                self.set("warn_limit", bad)

    def test_unknown_setting_refused_and_no_sql_through_key(self):
        for k in ("lock_links; DROP TABLE settings", "welcome_text", ""):
            with self.assertRaises(self.G.HTTPException):
                self.set(k, 1)
        self.assertIsNotNone(self.row("SELECT 1 FROM settings WHERE chat_id=?", CHAT) or (1,))

    def test_contract_list_validation_add_remove_and_limit(self):
        evm = "0x" + "ab" * 20
        sol = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
        self.set("ca_add", evm)
        self.set("ca_add", sol)
        self.set("ca_add", "ferzan")
        self.assertEqual(self.row("SELECT official_cas FROM settings WHERE chat_id=?", CHAT)[0], f"{evm} {sol} FERZAN")
        for bad in ("0x123", "hello world", "0x" + "zz" * 20, "", "<script>"):
            with self.assertRaises(self.G.HTTPException, msg=bad):
                self.set("ca_add", bad)
        with self.assertRaises(self.G.HTTPException):
            self.set("ca_add", evm)  # duplicate
        st = self.set("ca_remove", evm)
        self.assertEqual([c["value"] for c in st["cas"]], [sol, "FERZAN"])
        with self.assertRaises(self.G.HTTPException):
            self.set("ca_remove", evm)

    def test_every_change_is_logged_with_the_admin_id(self):
        self.set("antilink", False)
        r = self.row("SELECT admin_id, action, detail FROM config_audit_log")
        self.assertEqual(r, (ADMIN, "app:antilink", "off"))

    def test_write_rate_limit(self):
        for _ in range(60):
            self.set("antilink", True)
        with self.assertRaises(self.G.HTTPException) as c:
            self.set("antilink", True)
        self.assertEqual(c.exception.status_code, 429)

    def test_unknown_group_is_refused(self):
        self.admins.add((-5, ADMIN))
        with self.assertRaises(self.G.HTTPException) as c:
            self.set("antilink", False, chat=-5)
        self.assertEqual(c.exception.status_code, 404)
        self.assertIsNone(self.row("SELECT 1 FROM settings WHERE chat_id=-5"))

    def test_group_view(self):
        self.set("caguard", True)
        out = self.G.group(self.G.GroupBody(initData=init_data(ADMIN), chat_id=CHAT))
        self.assertEqual(out["members"], 2418)
        self.assertTrue(out["toggles"]["caguard"])
        self.assertEqual(len(out["reports"]), 1)
        self.assertEqual(out["activity"][0]["action"], "app:caguard")

    # reports
    def act(self, action, uid=ADMIN, rid=1):
        return self.G.report_action(self.G.ReportBody(initData=init_data(uid), chat_id=CHAT, report_id=rid, action=action))

    def test_report_dismiss_closes_without_telegram_action(self):
        self.act("dismiss")
        self.assertEqual(self.row("SELECT status, resolved_by FROM reports WHERE id=1"), ("resolved", ADMIN))
        self.assertFalse([c for c in self.calls if c[0] == "banChatMember"])
        with self.assertRaises(self.G.HTTPException):
            self.act("dismiss")

    def test_report_ban_bans_and_logs(self):
        self.act("ban")
        self.assertIn(("banChatMember", {"chat_id": CHAT, "user_id": 900}), self.calls)
        self.assertEqual(self.row("SELECT action, mod_id, target_id FROM audit_log"), ("ban", ADMIN, 900))

    def test_report_warn_counts_and_bans_at_limit(self):
        self.set("warn_limit", 2)
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO warns(chat_id,user_id,count) VALUES(?,?,1)", (CHAT, 900))
        con.commit()
        con.close()
        out = self.act("warn")
        self.assertTrue(out["banned"])
        self.assertEqual(self.row("SELECT count FROM warns WHERE user_id=900")[0], 2)

    def test_report_warn_below_limit_does_not_ban(self):
        out = self.act("warn")
        self.assertFalse(out["banned"])
        self.assertFalse([c for c in self.calls if c[0] == "banChatMember"])

    def test_cannot_act_on_an_admin(self):
        self.admins.add((CHAT, 900))
        with self.assertRaises(self.G.HTTPException):
            self.act("ban")
        self.assertEqual(self.row("SELECT status FROM reports WHERE id=1")[0], "open")

    def test_non_admin_cannot_act_on_reports(self):
        with self.assertRaises(self.G.HTTPException) as c:
            self.act("ban", uid=OTHER)
        self.assertEqual(c.exception.status_code, 403)

    def test_report_of_another_group_cannot_be_acted_on(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO reports(chat_id,target_id,text,ts) VALUES(-1002,901,'x',1)")
        con.commit()
        con.close()
        with self.assertRaises(self.G.HTTPException) as c:
            self.act("ban", rid=2)
        self.assertEqual(c.exception.status_code, 404)


class Pages(unittest.TestCase):
    def test_pages_offer_app_or_classic_like_the_other_bots(self):
        for name in ("guardian.html", "buybot.html"):
            html = (HERE / "miniapp" / name).read_text(encoding="utf-8")
            for needle in ('id="chooser"', 'id="pickApp"', 'id="pickClassic"', 'id="toClassic"', 'qs.get("from")==="menu"', "?start=home"):
                self.assertIn(needle, html, f"{name}: {needle}")


if __name__ == "__main__":
    unittest.main()
