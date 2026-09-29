import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import pytest
import price_fetcher as pf

CA = "T" + "A" * 33


def test_plain_with_pool(monkeypatch):
    import tron_signer
    monkeypatch.setattr(tron_signer, "plain_meta", lambda ca: {"symbol": "TRZ", "name": "Tronz", "price_usd": 0.01, "fdv_usd": 1e5})
    s = pf._tron_plain_snap(CA)
    assert s.symbol == "TRZ" and s.chain == "tron" and s.price_usd == 0.01


def test_plain_no_pool_explains(monkeypatch):
    import tron_signer
    monkeypatch.setattr(tron_signer, "plain_meta", lambda ca: {"symbol": "TRZ", "name": "Tronz", "price_usd": 0.0})
    with pytest.raises(pf.PriceFetchError, match="no SunSwap pool"):
        pf._tron_plain_snap(CA)


def test_not_a_token(monkeypatch):
    import tron_signer
    monkeypatch.setattr(tron_signer, "plain_meta", lambda ca: {})
    assert pf._tron_plain_snap(CA) is None
    assert pf._tron_plain_snap("0xabc") is None
