"""Tests for the Launch Bot's TON bonding-curve flow and the holder-tier screens (no network, no real signing).

  cd "Launch Bot" && python -m pytest tests/test_ton_curve_tg.py      (or: python -m unittest tests.test_ton_curve_tg)

python-telegram-bot is stubbed when it isn't installed, so this runs anywhere with the bot's other pure-python deps.
"""
import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "Trade Desk"))


def _stub_telegram():
    try:
        import telegram  # noqa: F401
        import telegram.ext  # noqa: F401
        return
    except ImportError:
        pass

    class _Obj:
        def __init__(self, *a, **k):
            self.args, self.kw = a, k
            self.text = a[0] if a else k.get("text")
            self.callback_data = k.get("callback_data")
            self.url = k.get("url")

    class Markup(_Obj):
        def __init__(self, rows=None, **k):
            super().__init__(**k)
            self.inline_keyboard = rows or []

    tg = types.ModuleType("telegram")
    tg.BotCommand = tg.Update = tg.WebAppInfo = _Obj
    tg.InlineKeyboardButton = _Obj
    tg.InlineKeyboardMarkup = Markup
    ext = types.ModuleType("telegram.ext")
    for n in ("Application", "CommandHandler", "CallbackQueryHandler", "MessageHandler", "ConversationHandler", "filters"):
        setattr(ext, n, mock.MagicMock())
    ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    sys.modules["telegram"], sys.modules["telegram.ext"] = tg, ext


_stub_telegram()
import launch_bot as lb  # noqa: E402
import ferzan_perks as fp  # noqa: E402
import ton_curve as tcv  # noqa: E402
import ton_launch as tl  # noqa: E402
import tron_launch as tron  # noqa: E402

CURVE_ENV = {"TON_CURVE_LIVE": "1", "TON_CURVE_MASTER": "EQ_master_test", "TON_KEEPER_ADDRESS": "EQ_keeper_test"}
TON_VARS = ("TON_CURVE_LIVE", "TON_CURVE_MASTER", "TON_KEEPER_ADDRESS", "TON_CURVE_MIN_GRAD_TON", "LAUNCH_FEE_NANOTON",
            "FERZAN_PERK_MINT", "FERZAN_PERKS_OFF", "LAUNCH_FEE_LAMPORTS")


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class Msg:
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append((text, kw))


def fake_update(uid=7):
    u = mock.MagicMock()
    u.effective_user.id = uid
    u.effective_chat.id = uid
    u.effective_message = Msg()
    return u


def buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


