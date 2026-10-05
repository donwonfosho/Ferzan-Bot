import re, unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "guardian_bot.py").read_text()


def _slice(start, end):
    a = SRC.index(start)
    return SRC[a:SRC.index(end, a)]


def _ns():
    ns = {"re": re, "os": __import__("os")}
    exec(_slice("# Whole words only", "DEFAULT_WORDS"), ns)
    exec(_slice("def _safe_fmt", "async def _lift_restriction"), ns)
    exec(_slice("_PROTECTED_DOMAINS = {", "def _federate_report"), ns)
    return ns


class Pure(unittest.TestCase):
    def test_real_names_are_not_impersonators(self):
        fake = _ns()["FAKE"]
        for ok in ("Devon", "Devika Rao", "Towner", "Sandeep Devgan", "Adminova", "Ownership"):
            self.assertIsNone(fake.search(ok), ok)

    def test_staff_lookalikes_still_caught(self):
        fake = _ns()["FAKE"]
        for bad in ("Ferzan Support", "official_ferzan", "ADMIN", "Dev Team", "ferzan_helpdesk", "Moderator Mike"):
            self.assertIsNotNone(fake.search(bad), bad)

    def test_welcome_text_with_braces_never_raises(self):
        f = _ns()["_safe_fmt"]
        self.assertEqual(f("Hi {first} to {chatname}! {wallet} { }", "Al", "Club"), "Hi Al to Club! {wallet} { }")

    def test_scam_values_are_validated(self):
        ok = _ns()["_scam_value_ok"]
        self.assertTrue(ok("0x" + "ab" * 20, "ca"))
        self.assertTrue(ok("scam-site.xyz", "domain"))
        for bad, kind in (("t", "domain"), (".", "domain"), ("hello world", "domain"), ("abc", "ca"), ("a.b", "domain")):
            self.assertFalse(ok(bad, kind), bad)


class Wiring(unittest.TestCase):
    def test_private_chat_gban_is_owner_only(self):
        self.assertEqual(SRC.count('update.effective_chat.type == "private" and update.effective_user.id not in OWNER_IDS'), 2)

    def test_domains_never_auto_promote(self):
        self.assertIn('kind == "ca":', _slice("def _federate_report", "# ---- OCR"))

    def test_filter_ban_is_not_global(self):
        blk = _slice('if hit_action == "ban":', 'elif hit_action == "mute":')
        self.assertNotIn("global_bans", blk)

    def test_notes_run_after_moderation(self):
        body = SRC[SRC.index("async def on_text"):SRC.index("async def del_pin_notice")]
        self.assertNotIn('text_raw.startswith("#") and len(text_raw) > 1', body)
        self.assertLess(body.index("# Posted anonymously"), body.index("autoreply"))
        self.assertGreater(body.index("await _fire_notes(context, msg, chat_id, user.id, text_raw, text, False)"),
                           body.index("flood") )

    def test_edits_do_not_count_for_slowmode_or_flood(self):
        self.assertIn("if slow_s > 0 and not is_edit:", SRC)
        self.assertIn("    if not is_edit:\n        dq.append(now_ts)", SRC)

    def test_impersonators_muted_by_default(self):
        self.assertIn('GUARDIAN_FAKE_ACTION", "mute"', SRC)

    def test_gate_failures_never_leave_people_muted(self):
        self.assertEqual(SRC.count("await _gate_send_fallback(context"), 2)

    def test_webhook_has_secret(self):
        self.assertIn("secret_token=", SRC)

    def test_format_is_not_str_format(self):
        self.assertNotIn("chosen_text.format(", SRC)


if __name__ == "__main__":
    unittest.main()
