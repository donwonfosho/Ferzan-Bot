"""Buy app: add a token from the app (look-up, track, limits) and groups the bot was added to (no network).

  cd "Launch Bot" && python -m unittest tests.test_buy_app_track
"""
import os
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from tests.test_buy_app import ADMIN, OTHER, SCHEMA, TOKEN, init_data

CHAT = -2001
EVM = "0x" + "cd" * 20
SOL = "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"
TRON = "T" + "A" * 33
TON = "EQ" + "A" * 46


class Track(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "b.db")
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.execute("CREATE TABLE known_groups (chat_id INTEGER PRIMARY KEY, title TEXT, ts INTEGER)")
        con.commit()
        con.close()
        self.env = mock.patch.dict(os.environ, {"BUYBOT_TOKEN": TOKEN, "BUYBOT_DB": self.db})
        self.env.start()
        import buy_app as B
        self.B = B
        B._rate.clear(); B._groups_cache.clear(); B._title_cache.clear()
        self.admins = {(CHAT, ADMIN)}

        def fake_tg(method, **p):
            if method == "getChatMember":
                return {"status": "administrator" if (p["chat_id"], p["user_id"]) in self.admins else "member"}
            if method == "getChat":
                return {"title": "Velkar"}
            return True
        self.patches = [mock.patch.object(B, "_tg", fake_tg),
                        mock.patch.object(B, "_lookup_pool", lambda c, a: {"pool": "POOL1", "name": "Velkar", "symbol": "VLK", "dex": "uniswap"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        self.tmp.cleanup()

    def row(self, sql, *a):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, a).fetchone()
        finally:
            con.close()

    def track(self, chain="base", ca=EVM, uid=ADMIN, chat=CHAT, **kw):
        return self.B.track(self.B.TrackBody(initData=init_data(uid), chat_id=chat, chain=chain, ca=ca, **kw))

    def status(self, fn):
        with self.assertRaises(self.B.HTTPException) as c:
            fn()
        return c.exception.status_code

    def test_address_checks_per_chain(self):
        ok = [("base", EVM), ("eth", EVM.upper().replace("0X", "0x")), ("sol", SOL), ("tron", TRON), ("ton", TON), ("ton", "UQ" + "b" * 46)]
        for c, a in ok:
            self.assertEqual(self.B._clean_token(c, a)[0], c)
        bad = [("base", SOL), ("sol", EVM), ("tron", EVM), ("ton", EVM), ("base", EVM + "00"), ("base", "0x123"),
               ("moon", EVM), ("base", EVM + "/../x"), ("base", EVM + "?a=b"), ("sol", SOL + " x"), ("base", "")]
        for c, a in bad:
            self.assertEqual(self.status(lambda: self.B._clean_token(c, a)), 400, (c, a))

    def test_evm_addresses_are_stored_lowercase(self):
        self.assertEqual(self.B._clean_token("base", "0x" + "AB" * 20)[1], "0x" + "ab" * 20)
        self.assertEqual(self.B._clean_token("sol", SOL)[1], SOL)

    def test_track_first_token_in_a_new_group(self):
        out = self.track(min_usd=40, sell=True, whale=2500)
        self.assertTrue(out["tracking"])
        self.assertEqual(out["added"]["name"], "Velkar")
        r = self.row("SELECT chain, ca, pool, min_usd, whale_usd, sell_alerts FROM watches WHERE chat_id=?", CHAT)
        self.assertEqual(r, ("base", EVM, "POOL1", 40.0, 2500.0, 1))
        self.assertEqual(self.row("SELECT action, admin_id FROM miniapp_audit WHERE chat_id=?", CHAT)[1], ADMIN)

    def test_second_token_inherits_the_groups_look(self):
        self.track(min_usd=40, sell=True)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE watches SET emoji='🚀', tg_url='https://t.me/velkar', whale_usd=900 WHERE chat_id=?", (CHAT,))
        con.commit(); con.close()
        self.track(chain="sol", ca=SOL)
        r = self.row("SELECT emoji, tg_url, whale_usd, sell_alerts, min_usd FROM watches WHERE chat_id=? AND chain='sol'", CHAT)
        self.assertEqual(r, ("🚀", "https://t.me/velkar", 900.0, 1, 40.0))

    def test_duplicate_is_refused_even_with_different_case(self):
        self.track()
        self.assertEqual(self.status(lambda: self.track(ca=EVM.replace("cd", "CD"))), 409)

    def test_group_is_limited_to_five_tokens(self):
        for i in range(5):
            self.track(ca="0x" + f"{i:02x}" * 20)
        self.assertEqual(self.status(lambda: self.track(ca="0x" + "99" * 20)), 400)

    def test_no_pool_means_nothing_is_saved(self):
        with mock.patch.object(self.B, "_lookup_pool", lambda c, a: None):
            self.assertEqual(self.status(lambda: self.track()), 404)
        self.assertIsNone(self.row("SELECT 1 FROM watches"))

    def test_non_admin_cannot_look_up_or_track(self):
        self.assertEqual(self.status(lambda: self.track(uid=OTHER)), 403)
        self.assertEqual(self.status(lambda: self.B.lookup(self.B.TrackBody(initData=init_data(OTHER), chat_id=CHAT, chain="base", ca=EVM))), 403)
        self.assertIsNone(self.row("SELECT 1 FROM watches"))

    def test_bad_minimum_is_refused_before_anything_is_written(self):
        self.assertEqual(self.status(lambda: self.track(min_usd=0)), 400)
        self.assertEqual(self.status(lambda: self.track(min_usd=-5)), 400)
        self.assertIsNone(self.row("SELECT 1 FROM watches"))

    def test_lookup_changes_nothing_and_reports_found_or_not(self):
        out = self.B.lookup(self.B.TrackBody(initData=init_data(ADMIN), chat_id=CHAT, chain="base", ca=EVM))
        self.assertEqual((out["found"], out["symbol"]), (True, "VLK"))
        with mock.patch.object(self.B, "_lookup_pool", lambda c, a: None):
            out = self.B.lookup(self.B.TrackBody(initData=init_data(ADMIN), chat_id=CHAT, chain="base", ca=EVM))
        self.assertFalse(out["found"])
        self.assertIsNone(self.row("SELECT 1 FROM watches"))

    def test_lookup_rate_limit(self):
        for _ in range(20):
            self.B.lookup(self.B.TrackBody(initData=init_data(ADMIN), chat_id=CHAT, chain="base", ca=EVM))
        self.assertEqual(self.status(lambda: self.B.lookup(self.B.TrackBody(initData=init_data(ADMIN), chat_id=CHAT, chain="base", ca=EVM))), 429)

    def test_group_added_but_not_tracking_is_listed_and_viewable(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO known_groups(chat_id, title, ts) VALUES(?,?,?)", (CHAT, "Velkar", int(time.time())))
        con.commit(); con.close()
        g = self.B.groups(self.B.AppBody(initData=init_data(ADMIN)))
        self.assertEqual([x["chat_id"] for x in g["groups"]], [CHAT])
        out = self.B.group(self.B.GroupBody(initData=init_data(ADMIN), chat_id=CHAT))
        self.assertFalse(out["tracking"])
        self.assertEqual(out["tokens"], [])

    def test_untracking_the_last_token_keeps_the_group_in_the_list(self):
        self.track()
        out = self.B.untrack(self.B.UntrackBody(initData=init_data(ADMIN), chat_id=CHAT, chain="base", ca=EVM))
        self.assertFalse(out["left"])
        self.B._groups_cache.clear()
        g = self.B.groups(self.B.AppBody(initData=init_data(ADMIN)))
        self.assertEqual([x["chat_id"] for x in g["groups"]], [CHAT])


class LookupSources(unittest.TestCase):
    def setUp(self):
        import buy_app as B
        self.B = B

    def test_dexscreener_pair_on_the_right_chain_wins(self):
        pairs = {"pairs": [{"chainId": "ethereum", "pairAddress": "WRONG"},
                           {"chainId": "base", "pairAddress": "RIGHT", "dexId": "aerodrome", "baseToken": {"name": "Velkar", "symbol": "VLK"}}]}
        with mock.patch.object(self.B, "_get_json", lambda url, **k: pairs):
            hit = self.B._lookup_pool("base", EVM)
        self.assertEqual((hit["pool"], hit["symbol"]), ("RIGHT", "VLK"))

    def test_falls_back_to_geckoterminal_then_ferzan_curve(self):
        def gecko(url, **k):
            if "dexscreener" in url:
                return {"pairs": []}
            if "geckoterminal" in url:
                return {"data": [{"id": "base_0xPOOL", "attributes": {"name": "VLK / WETH"}}]}
            return None
        with mock.patch.object(self.B, "_get_json", gecko):
            self.assertEqual(self.B._lookup_pool("base", EVM)["pool"], "0xPOOL")

        def curve(url, **k):
            if "curve-by-token" in url:
                return {"found": True, "name": "Velkar", "symbol": "VLK", "graduated": False}
            return {"pairs": [], "data": []} if "dexscreener" in url else None
        with mock.patch.object(self.B, "_get_json", curve):
            self.assertEqual(self.B._lookup_pool("base", EVM)["pool"], "ferzan:" + EVM)
            self.assertIsNone(self.B._lookup_pool("sol", SOL))

    def test_graduated_curve_token_is_not_taken_from_the_curve(self):
        def curve(url, **k):
            if "curve-by-token" in url:
                return {"found": True, "graduated": True}
            return {"pairs": [], "data": []}
        with mock.patch.object(self.B, "_get_json", curve):
            self.assertIsNone(self.B._lookup_pool("base", EVM))


if __name__ == "__main__":
    unittest.main()
