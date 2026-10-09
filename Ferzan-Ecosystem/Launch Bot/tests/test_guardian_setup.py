"""Guardian app: protection levels (presets), undo, who-may-run-which-command, and drift against the bot's own tiers.

  cd "Launch Bot" && python -m unittest tests.test_guardian_setup
"""
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_guardian_app import ADMIN, CHAT, OTHER, TOKEN, init_data

SCHEMA = """
CREATE TABLE settings (chat_id INTEGER PRIMARY KEY, antilink INTEGER DEFAULT 1, captcha_enabled INTEGER DEFAULT 0,
 captcha_mode TEXT DEFAULT 'button', caguard_enabled INTEGER DEFAULT 0, official_cas TEXT, lock_forwards INTEGER DEFAULT 0,
 lock_stickers INTEGER DEFAULT 0, slowmode_seconds INTEGER DEFAULT 0, warn_limit INTEGER DEFAULT 3, rules_text TEXT,
 welcome_enabled INTEGER DEFAULT 0, goodbye_enabled INTEGER DEFAULT 0, cleanservice_enabled INTEGER DEFAULT 0,
 linkscan_enabled INTEGER DEFAULT 0, newacct_enabled INTEGER DEFAULT 0, adaptive_slowmode_enabled INTEGER DEFAULT 0,
 rules_gate_enabled INTEGER DEFAULT 0, lock_links INTEGER DEFAULT 0, votemute_enabled INTEGER DEFAULT 0,
 protection_tier TEXT, tier_backup TEXT);
CREATE TABLE known_chats (chat_id INTEGER PRIMARY KEY, title TEXT, last_seen INTEGER);
CREATE TABLE config_audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, admin_id INTEGER, action TEXT, detail TEXT, ts INTEGER);
CREATE TABLE joins (chat_id INTEGER, user_id INTEGER, joined_ts INTEGER);
CREATE TABLE reports (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, target_id INTEGER, text TEXT, status TEXT, ts INTEGER);
CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER, mod_id INTEGER, target_id INTEGER, action TEXT, reason TEXT, ts INTEGER);
CREATE TABLE command_perms (chat_id INTEGER, command TEXT, tier TEXT, PRIMARY KEY (chat_id, command));
"""


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "g.db")
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        con.execute("INSERT INTO known_chats VALUES(?,?,?)", (CHAT, "Velkar", int(time.time())))
        con.commit(); con.close()
        self.env = mock.patch.dict(os.environ, {"GUARDIAN_TOKEN": TOKEN, "GUARDIAN_DB": self.db})
        self.env.start()
        import guardian_app as G
        self.G = G
        G._rate.clear(); G._groups_cache.clear(); G._bot_id.clear()
        self.bot_admin = True

        def fake_tg(method, **p):
            if method == "getChatMember":
                if p["user_id"] == 999:
                    return {"status": "administrator" if self.bot_admin else "member"}
                return {"status": "administrator" if (p["chat_id"], p["user_id"]) == (CHAT, ADMIN) else "member"}
            if method == "getMe":
                return {"id": 999}
            if method == "getChatMemberCount":
                return 10
            return True
        self.p = mock.patch.object(G, "_tg", fake_tg)
        self.p.start()

    def tearDown(self):
        self.p.stop(); self.env.stop(); self.tmp.cleanup()

    def row(self, sql, *a):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, a).fetchone()
        finally:
            con.close()

    def tier(self, t, uid=ADMIN):
        return self.G.set_tier(self.G.TierBody(initData=init_data(uid), chat_id=CHAT, tier=t))

    def perm(self, cmd, t, uid=ADMIN):
        return self.G.set_perm(self.G.PermBody(initData=init_data(uid), chat_id=CHAT, command=cmd, tier=t))

    def status(self, fn):
        with self.assertRaises(self.G.HTTPException) as c:
            fn()
        return c.exception.status_code

    def test_standard_switches_on_the_join_check_and_the_rest(self):
        out = self.tier("standard")
        r = self.row("SELECT captcha_enabled, captcha_mode, antilink, welcome_enabled, warn_limit, protection_tier FROM settings WHERE chat_id=?", CHAT)
        self.assertEqual(r, (1, "button", 1, 1, 3, "standard"))
        self.assertEqual(out["tier"], "standard")
        self.assertEqual(self.row("SELECT action FROM config_audit_log")[0], "app:tier")

    def test_shield_skips_rules_gate_and_contract_guard_until_they_exist(self):
        out = self.tier("shield")
        r = self.row("SELECT rules_gate_enabled, caguard_enabled, linkscan_enabled FROM settings WHERE chat_id=?", CHAT)
        self.assertEqual(r, (0, 0, 1))
        self.assertEqual(len(out["notes"]), 2)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE settings SET rules_text='be kind', official_cas='FERZAN' WHERE chat_id=?", (CHAT,))
        con.commit(); con.close()
        out = self.tier("shield")
        self.assertEqual(self.row("SELECT rules_gate_enabled, caguard_enabled FROM settings WHERE chat_id=?", CHAT), (1, 1))
        self.assertEqual(out["notes"], [])

    def test_fortress_sets_slow_mode_floor_and_two_strikes_but_keeps_a_longer_slow_mode(self):
        self.tier("fortress")
        self.assertEqual(self.row("SELECT slowmode_seconds, warn_limit, lock_links, lock_forwards FROM settings WHERE chat_id=?", CHAT), (10, 2, 1, 1))
        con = sqlite3.connect(self.db)
        con.execute("UPDATE settings SET slowmode_seconds=60"); con.commit(); con.close()
        self.tier("fortress")
        self.assertEqual(self.row("SELECT slowmode_seconds FROM settings")[0], 60)

    def test_undo_restores_what_was_there_before(self):
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO settings(chat_id, warn_limit, antilink) VALUES(?,5,0)", (CHAT,)); con.commit(); con.close()
        self.tier("fortress")
        self.assertEqual(self.row("SELECT warn_limit, antilink FROM settings")[0], 2)
        out = self.tier("undo")
        self.assertEqual(self.row("SELECT warn_limit, antilink, protection_tier, tier_backup FROM settings"), (5, 0, None, None))
        self.assertEqual(out["tier"], "")
        self.assertEqual(self.status(lambda: self.tier("undo")), 400)

    def test_the_bot_can_read_the_backup_the_app_wrote(self):
        self.tier("shield")
        b = json.loads(self.row("SELECT tier_backup FROM settings")[0])
        self.assertIn("__tier", b)

    def test_unknown_level_and_non_admin_refused(self):
        self.assertEqual(self.status(lambda: self.tier("max")), 400)
        self.assertEqual(self.status(lambda: self.tier("standard", uid=OTHER)), 403)
        self.assertIsNone(self.row("SELECT 1 FROM settings"))

    def test_command_permissions(self):
        out = self.perm("gban", "mod")
        by = {p["command"]: p for p in out["perms"]}
        self.assertEqual((by["gban"]["tier"], by["gban"]["default"]), ("mod", "admin"))
        self.assertEqual((by["gmute"]["tier"], by["gmute"]["default"]), ("mod", "mod"))
        self.assertEqual(self.row("SELECT tier FROM command_perms WHERE chat_id=? AND command='gban'", CHAT)[0], "mod")
        self.perm("gban", "admin")
        self.assertEqual(self.row("SELECT COUNT(*) FROM command_perms")[0], 1)
        self.assertEqual(self.row("SELECT detail FROM config_audit_log ORDER BY id DESC")[0], "/gban -> admin")

    def test_command_permissions_reject_other_commands_values_and_non_admins(self):
        self.assertEqual(self.status(lambda: self.perm("gset; DROP TABLE settings", "mod")), 400)
        self.assertEqual(self.status(lambda: self.perm("start", "mod")), 400)
        self.assertEqual(self.status(lambda: self.perm("gban", "everyone")), 400)
        self.assertEqual(self.status(lambda: self.perm("gban", "mod", uid=OTHER)), 403)
        self.assertIsNone(self.row("SELECT 1 FROM command_perms"))

    def test_group_view_carries_setup_state_and_bot_admin_flag(self):
        out = self.G.group(self.G.GroupBody(initData=init_data(ADMIN), chat_id=CHAT))
        self.assertEqual(out["tier"], "")
        self.assertEqual([t["id"] for t in out["tiers"]], ["standard", "shield", "fortress"])
        self.assertEqual(len(out["perms"]), 6)
        self.assertTrue(out["bot_admin"])
        self.bot_admin = False
        out = self.G.group(self.G.GroupBody(initData=init_data(ADMIN), chat_id=CHAT))
        self.assertFalse(out["bot_admin"])

    def test_levels_match_the_bots_own_definitions(self):
        """If someone edits /gsetup in the bot, this fails until the app is updated too."""
        src = (Path(__file__).resolve().parents[2] / "Guardian Bot" / "guardian_bot.py").read_text()
        start = src.index("TIER_ORDER = ")
        end = src.index("_COL_LABEL = ")
        ns: dict = {}
        exec(src[start:end], ns)
        self.assertEqual(tuple(ns["TIER_ORDER"]), self.G.TIER_ORDER)
        for t in ns["TIER_ORDER"]:
            self.assertEqual(ns["TIERS"][t]["set"], self.G.TIERS[t]["set"], t)
            self.assertEqual(ns["TIERS"][t]["min"], self.G.TIERS[t]["min"], t)
        m = re.search(r'PERM_CUSTOMIZABLE_COMMANDS = \(([^)]*)\)', src)
        self.assertEqual(sorted(re.findall(r'"(\w+)"', m.group(1))), sorted(self.G.PERM_DEFAULTS))
        for cmd, dflt in self.G.PERM_DEFAULTS.items():
            self.assertRegex(src, rf'_check_cmd_perm\(update, context, "{cmd}", "{dflt}"\)')


if __name__ == "__main__":
    unittest.main()
