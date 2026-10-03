import types
import unittest

import sniper


def L(token, source):
    return types.SimpleNamespace(token=token, pool=token, source=source)


class PickLaunches(unittest.TestCase):
    def test_caps_dedupes_and_puts_real_pools_before_paid_boosts(self):
        rows = [L(f"b{i}", "dexscreener-boost") for i in range(10)]
        rows += [L("n1", "geckoterminal"), L("n1", "geckoterminal"), L("n2", "geckoterminal-trend")]
        out = sniper.pick_launches(rows, 6)
        self.assertEqual(len(out), 6)
        self.assertEqual([x.token for x in out[:2]], ["n1", "n2"])
        self.assertEqual(len({x.token for x in out}), 6)

    def test_empty_and_zero(self):
        self.assertEqual(sniper.pick_launches([], 6), [])
        self.assertEqual(sniper.pick_launches([L("a", "x")], 0), [])


if __name__ == "__main__":
    unittest.main()
