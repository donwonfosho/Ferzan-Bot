import os, sys, unittest
_R = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, _R); sys.path.insert(0, os.path.join(_R, "liq"))
from liq import basestonk_mm as mm

TOK = "0x" + "11" * 20
OK_SPENDER = "0x161026041cf3701fc8c2f11d1a9121681be3718f"
EVIL = "0x" + "ab" * 20
ALLOWED = mm._BASE_SWAP_TO


def approve(spender):
    return {"to": TOK, "data": "0x095ea7b3" + spender[2:].rjust(64, "0") + "f" * 64, "value": "0", "chainId": 8453}


class Approvals(unittest.TestCase):
    def test_known_spender_ok(self):
        self.assertIsNone(mm._tx_problem(approve(OK_SPENDER), 8453, "approve", 0, {TOK}, ALLOWED))

    def test_unknown_spender_refused(self):
        why = mm._tx_problem(approve(EVIL), 8453, "approve", 0, {TOK}, ALLOWED)
        self.assertIn("not a known", why)

    def test_unchecked_when_allow_list_off(self):
        self.assertIsNone(mm._tx_problem(approve(EVIL), 8453, "approve", 0, {TOK}, None))

    def test_swap_refusal_names_the_contract(self):
        tx = {"to": EVIL, "data": "0x12345678", "value": "0", "chainId": 8453}
        self.assertIn(EVIL, mm._tx_problem(tx, 8453, "swap", 0, {TOK}, ALLOWED))


if __name__ == "__main__":
    unittest.main()
