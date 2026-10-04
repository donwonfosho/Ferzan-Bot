"""Launch Bot: the Trade Bot wallet button for EVM chains is wired end to end (source-level, no Telegram needed)."""
import pathlib
import re
import unittest

HERE = pathlib.Path(__file__).resolve().parent.parent
BOT = (HERE / "launch_bot.py").read_text()
API = (HERE / "api.py").read_text()


class Wiring(unittest.TestCase):
    def test_chains_exclude_arc_and_solana(self):
        m = re.search(r"EVM_TB_CHAINS = \{([^}]*)\}", BOT)
        names = set(re.findall(r'"(\w+)"', m.group(1)))
        self.assertEqual(names, {"base", "bsc", "ethereum", "robinhood"})

    def test_button_and_handler(self):
        self.assertIn('chain in EVM_TB_CHAINS and mode in ("plain", "bonding_curve")', BOT)
        self.assertIn('query.data == "confirm:tb" and launch["chain"] in EVM_TB_CHAINS', BOT)
        self.assertIn("_evm_tb_go(update, context, launch)", BOT)

    def test_request_marked_as_tradebot_source(self):
        body = BOT[BOT.index("async def _evm_tb_go"):BOT.index("async def _evm_tb_run")]
        self.assertIn('source="tradebot_wallet"', body)

    def test_never_completes_before_receipt(self):
        body = BOT[BOT.index("async def _evm_tb_run"):BOT.index("def _tron_complete")]
        self.assertLess(body.index('if res.get("ok"):'), body.index("_tron_complete"))

    def test_api_returns_factory_for_the_helper(self):
        self.assertIn('"factory": factory_addr', API)


if __name__ == "__main__":
    unittest.main()
