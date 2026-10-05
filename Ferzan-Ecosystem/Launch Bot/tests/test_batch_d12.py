import os, sys, unittest
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import sol_trades as st


def sigs(*names):
    return [{"signature": n} for n in names]


class Cursor(unittest.TestCase):
    def setUp(self):
        st._FAILS.clear()

    def test_all_fetched_moves_to_the_end(self):
        s = sigs("a", "b", "c")
        self.assertEqual(st.safe_cursor(s, [{}, {}, {}], ["a", "b", "c"]), "c")

    def test_failed_fetch_holds_the_cursor_before_it(self):
        s = sigs("a", "b", "c")
        self.assertEqual(st.safe_cursor(s, [{}, None, {}], ["a", "b", "c"]), "a")

    def test_first_failing_means_no_move(self):
        s = sigs("a", "b")
        self.assertEqual(st.safe_cursor(s, [None, {}], ["a", "b"]), "")

    def test_gives_up_after_the_limit(self):
        s = sigs("a", "b", "c")
        out = None
        for _ in range(st.MAX_FETCH_TRIES):
            out = st.safe_cursor(s, [{}, None, {}], ["a", "b", "c"])
        self.assertEqual(out, "c")  # the stuck one is abandoned, the stream moves on

    def test_error_transactions_are_ignored_in_the_ok_list(self):
        s = sigs("a", "bad", "c")  # "bad" had an on-chain error, so it is not in `ok` and never fetched
        self.assertEqual(st.safe_cursor(s, [{}, {}], ["a", "c"]), "c")

    def test_recovered_signature_resets_its_count(self):
        s = sigs("a", "b")
        st.safe_cursor(s, [{}, None], ["a", "b"])
        st.safe_cursor(s, [{}, {}], ["a", "b"])
        self.assertNotIn("b", st._FAILS)


class Wiring(unittest.TestCase):
    def test_pass_failure_restores_cursors_and_helper_has_a_timeout(self):
        with open(os.path.join(HERE, "sol_trades.py")) as fh:
            s = fh.read()
        self.assertIn("st.clear(); st.update(before)", s)
        self.assertIn("select.select([self.p.stdout], [], [], 20)", s)


if __name__ == "__main__":
    unittest.main()
