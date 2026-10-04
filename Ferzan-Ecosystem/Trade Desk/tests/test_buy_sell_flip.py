"""'Buy more' on a sell panel must never buy by itself; Go to buy / Go to sell flip one message back and forth.

bot.py needs python-telegram-bot, so these checks read the source (no network, no bot import).

  cd "Trade Desk" && python -m unittest tests.test_buy_sell_flip
"""
import ast
import pathlib
import unittest

SRC = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text()
TREE = ast.parse(SRC)


def _func(name):
    for n in ast.walk(TREE):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(SRC, n)
    raise AssertionError(f"{name} not found")


class Flip(unittest.TestCase):
    def test_sell_panel_has_go_to_buy_not_an_instant_buy(self):
        panel = _func("_bag_panel")
        self.assertNotIn('callback_data=f"buy:', panel)   # the old one-tap $25 buy
        self.assertNotIn("Buy more", panel)
        self.assertIn('"↔️ Go to buy", callback_data=f"sig:{short}"', panel)  # opens the buy card

    def test_buy_card_go_to_sell_flips_in_place(self):
        card = _func("card_keyboard")
        self.assertIn('"💰 Go to sell", callback_data=f"slf:{q}"', card)

    def test_flip_handler_edits_the_same_message_and_stops_the_live_refresh(self):
        i = SRC.index('if data.startswith("slf:")')
        block = SRC[i:i + 1400]
        self.assertIn("query.edit_message_text(", block)
        self.assertIn("_stop_live_card(", block)
        self.assertIn("context.bot.send_message(uid, text", block)   # falls back to a new message if it can't edit

    def test_the_instant_buy_handler_is_not_attached_to_any_button_that_ships_a_default_amount_from_a_sell_panel(self):
        # the only remaining buy: buttons are the signal-card fallback; none belongs to _bag_panel / sell_keyboard
        self.assertNotIn('callback_data=f"buy:', _func("sell_keyboard"))

    def test_callback_data_fits_telegram_limit(self):
        longest = "slf:" + "E" * 48
        self.assertLessEqual(len(longest.encode()), 64)


if __name__ == "__main__":
    unittest.main()
