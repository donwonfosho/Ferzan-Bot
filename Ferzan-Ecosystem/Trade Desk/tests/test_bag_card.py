import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _stub_telegram():
    """python-telegram-bot is not needed to test the card layout: give bot.py light stand-ins when it is absent."""
    try:
        import telegram  # noqa: F401
        return
    except ImportError:
        pass
    from unittest import mock

    class _Btn:
        def __init__(self, text, callback_data=None, **kw):
            self.text, self.callback_data = text, callback_data

    class _Markup:
        def __init__(self, rows):
            self.inline_keyboard = rows

    class _Mod(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            return mock.MagicMock()

    tg = _Mod("telegram")
    tg.InlineKeyboardButton, tg.InlineKeyboardMarkup = _Btn, _Markup
    sys.modules["telegram"] = tg
    for sub in ("telegram.ext", "telegram.constants", "telegram.error", "telegram.request", "telegram.helpers"):
        sys.modules[sub] = _Mod(sub)


def _bot():
    _stub_telegram()
    import bot
    return bot


class FakeKb:
    def __init__(self, rows):
        self.inline_keyboard = rows


def _mk(bot, n):
    B = bot.InlineKeyboardButton
    panels, infos = [], []
    for i in range(n):
        rows = [[B("25%", callback_data=f"slp:25:M{i}")], [B("🔄 Refresh", callback_data=f"bagr:M{i}")]]
        panels.append((f"🎒 <b>Position</b> · TON\nTok{i}", FakeKb(rows)))
        infos.append({"mint": f"M{i}", "sym": f"S{i}", "venue": "TON", "worth": 1.5 * (i + 1), "pct": -0.1 if i == 0 else None})
    return bot._bag_store(1, panels, infos, "🎒 <b>Your bag</b> · %d positions" % n)


class BagCard(unittest.TestCase):
    def test_one_card_with_pager_and_wraparound(self):
        bot = _bot()
        d = _mk(bot, 3)
        text, kb = bot._bag_render(d, 0)
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("1 of 3", text)
        self.assertIn("bagn:2", cbs)  # Prev from the first page wraps to the last
        self.assertIn("bagn:1", cbs)
        self.assertIn("bagl", cbs)
        self.assertIn("slp:25:M0", cbs)  # the position's own sell buttons stay on the card
        t2, k2 = bot._bag_render(d, 2)
        self.assertIn("3 of 3", t2)
        self.assertIn("bagn:0", [b.callback_data for row in k2.inline_keyboard for b in row])

    def test_single_position_still_marked_as_a_card(self):
        bot = _bot()
        d = _mk(bot, 1)
        text, kb = bot._bag_render(d, 0)
        msg = types.SimpleNamespace(reply_markup=kb)
        self.assertTrue(bot._is_bag_card(msg))
        self.assertFalse(any((b.callback_data or "").startswith("bagn:") for row in kb.inline_keyboard for b in row))

    def test_list_view_opens_each_position(self):
        bot = _bot()
        d = _mk(bot, 2)
        text, kb = bot._bag_list(d)
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertEqual([c for c in cbs if c.startswith("bagn:")], ["bagn:0", "bagn:1"])
        self.assertIn("$S0", text)

    def test_empty_bag(self):
        bot = _bot()
        d = _mk(bot, 0)
        text, kb = bot._bag_render(d, 0)
        self.assertIn("No tokens yet", text)

    def test_standalone_panel_is_not_a_card(self):
        bot = _bot()
        B = bot.InlineKeyboardButton
        msg = types.SimpleNamespace(reply_markup=FakeKb([[B("🔄 Refresh", callback_data="bagr:M0")]]))
        self.assertTrue(bot._is_bag_panel(msg))
        self.assertFalse(bot._is_bag_card(msg))


if __name__ == "__main__":
    unittest.main()
