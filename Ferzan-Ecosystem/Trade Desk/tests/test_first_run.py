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
