"""First-run sequence: /start -> ONE welcome message with two buttons -> tour steps -> the full desk (source-level)."""
import os
import pathlib
import re
import unittest

BOT = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text()


def body(start_marker: str, end_marker: str) -> str:
    return BOT[BOT.index(start_marker):BOT.index(end_marker)]


class FirstRun(unittest.TestCase):
    def test_welcome_keyboard_has_only_two_choices(self):
        kb = body("def _welcome_keyboard", "async def start(")
        self.assertEqual(sorted(re.findall(r'callback_data="([^"]+)"', kb)), ["tour:skip", "tour:start"])

    def test_start_does_not_dump_the_tour_or_the_grid_on_a_new_user(self):
        st = body("async def start(", "# ---- first-run tour")
        self.assertNotIn("_tour_step1", st)  # the tour only starts when the user taps the button
        self.assertEqual(st.count("_welcome_keyboard() if first_time else home_keyboard"), 3)  # animation, photo, text paths

    def test_tour_buttons_are_routed(self):
        self.assertIn('data == "tour:start"', BOT)
        skip = body('elif data == "tour:skip":', 'elif data == "tour:bal":')
        self.assertIn('db.set_flag(uid, "onboarded", True)', skip)  # skipping counts as onboarded, so /start shows the desk
        self.assertIn("await start(update, context)", skip)

    def test_tour_ends_at_the_desk(self):
        self.assertIn('InlineKeyboardButton("🏠 Go to the desk", callback_data="go:home")', BOT)

    def test_new_pools_button_is_a_short_list(self):
        self.assertIn('context.user_data["launch_cards"] = 3', BOT)
        self.assertIn('context.user_data.pop("launch_cards", 6)', BOT)


if __name__ == "__main__":
    unittest.main()


class NoFloodForNewUsers(unittest.TestCase):
    """A new account used to get 100+ launch cards in a few hours (alerts defaulted ON)."""

    def test_new_users_start_with_dm_launch_alerts_off(self):
        import tempfile

        import db

        db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "t.db"
        db.init_db()
        user = db.ensure_user(501, "newbie")
        self.assertEqual(int(user["alerts_on"]), 0)
        self.assertEqual(db.list_alert_users(), [])  # the scanner skips them too until they opt in
        db.update_user(501, alerts_on=1)  # turning it on in Settings still works
        self.assertEqual(db.list_alert_users(), [501])

    def test_opted_in_users_get_a_short_batch(self):
        self.assertIn("for ln in diverse[:_dm_max()]:", BOT)
        ns = {"os": os}
        exec(BOT[BOT.index("def _dm_max"):BOT.index("async def _launch_feed_job")], ns)
        os.environ.pop("FERZAN_DM_MAX", None)
        self.assertEqual(ns["_dm_max"](), 3)
        os.environ["FERZAN_DM_MAX"] = "50"
        self.assertEqual(ns["_dm_max"](), 8)
        os.environ["FERZAN_DM_MAX"] = "junk"
        self.assertEqual(ns["_dm_max"](), 3)
        os.environ.pop("FERZAN_DM_MAX", None)

    def test_home_menu_has_no_signal_buttons_in_chat(self):
        kb = body("def home_keyboard", "# ---- home screen helpers")
        self.assertNotIn("go:feeds", kb)
        self.assertNotIn("go:launches", kb)
        self.assertIn('InlineKeyboardButton("📡 Signals", url=signals)', kb)  # opens the signal channels instead

    def test_launch_cards_are_not_pushed_into_private_chats_by_default(self):
        ns = {"os": os}
        exec(BOT[BOT.index("def _launch_dms_on"):BOT.index("def _dm_max")], ns)
        os.environ.pop("FERZAN_LAUNCH_DMS", None)
        self.assertFalse(ns["_launch_dms_on"]())
        os.environ["FERZAN_LAUNCH_DMS"] = "1"
        self.assertTrue(ns["_launch_dms_on"]())
        os.environ.pop("FERZAN_LAUNCH_DMS", None)
        job = body("async def _launch_feed_job", "async def _live_buy_followup") if "async def _live_buy_followup" in BOT else BOT[BOT.index("async def _launch_feed_job"):]
        self.assertIn("            if dms:\n                text, markup = await asyncio.to_thread(launch_card, ln)", job)
        self.assertIn('if not dms and not db.flag_on(uid, "auto_buy", 0):', job)  # auto-buy users still get scanned


class HomeScreenPolish(unittest.TestCase):
    def test_signals_button_shares_a_two_button_row(self):
        kb = body("def home_keyboard", "# ---- home screen helpers")
        row = kb[kb.index("# Opens the Launch Bot"):]
        row = row[:row.index("],")]
        self.assertIn("🚀 Launch", row)
        self.assertIn("📡 Signals", row)

    def test_hot_buttons_never_repeat_a_label(self):
        self.assertIn("all(label != lbl for lbl, _cb in out)", BOT)
