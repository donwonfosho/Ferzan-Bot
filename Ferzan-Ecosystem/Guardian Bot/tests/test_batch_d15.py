import re, sqlite3, tempfile, unittest
from pathlib import Path
from unittest import mock

SRC = (Path(__file__).resolve().parent.parent / "guardian_bot.py").read_text()


def _slice(start, end):
    a = SRC.index(start)
    return SRC[a:SRC.index(end, a)]


def cfg_ns():
    ns = {"re": re}
    exec(_slice("_NESTED_QUANT = ", "def _apply_config"), ns)
    return ns


class Regex(unittest.TestCase):
    def test_normal_patterns_pass(self):
        ok = cfg_ns()["_regex_ok"]
        for p in (r"seed\s*ph?rase", r"free\s+mint", r"(airdrop|giveaway)\s+now", r"\bclaim\b"):
            self.assertTrue(ok(p)[0], p)

    def test_catastrophic_shapes_refused(self):
        ok = cfg_ns()["_regex_ok"]
        for p in (r"(a+)+$", r"(.*)*x", r"(\w+\s?)*end", r"(a|b+){2,}", "x" * 200, "", "(unclosed"):
            self.assertFalse(ok(p)[0], p)


class Import(unittest.TestCase):
    def clean(self, cfg):
        return cfg_ns()["_clean_config"](cfg)

    def test_not_an_object(self):
        for bad in ([], "x", 5, None):
            with self.assertRaises(ValueError):
                self.clean(bad)

    def test_log_chat_cannot_be_imported(self):
        out = self.clean({"settings": {"antilink": 1, "log_chat_id": "-100999"}})
        self.assertEqual(out["settings"], {"antilink": 1})

    def test_bad_entries_dropped_good_kept(self):
        out = self.clean({
            "filters": [{"word": "free mint", "action": "ban"}, {"word": "(a+)+$", "is_regex": True}, {"word": 5}, "x",
                        {"word": "ok", "action": "explode"}],
            "local_scam": [{"value": "scam.xyz", "kind": "domain"}, {"value": 3}, {"value": "z", "kind": "weird"}],
            "link_whitelist": ["a.com", 3, ""],
            "faq": [{"question": "q", "answer": "a"}, {"question": "q"}],
            "welcome_variants": ["hi", 7, "  "],
        })
        self.assertEqual([f["word"] for f in out["filters"]], ["free mint", "ok"])
        self.assertEqual(out["filters"][1]["action"], "mute")
        self.assertEqual([x["kind"] for x in out["local_scam"]], ["domain", "ca"])
        self.assertEqual(out["link_whitelist"], ["a.com"])
        self.assertEqual(len(out["faq"]), 1)
        self.assertEqual(out["welcome_variants"], ["hi"])

    def test_wrong_container_types_do_not_crash(self):
        out = self.clean({"filters": "nope", "local_scam": {"a": 1}, "link_whitelist": 3, "faq": "x"})
        self.assertEqual((out["filters"], out["local_scam"], out["link_whitelist"]), ([], [], []))
        self.assertNotIn("faq", out)


class Ssrf(unittest.TestCase):
    def ns(self):
        import socket, ipaddress, urllib.request, urllib.error
        ns = {"urllib": __import__("urllib"), "socket": socket}
        exec("import urllib.request, urllib.error\n" + _slice("def _public_host", "async def _resolve_shortlinks"), ns)
        return ns

    def test_private_and_metadata_addresses_refused(self):
        host = self.ns()["_public_host"]
        for ip in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1"):
            with mock.patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", (ip, 0))]):
                self.assertFalse(host("x.test"), ip)

    def test_public_address_ok_and_mixed_refused(self):
        host = self.ns()["_public_host"]
        with mock.patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]):
            self.assertTrue(host("example.com"))
        with mock.patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0)), (0, 0, 0, "", ("10.0.0.1", 0))]):
            self.assertFalse(host("sneaky.test"))

    def test_resolve_refuses_internal_start(self):
        ns = self.ns()
        with mock.patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("127.0.0.1", 0))]):
            self.assertIsNone(ns["_resolve_shortlink_sync"]("http://bit.ly/x"))
        self.assertIsNone(ns["_resolve_shortlink_sync"]("file:///etc/passwd"))

    def test_redirects_are_checked(self):
        ns = self.ns()
        h = ns["_SafeRedirect"]()
        req = ns["urllib"].request.Request("http://bit.ly/x")
        with mock.patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("169.254.169.254", 0))]):
            with self.assertRaises(ns["urllib"].error.URLError):
                h.redirect_request(req, None, 302, "Found", {}, "http://metadata.test/latest")


class SchemaOnce(unittest.TestCase):
    def test_runs_once_and_again_for_a_new_file(self):
        calls = []
        d = Path(tempfile.mkdtemp())
        ns = {"sqlite3": sqlite3, "DB": d / "g.db", "_SCHEMA_READY": set()}
        ns["_db_schema"] = lambda con: (calls.append(1), con.execute("CREATE TABLE IF NOT EXISTS t(x)"))
        exec(_slice("def _db() ->", "def _db_schema"), ns)
        ns["_db"]().close(); ns["_db"]().close(); ns["_db"]().close()
        self.assertEqual(len(calls), 1)
        ns["DB"].unlink()
        ns["_db"]().close()
        self.assertEqual(len(calls), 2)


class Wiring(unittest.TestCase):
    def test_setlogchat_checks_both_sides(self):
        body = _slice("async def setlogchat", "async def slowmode_cmd")
        self.assertIn("get_chat_member(int(val), context.bot.id)", body)
        self.assertIn("get_chat_member(int(val), update.effective_user.id)", body)

    def test_slash_filter_is_group_only(self):
        self.assertIn('chat.type not in ("group", "supergroup")', _slice("async def _slash_filter_trigger", "async def _pending_text_capture"))

    def test_shadowban_sweep_registered_early_for_all_types(self):
        self.assertIn("MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL, _shadowban_sweep),\n        group=-5", SRC)

    def test_importconfig_limits_and_cleans(self):
        body = _slice("async def importconfig_cmd", "async def cloneconfig_cmd")
        self.assertIn("512 * 1024", body)
        self.assertIn("_clean_config(", body)


if __name__ == "__main__":
    unittest.main()
