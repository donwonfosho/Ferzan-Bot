import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


class TonFill(unittest.TestCase):
    def setUp(self):
        import ton_signer
        self.t = ton_signer
        self.sleep = mock.patch.object(ton_signer.time, "sleep", lambda s: None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()

    def test_unreadable_balance_is_not_faked(self):
        with mock.patch.object(self.t, "jetton_holding", side_effect=RuntimeError("node down")):
            self.assertIsNone(self.t._held_amount("k", "EQx"))
        self.assertIsNone(self.t._bought_tokens("k", "EQx", None))

    def test_tokens_arriving_are_measured(self):
        seq = iter([10.0, 10.0, 25.5])
        with mock.patch.object(self.t, "jetton_holding", side_effect=lambda *_: (next(seq), "a")):
            got = self.t._bought_tokens("k", "EQx", 10.0, wait_s=999)
        self.assertAlmostEqual(got, 15.5)

    def test_bounced_swap_reports_zero(self):
        clock = iter([0, 1, 2, 200, 300])
        with mock.patch.object(self.t, "jetton_holding", return_value=(10.0, "a")), \
                mock.patch.object(self.t.time, "monotonic", side_effect=lambda: next(clock)):
            self.assertEqual(self.t._bought_tokens("k", "EQx", 10.0, wait_s=90), 0.0)

    def test_message_pauses_automation(self):
        import sendstate
        self.assertTrue(sendstate.is_unclear(self.t._no_tokens_msg(1.0, "EQabc")))


if __name__ == "__main__":
    unittest.main()
