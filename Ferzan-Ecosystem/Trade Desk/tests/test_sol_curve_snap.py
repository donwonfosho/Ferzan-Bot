"""Buy card for a brand-new Ferzan Solana (Meteora curve) coin: price comes from the Launch API's curve index."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import price_fetcher as pf

MINT = "41wLscRRf7QpJ9CQNd7EuBHLmEMXF9QyKYcdA1awUfzn"


class _R:
    def __init__(self, d, ok=True):
        self._d, self.ok, self.content = d, ok, b"x"

    def json(self):
        return self._d


def _patch(monkeypatch, replies):
    calls = []

    def fake_get(url, params=None, timeout=None):
        assert (params or {}).get("fresh") == 1  # the Trade Bot asks the API to read brand-new coins from the chain
        calls.append(url)
        r = replies[min(len(calls) - 1, len(replies) - 1)]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(pf.requests, "get", fake_get)
    monkeypatch.setattr(pf.time, "sleep", lambda s: None)
    return calls


GOOD = {"indexed": True, "symbol": "SOL", "name": "SOL Yeah", "pool": "PoolX", "price": 2e-8,
        "native_usd": 150.0, "raised_sol": 2.0, "mcap_usd": 3000.0, "progress": 2.5, "graduated": False}


def test_snapshot_built(monkeypatch):
    _patch(monkeypatch, [_R(GOOD)])
    s = pf._ferzan_curve_snap(MINT)
    assert s.dex == "ferzan-curve" and s.chain == "solana" and s.token_address == MINT
    assert abs(s.price_usd - 3e-6) < 1e-12 and s.liquidity_usd == 300.0 and s.fdv == 3000.0


def test_not_indexed_is_none_after_retry(monkeypatch):
    calls = _patch(monkeypatch, [_R({"indexed": False})])
    assert pf._ferzan_curve_snap(MINT) is None and len(calls) == 2


def test_indexed_on_second_try(monkeypatch):
    _patch(monkeypatch, [_R({"indexed": False}), _R(GOOD)])
    assert pf._ferzan_curve_snap(MINT) is not None


def test_graduated_and_no_price_and_down(monkeypatch):
    _patch(monkeypatch, [_R({**GOOD, "graduated": True})])
    assert pf._ferzan_curve_snap(MINT) is None
    _patch(monkeypatch, [_R({**GOOD, "price": 0})])
    assert pf._ferzan_curve_snap(MINT) is None
    _patch(monkeypatch, [pf.requests.RequestException("down")])
    assert pf._ferzan_curve_snap(MINT) is None


def test_load_market_uses_it(monkeypatch):
    monkeypatch.setattr(pf, "search_dex", lambda q: None)
    _patch(monkeypatch, [_R(GOOD)])
    assert pf.load_market(MINT).dex == "ferzan-curve"