class Base(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for k in TON_VARS:
            os.environ.pop(k, None)
        # no service env file may leak in from the machine running the tests
        self._dv = mock.patch("dotenv.dotenv_values", return_value={})
        self._dv.start()
        self._tmp = tempfile.TemporaryDirectory()
        self._db = mock.patch.object(lb.db, "DB_PATH", os.path.join(self._tmp.name, "t.db"))
        self._db.start()

    def tearDown(self):
        self._db.stop()
        self._dv.stop()
        self._env.stop()
        self._tmp.cleanup()


class CurveLive(Base):
    def test_needs_all_three(self):
        self.assertFalse(lb._curve_live("ton"))
        for missing in CURVE_ENV:
            env = {k: v for k, v in CURVE_ENV.items() if k != missing}
            with mock.patch.dict(os.environ, env):
                self.assertFalse(lb._curve_live("ton"), missing)
        with mock.patch.dict(os.environ, dict(CURVE_ENV, TON_CURVE_LIVE="0")):
            self.assertFalse(lb._curve_live("ton"))
        with mock.patch.dict(os.environ, CURVE_ENV):
            self.assertTrue(lb._curve_live("ton"))

    def test_matches_api_condition(self):
        # api.py: TON_CURVE_LIVE == "1" and TON_CURVE_MASTER and TON_KEEPER_ADDRESS (source check, so they can't drift)
        src = (HERE / "api.py").read_text()
        for name in CURVE_ENV:
            self.assertIn(f'"{name}"', src.split("def _ton_curve_live")[1].split("logging.basicConfig")[0])

    def test_reads_env_file_like_tron_setting(self):
        files = {"/opt/ferzan/.env": {"TON_CURVE_LIVE": "1", "TON_KEEPER_ADDRESS": "EQ_k"},
                 str(HERE / ".env"): {"TON_CURVE_MASTER": "EQ_m"}}
        with mock.patch("dotenv.dotenv_values", side_effect=lambda f: files.get(str(f), {})):
            self.assertTrue(lb._curve_live("ton"))

    def test_chain_lists_ton_only_when_live(self):
        self.assertNotIn("ton", lb._live_chains())
        with mock.patch.dict(os.environ, CURVE_ENV):
            self.assertIn("ton", lb._live_chains())

    def test_min_grad_default_and_env(self):
        self.assertEqual(lb.ton_curve_min_grad(), 2000)
        self.assertEqual(lb.ton_grad_presets(), ["2000", "5000", "10000"])
        with mock.patch.dict(os.environ, {"TON_CURVE_MIN_GRAD_TON": "5"}):
            self.assertEqual(lb.ton_curve_min_grad(), 5)
            self.assertEqual(lb.ton_grad_presets(), ["5", "2000", "5000"])
        with mock.patch.dict(os.environ, {"TON_CURVE_MIN_GRAD_TON": "junk"}):
            self.assertEqual(lb.ton_curve_min_grad(), 2000)


class Flow(Base):
    def launch(self, **kw):
        return dict({"chain": "ton", "mode": "bonding_curve", "extra_params": {}, "name": "Cool", "symbol": "COOL",
                     "decimals": 9}, **kw)

    def test_steps(self):
        self.assertEqual(lb._steps(self.launch()), ["type", "name", "symbol", "logo", "info", "supply", "grad"])
        self.assertIn("devbuy", lb._steps(self.launch(chain="tron")))

    def test_supply_limits_and_hand_off_to_grad(self):
        ctx = mock.MagicMock()
        ctx.user_data = {"launch": self.launch()}
        u = fake_update()
        self.assertEqual(run(lb._set_supply(u, ctx, 10**10)), lb.ENTERING_SUPPLY)
        with mock.patch.object(lb, "_ask_grad", mock.AsyncMock(return_value=lb.ENTERING_GRAD)):
            self.assertEqual(run(lb._set_supply(u, ctx, 10**9)), lb.ENTERING_GRAD)
        self.assertEqual(ctx.user_data["launch"]["total_supply_raw"], str(10**9 * 10**9))

    def test_grad_respects_minimum(self):
        ctx = mock.MagicMock()
        ctx.user_data = {"launch": self.launch()}
        u = fake_update()
        with mock.patch.object(lb, "_show_confirm", mock.AsyncMock(return_value=lb.CONFIRMING)) as conf:
            self.assertEqual(run(lb._set_grad(u, ctx, 100)), lb.ENTERING_GRAD)   # below the 2000 default
            conf.assert_not_called()
            self.assertEqual(run(lb._set_grad(u, ctx, 20_000_000)), lb.ENTERING_GRAD)  # above the API cap
            self.assertEqual(run(lb._set_grad(u, ctx, 2000)), lb.CONFIRMING)
            ex = ctx.user_data["launch"]["extra_params"]
            self.assertEqual(ex["graduation_eth_threshold"], str(2000 * 10**9))
            self.assertEqual((ex["dev_buy"], ex["max_buy"], ex["start_minutes"]), ("0", "0", "0"))
        with mock.patch.dict(os.environ, {"TON_CURVE_MIN_GRAD_TON": "5"}), \
                mock.patch.object(lb, "_show_confirm", mock.AsyncMock(return_value=lb.CONFIRMING)):
            self.assertEqual(run(lb._set_grad(u, ctx, 5)), lb.CONFIRMING)
            self.assertEqual(ctx.user_data["launch"]["extra_params"]["graduation_eth_threshold"], str(5 * 10**9))

    def test_need_nano_matches_the_transaction(self):
        self.assertEqual(lb._ton_need_nano("plain"), 300_000_000 + 300_000_000)
        want = tcv.CURVE_TON + tl.DEPLOY_TON + tl.ADMIN_TON + tl.launch_fee_nano()
        self.assertEqual(lb._ton_need_nano("bonding_curve"), want)
        with mock.patch.dict(os.environ, {"LAUNCH_FEE_NANOTON": "0"}):
            self.assertEqual(lb._ton_need_nano("bonding_curve"), 350_000_000)

    def _confirm(self, info):
        ctx = mock.MagicMock()
        lau = self.launch(total_supply_raw=str(10**9 * 10**9), supply_display="1,000,000,000 (1B)",
                          extra_params={"graduation_eth_threshold": str(2000 * 10**9), "graduation_display": "2000 TON",
                                        "dev_buy": "0", "max_buy": "0", "start_minutes": "0"})
        ctx.user_data = {"launch": lau}
        u = fake_update()
        calls = []

        async def fake_run(cmd, args, timeout=150, script="tron_launch_exec.py"):
            calls.append((cmd, script))
            return info

        with mock.patch.object(tron, "run", fake_run):
            self.assertEqual(run(lb._show_confirm(u, ctx)), lb.CONFIRMING)
        return u.effective_message.sent[0], calls

    def test_confirm_screen_cost_wallet_and_buttons(self):
        (text, kw), calls = self._confirm({"ok": True, "address": "UQ_tb_wallet", "balance_ton": 0.2, "need_ton": 0.75,
                                           "enough": False})
        self.assertEqual(calls, [("info", "ton_launch_exec.py")])      # read-only balance check, no "launch"
        for frag in ("Bonding curve", "2000 TON", "0.3 TON", "0.65 TON", "Trade Bot TON wallet", "UQ_tb_wallet",
                     "Send at least", "Nothing is sent until you tap", "none (TON curves have no dev buy yet)",
                     "2 minutes"):
            self.assertIn(frag, text)
        self.assertNotIn("Max buy", text)
        data = [b.callback_data for b in buttons(kw["reply_markup"])]
        self.assertIn("confirm:yes", data)     # connect a wallet
        self.assertIn("confirm:tb", data)      # Trade Bot wallet
        self.assertIn("confirm:later", data)
        labels = " ".join(str(b.text) for b in buttons(kw["reply_markup"]))
        self.assertIn("Connect a wallet", labels)
        self.assertIn("Trade Bot wallet", labels)

    def test_confirm_screen_without_trade_bot_wallet(self):
        (text, _), _ = self._confirm({"ok": False, "error": "no_wallet"})
        self.assertIn("No Trade Bot wallet yet", text)

    def test_trade_bot_go_creates_curve_request_and_runs_in_background(self):
        ctx = mock.MagicMock()
        lau = self.launch(total_supply_raw="1000000000000000000", extra_params={"graduation_eth_threshold": "2000000000000"})
        ctx.user_data = {"launch": lau}
        u = fake_update()
        u.callback_query.edit_message_text = mock.AsyncMock()
        created = {}

        def fake_create(**kw):
            created.update(kw)
            return types.SimpleNamespace(id="req1")

        sent_coro = []
        ctx.application.create_task = lambda c: (sent_coro.append(c), c.close())
        with mock.patch.object(lb.db, "create_launch_request", fake_create):
            self.assertEqual(run(lb._ton_tb_go(u, ctx, lau)), lb.ConversationHandler.END)
        self.assertEqual((created["chain"], created["mode"]), ("ton", "bonding_curve"))
        self.assertEqual(created["extra_params"]["source"], "tradebot_wallet")
        self.assertEqual(len(sent_coro), 1)

    def test_trade_bot_run_uses_curve_need_and_stops_on_low_balance(self):
        bot = mock.MagicMock()
        bot.send_message = mock.AsyncMock()
        seen = []

        async def fake_run(cmd, args, timeout=150, script=""):
            seen.append((cmd, args))
            return {"ok": True, "address": "UQ_x", "balance_ton": 0.1, "need_ton": 0.75, "enough": False}

        with mock.patch.object(tron, "run", fake_run), mock.patch.object(lb.db, "update_status") as st:
            run(lb._ton_tb_run(bot, "req1", 7, 7, "bonding_curve"))
        self.assertEqual(seen, [("info", {"uid": 7, "need_nano": lb._ton_need_nano("bonding_curve")})])  # never got to "launch"
        st.assert_called_once()
        self.assertIn("Nothing was sent", bot.send_message.call_args.kwargs["text"])


class Tiers(Base):
    MINT = "So11111111111111111111111111111111111111112"

    def test_text_lists_ladder_from_ferzan_perks(self):
        t = lb._tiers_text()
        for frag in ("holder", "booster", "whale", "titan", "1,000,000+", "5,000,000+", "10,000,000+", "25,000,000+",
                     "half price", "free", "0.15%", "0.10%", "0.05%", "0%", "else 0.25%", "10% off (soon)",
                     "15% off (soon)", "25% off (soon)", "40% off (soon)", "Fri Oct 9 2026, 7PM ET"):
            self.assertIn(frag, t)
        self.assertLess(t.index("titan"), t.index("Fri Oct 9") + 10_000)

    def test_mint_stays_hidden(self):
        with mock.patch.dict(os.environ, {"FERZAN_PERK_MINT": self.MINT}):
            self.assertNotIn(self.MINT, lb._tiers_text(fp.perks("x" * 44), "x" * 44))

    def test_own_tier_shown(self):
        w = "A" * 44
        own = {"active": True, "tier": "whale", "badge": "🐋 FERZAN whale", "balance": 12_000_000.0, "next_tier": "titan",
               "next_min": 25_000_000.0, "holder_min": 1_000_000.0}
        t = lb._tiers_text(own, w)
        self.assertIn("FERZAN whale", t)
        self.assertIn("12,000,000", t)
        self.assertIn("titan at 25,000,000", t)

    def test_before_announcement_no_tier_claimed(self):
        w = "A" * 44
        t = lb._tiers_text(fp.perks(w), w)     # no mint set: perks inactive, no network
        self.assertIn("aren't live yet", t)

    def test_tiers_cmd_saves_wallet_and_validates(self):
        w = "9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin"
        ctx = mock.MagicMock()
        u = fake_update(uid=42)
        ctx.args = ["not-a-wallet!"]
        run(lb.tiers_cmd(u, ctx))
        self.assertIn("Solana address", u.effective_message.sent[-1][0])
        self.assertEqual(lb._get_perk_wallet(42), "")
        ctx.args = [w]
        with mock.patch.object(fp, "perks", return_value={"active": True, "tier": "none", "balance": 5.0, "holder_min": 1e6}):
            run(lb.tiers_cmd(u, ctx))
        self.assertEqual(lb._get_perk_wallet(42), w)
        self.assertIn("no tier yet", u.effective_message.sent[-1][0])
        ctx.args = ["clear"]
        run(lb.tiers_cmd(u, ctx))
        self.assertEqual(lb._get_perk_wallet(42), "")

    def test_menu_button_and_command_registered(self):
        src = (HERE / "launch_bot.py").read_text()
        self.assertIn('callback_data="go:tiers"', src)
        self.assertIn('CommandHandler("tiers", tiers_cmd)', src)
        self.assertIn('BotCommand("tiers"', src)

    def test_solana_fee_row_discount(self):
        lb._set_perk_wallet(5, "B" * 44)
        with mock.patch.object(fp, "perks", return_value={"active": True, "launch_fee_off_pct": 50, "badge": "🔷 FERZAN holder"}):
            fee, note = run(lb._solana_fee_row(5))
        self.assertIn("0.025 SOL (was 0.05)", fee)
        self.assertIn("launch fee 50% off", note)
        fee, note = run(lb._solana_fee_row(99))          # no saved wallet
        self.assertEqual(fee, "0.05 SOL + network cost")
        self.assertIn("/tiers", note)

    def test_solana_fee_row_free_for_whale(self):
        lb._set_perk_wallet(5, "B" * 44)
        with mock.patch.object(fp, "perks", return_value={"active": True, "launch_fee_off_pct": 100, "badge": "🐋 FERZAN whale"}):
            fee, note = run(lb._solana_fee_row(5))
        self.assertIn("0 SOL", fee)
        self.assertIn("no launch fee", note)


class TradeDeskHelper(unittest.TestCase):
    def test_four_messages_allowed_five_refused(self):
        import ton_launch_exec as ex
        self.assertEqual(ex.MAX_MESSAGES, 4)
        for bad in ([], [{}] * 5, "x"):
            with self.assertRaises(SystemExit):   # out() prints one JSON error line and exits
                ex._parse(bad)

    def test_curve_total_under_safety_limit(self):
        import ton_launch_exec as ex
        total = tcv.CURVE_TON + tl.DEPLOY_TON + tl.ADMIN_TON + 300_000_000
        self.assertLess(total, ex.MAX_TOTAL_NANO)


if __name__ == "__main__":
    unittest.main()
