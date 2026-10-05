import re, unittest
from pathlib import Path
from unittest import mock

API = (Path(__file__).resolve().parent.parent / "api.py").read_text()


def _slice(start, end):
    a = API.index(start)
    return API[a:API.index(end, a)]


class HTTPException(Exception):
    def __init__(self, code, msg=""): self.code, self.msg = code, msg


WALLET = "W" * 44
MINT = "M" * 43
OTHER = "O" * 43
SIG = "5" * 87


def verifier(chain_tx, built=None, source=""):
    """_verify_site_launch's Solana branch, run for real against a fake RPC answer."""
    class R:
        def json(self): return {"result": chain_tx}
    class Req:
        pass
    ns = {"_re": re, "HTTPException": HTTPException, "time": mock.MagicMock(), "RPC_URLS": {"solana": "x"},
          "requests": mock.MagicMock(), "FACTORY_ADDRESSES": {}, "CURVE_LAUNCHED_TOPIC": "", "_topic_addr": None, "logger": mock.MagicMock()}
    ns["requests"].post.return_value = R()
    exec(_slice("def _verify_site_launch", "class SolBroadcastBody"), ns)
    req = Req()
    req.chain = "solana"; req.wallet_address = WALLET
    req.extra_params = {"sol_mints": built or [], "source": source}
    body = mock.MagicMock(); body.tx_hash = SIG; body.result_token_address = MINT
    return lambda: ns["_verify_site_launch"](req, body)


def tx(payer=WALLET, keys=(MINT,), err=None):
    return {"meta": {"err": err}, "transaction": {"message": {"accountKeys": [{"pubkey": payer}] + [{"pubkey": k} for k in keys]}}}


class PlainSol(unittest.TestCase):
    def test_plain_solana_launches_are_now_proven_on_chain(self):
        ns = {"logger": mock.MagicMock(), "FACTORY_ADDRESSES": {}}
        exec(_slice("def _verifiable_launch", "def ex_grad"), ns)
        class Req: pass
        for mode, want in (("plain", True), ("meteora", True), ("pumpfun", False)):
            r = Req(); r.chain = "solana"; r.mode = mode; r.wallet_address = WALLET; r.extra_params = {}; r.id = "x"
            self.assertIs(ns["_verifiable_launch"](r), want, mode)

    def test_a_mint_this_request_built_passes(self):
        self.assertEqual(verifier(tx(), built=[MINT])(), {"token": MINT})

    def test_someone_elses_token_is_refused(self):
        with self.assertRaises(HTTPException) as e:
            verifier(tx(keys=(MINT,)), built=[OTHER])()
        self.assertIn("not the coin", e.exception.msg)

    def test_a_transaction_from_another_wallet_is_refused(self):
        with self.assertRaises(HTTPException) as e:
            verifier(tx(payer="X" * 44), built=[MINT])()
        self.assertIn("not made by this wallet", e.exception.msg)

    def test_a_failed_or_unrelated_transaction_is_refused(self):
        with self.assertRaises(HTTPException): verifier(tx(err={"x": 1}), built=[MINT])()
        with self.assertRaises(HTTPException): verifier(tx(keys=(OTHER,)), built=[MINT])()   # mint not even in the tx
        with self.assertRaises(HTTPException): verifier({}, built=[MINT])()

    def test_the_plain_build_records_the_coin_it_made(self):
        plain = _slice('if req.mode == "plain":', 'elif req.mode in ("meteora"')
        self.assertIn('"sol_mints"', plain)
        self.assertIn("_set_extra(request_id", plain)


if __name__ == "__main__":
    unittest.main()
