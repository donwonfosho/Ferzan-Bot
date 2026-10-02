import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import trust  # noqa: E402


def test_rank_steps():
    assert trust.rank_for(0)["title"] == "Rookie"
    assert trust.rank_for(1_000)["title"] == "Trader"
    assert trust.rank_for(99_999)["title"] == "Sniper"
    top = trust.rank_for(5_000_000)
    assert top["title"] == "Factory Boss" and top["next_title"] == "" and top["pct"] == 100.0


def test_rank_progress_to_next():
    r = trust.rank_for(5_500)
    assert r["next_title"] == "Sniper" and r["to_next"] == 4_500


def test_countdown_format():
    t = trust.launch_at()
    assert trust.countdown(t - 90_000) == "1d 1h 0m"
    assert trust.countdown(t + 1) == "live now"


def test_stake_gives_no_discount_by_default(monkeypatch):
    import fees

    monkeypatch.delenv("FEE_STAKE_LEDGER", raising=False)
    monkeypatch.setenv("FEE_BPS", "50")
    monkeypatch.setattr(fees.db, "user_volume_usd", lambda *a, **k: 0)
    monkeypatch.setattr(fees.db, "get_user", lambda uid: {"stake_units": 50_000})
    assert fees.current_bps(7) == 50


def test_ton_and_tron_fee_skim_is_off_by_default_and_works_when_on(monkeypatch):
    import feecollect
    import withdraw

    monkeypatch.setenv("FEE_COLLECT_LIVE", "1")
    monkeypatch.setenv("FEE_BPS", "100")
    monkeypatch.setenv("FEE_WALLET_TON", "EQdest")
    monkeypatch.setenv("FEE_WALLET_TRON", "Tdest")
    monkeypatch.setattr(feecollect.db, "add_fee", lambda *a, **k: None)
    monkeypatch.setattr(feecollect.fees, "current_bps", lambda uid=None: 100)
    monkeypatch.setattr(feecollect, "_native_usd", lambda cg: 2.0)
    sent = {}
    monkeypatch.setattr(withdraw, "send_ton", lambda s, d, n: sent.setdefault("ton", (d, n)) and (True, "x"))
    monkeypatch.setattr(withdraw, "send_trx", lambda k, d, n: sent.setdefault("trx", (d, n)) and (True, "x"))
    assert feecollect.skim_buy(901, 100, "ton", sol_secret="s") == (False, "")
    assert feecollect.skim_buy(901, 100, "trx", evm_secret="k") == (False, "")
    monkeypatch.setenv("FEE_COLLECT_TON", "1")
    monkeypatch.setenv("FEE_COLLECT_TRON", "1")
    assert feecollect.skim_buy(901, 100, "ton", sol_secret="s")[0] is True
    assert feecollect.skim_buy(901, 100, "trx", evm_secret="k")[0] is True
    assert sent["ton"] == ("EQdest", 500_000_000)   # $1 fee at $2/TON = 0.5 TON
    assert sent["trx"] == ("Tdest", 500_000)        # 0.5 TRX


def test_ton_tron_sell_fee_needs_sell_switch(monkeypatch):
    import feecollect
    import withdraw

    monkeypatch.setenv("FEE_COLLECT_LIVE", "1")
    monkeypatch.setenv("FEE_COLLECT_TON", "1")
    monkeypatch.setenv("FEE_WALLET_TON", "EQdest")
    monkeypatch.setenv("FEE_COLLECT_SELLS", "0")
    monkeypatch.setattr(feecollect.db, "add_fee", lambda *a, **k: None)
    monkeypatch.setattr(feecollect.fees, "current_bps", lambda uid=None: 100)
    monkeypatch.setattr(feecollect, "_native_usd", lambda cg: 2.0)
    monkeypatch.setattr(withdraw, "send_ton", lambda s, d, n: (True, "x"))
    assert feecollect.skim_buy(902, 100, "ton", sol_secret="s", side="sell") == (False, "")
    monkeypatch.setenv("FEE_COLLECT_SELLS", "1")
    assert feecollect.skim_buy(902, 100, "ton", sol_secret="s", side="sell")[0] is True
