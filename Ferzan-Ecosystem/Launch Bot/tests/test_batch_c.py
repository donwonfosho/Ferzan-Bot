import os
import re
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def src(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


class BatchC(unittest.TestCase):
    def test_curve_poll_limits_configurable(self):
        for f in ("tron_indexer.py", "ton_indexer.py", "sol_indexer.py"):
            s = src(f)
            self.assertIn("INDEXER_MAX_CURVES", s)
            self.assertNotRegex(s, r"LIMIT (60|100)\b")

    def test_tron_cursor_overlaps(self):
        self.assertIn("since_ms - 60000", src("tron_indexer.py"))

    def test_evm_overlap_and_arc_scale(self):
        s = src("curve_indexer.py")
        self.assertIn("- REORG_OVERLAP", s)
        self.assertRegex(s, r'self\.chain == "arc":\s+native \*= 10 \*\* 12')

    def test_step_shrinks_only_on_range_errors(self):
        s = src("curve_indexer.py")
        i = s.index("smaller log range")
        self.assertIn('"too many results"', s[i - 600:i])

    def test_new_pool_swaps_refetched(self):
        s = src("curve_indexer.py")
        self.assertIn("new_pools", s)
        self.assertIn("could not fetch swaps for new pools", s)

    def test_ton_keeper_caps_pool_amount_and_stage_lock_noted(self):
        self.assertIn("tonIn > cs.grad", src("scripts/ton-keeper/ton_keeper.mjs"))
        self.assertIn('res.get("stage") == "lock"', src("ton_indexer.py"))

    def test_ton_valid_until_before_start(self):
        s = src("ton_curve.py")
        d = int(re.search(r"START_DELAY_S = (\d+)", s).group(1))
        v = int(re.search(r"valid_until=int\(time\.time\(\)\) \+ (\d+)", s).group(1))
        self.assertLessEqual(v, d - 30)

    def test_pool_trade_key_is_action_id(self):
        self.assertIn('action.get("action_id") or action.get("trace_id")', src("ton_pool_trades.py"))


if __name__ == "__main__":
    unittest.main()
