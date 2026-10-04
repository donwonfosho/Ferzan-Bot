"""Launch Bot: the Solana 'Launch from my Trade Bot wallet' button is wired end to end (source-level)."""
import os
import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
BOT = (HERE / "launch_bot.py").read_text()


class Wiring(unittest.TestCase):
    def test_button_and_handler_only_for_curve_mode(self):
        self.assertIn('(chain == "solana" and mode == "meteora")', BOT)
        self.assertIn('query.data == "confirm:tb" and launch["chain"] == "solana" and launch.get("mode") == "meteora"', BOT)
        self.assertIn("_sol_tb_go(update, context, launch)", BOT)

    def test_request_marked_as_tradebot_source(self):
        body = BOT[BOT.index("async def _sol_tb_go"):BOT.index("def _sol_complete")]
        self.assertIn('source="tradebot_wallet"', body)

    def test_completes_only_after_helper_confirms(self):
        body = BOT[BOT.index("async def _sol_tb_run"):BOT.index("def _tron_complete")]
        self.assertLess(body.index('if res.get("ok"):'), body.index("_sol_complete, req_id"))

    def test_possibly_sent_is_never_called_a_failure(self):
        body = BOT[BOT.index("async def _sol_tb_run"):BOT.index("def _tron_complete")]
        self.assertLess(body.index('res.get("maybe_sent")'), body.index('db.update_status(req_id, "failed", error_message=str(res.get("error"))'))

    def test_api_pins_the_mint_for_tradebot_launches(self):
        api = (HERE / "api.py").read_text()
        self.assertIn('"sol_mint": result.mint_address', api)
        self.assertIn('_ex.get("sol_mint") != mint', api)

    def test_cap(self):
        src = BOT[BOT.index("SOL_TB_MARGIN_LAMPORTS ="):BOT.index("async def _sol_tb_go")]
        ns = {"os": os, "re": __import__("re")}
        exec(src, ns)
        os.environ["LAUNCH_FEE_LAMPORTS"] = "50000000"
        self.assertEqual(ns["_sol_tb_cap"]({"dev_buy": "0.5"}), 500_000_000 + 50_000_000 + 80_000_000)
        self.assertEqual(ns["_sol_tb_cap"]({}), 130_000_000)
        self.assertEqual(ns["_sol_tb_cap"]({"dev_buy": "junk"}), 130_000_000)
        self.assertEqual(ns["_sol_tb_cap"]({"dev_buy": "0.5 SOL"}), 630_000_000)
        self.assertEqual(ns["_sol_tb_cap"]({"dev_buy": "1e1"}), 1_130_000_000)  # read like the API: 1, not 10


if __name__ == "__main__":
    unittest.main()
