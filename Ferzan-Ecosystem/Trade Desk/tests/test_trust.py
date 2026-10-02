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
