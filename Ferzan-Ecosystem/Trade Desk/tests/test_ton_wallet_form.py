"""A TON wallet is always shown to people in the UQ (non-bounceable) form, so a first deposit is not sent back.

  cd "Trade Desk" && python -m unittest tests.test_ton_wallet_form
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ton_addr_fmt import wallet_form  # noqa: E402

# A pair from TON's own documentation: the same wallet, bounceable and non-bounceable.
EQ = "EQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqB2N"
UQ = "UQCD39VS5jcptHL8vMjEXrzGaRcCVYto7HUn4bpAOg8xqEBI"


class WalletForm(unittest.TestCase):
    def test_eq_becomes_uq(self):
        self.assertEqual(wallet_form(EQ), UQ)

    def test_uq_stays(self):
        self.assertEqual(wallet_form(UQ), UQ)

    def test_whitespace_trimmed(self):
        self.assertEqual(wallet_form(f"  {EQ}\n"), UQ)

    def test_bad_input_is_never_changed(self):
        for bad in ("", "hello", EQ[:-1], EQ[:-1] + "A", EQ.replace("CD39", "CD3A"), "T" + "A" * 33, "0x" + "ab" * 20):
            self.assertEqual(wallet_form(bad), bad, bad)

    def test_round_trip_is_a_valid_checksummed_address(self):
        self.assertEqual(wallet_form(wallet_form(EQ)), UQ)

    def test_db_stores_and_returns_uq(self):
        import contextlib
        import sqlite3
        from unittest import mock

        try:
            import db
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"db module not importable here: {exc}")
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row

        @contextlib.contextmanager
        def fake_conn():
            yield con

        with mock.patch.object(db, "get_conn", fake_conn):
            db.set_ton_addr(7, EQ)
            self.assertEqual(con.execute("SELECT address FROM ton_addr WHERE user_id=7").fetchone()[0], UQ)
            # an older row saved in the EQ spelling is shown as UQ too
            con.execute("UPDATE ton_addr SET address=? WHERE user_id=7", (EQ,))
            self.assertEqual(db.get_ton_addr(7), UQ)


if __name__ == "__main__":
    unittest.main()
