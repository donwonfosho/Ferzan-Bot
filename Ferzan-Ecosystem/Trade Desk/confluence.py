"""
Confluence engine.

Banana Gun-style bots optimize for speed: paste a contract, buy now.
This engine does the opposite. It scores a name on five independent
factors and will refuse a trade when they do not agree.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from price_fetcher import MarketSnapshot


@dataclass
class Factor:
    name: str
    score: int
    weight: float
    note: str


@dataclass
class SignalCard:
    snapshot: MarketSnapshot
    score: int
    bias: str
    factors: list[Factor]
    vetoes: list[str]
    stop_pct: float
    take_pct: float
    thesis: str
    created_at: int = field(default_factory=lambda: int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        s = self.snapshot
        return {
            "query": s.query,
            "symbol": s.symbol,
            "name": s.name,
            "chain": s.chain,
            "dex": s.dex,
            "price_usd": s.price_usd,
            "score": self.score,
            "bias": self.bias,
            "stop_pct": self.stop_pct,
            "take_pct": self.take_pct,
            "thesis": self.thesis,
            "vetoes": self.vetoes,
            "url": s.url,
            "token_address": s.token_address,
            "pair_address": s.pair_address,
            "factors": [
                {"name": f.name, "score": f.score, "note": f.note} for f in self.factors
            ],
        }


def _clamp(n: float, lo: int = 0, hi: int = 100) -> int:
    return max(lo, min(hi, int(round(n))))


def _liq_score(usd: float) -> tuple[int, str]:
    if usd >= 5_000_000:
        return 88, "Deep book. Slippage risk is low."
    if usd >= 1_000_000:
        return 74, "Healthy liquidity for small-to-mid size."
    if usd >= 250_000:
        return 58, "Tradable, but size must stay small."
    if usd >= 50_000:
        return 36, "Thin book. Easy to get trapped."
    if usd <= 0:
        return 55, "No DEX liquidity reading (spot/index feed)."
    return 12, "Illiquid. Exit may not exist when you need it."


def _vol_score(vol: float, liq: float) -> tuple[int, str]:
    if liq <= 0:
        return 50, "No turnover baseline on this feed."
    turnover = vol / liq
    if turnover >= 2.0:
        return 82, f"Hot tape. 24h volume is {turnover:.1f}x liquidity."
    if turnover >= 0.6:
        return 70, f"Active. Turnover {turnover:.1f}x."
    if turnover >= 0.2:
        return 52, f"Normal flow. Turnover {turnover:.1f}x."
    return 28, f"Dead tape. Turnover {turnover:.1f}x."


def _momentum_score(s: MarketSnapshot) -> tuple[int, str]:
    aligned = sum(1 for p in (s.change_1h, s.change_6h, s.change_24h) if p > 0)
    if s.source == "coingecko" and s.change_1h == 0 and s.change_24h == 0:
        return 50, "Index feed only. No short-horizon momentum."
    if aligned == 3 and s.change_1h > 1.2:
        return 78, "All timeframes green, but chase risk is real."
    if aligned >= 2 and 0.4 <= s.change_1h <= 6:
        return 72, "Constructive grind, not a vertical candle."
    if s.change_1h >= 18:
        return 34, "Already extended on the 1h. Late entries get punished."
    if aligned <= 1 and s.change_1h < -3:
        return 30, "Tape is heavy. Longs need a different setup."
    return 48, "Mixed momentum. Wait for agreement."


def _flow_score(s: MarketSnapshot) -> tuple[int, str]:
    if s.buys_h1 + s.sells_h1 < 8:
        return 45, "Too few DEX prints to trust order-flow."
    ratio = s.buy_sell_ratio
    if ratio >= 1.6:
        return 80, f"Buyers dominate 1h flow ({s.buys_h1}/{s.sells_h1})."
    if ratio >= 1.15:
        return 66, f"Slight buy tilt ({s.buys_h1}/{s.sells_h1})."
    if ratio >= 0.85:
        return 50, f"Balanced flow ({s.buys_h1}/{s.sells_h1})."
    return 28, f"Sellers in control ({s.buys_h1}/{s.sells_h1})."


def _quality_score(s: MarketSnapshot) -> tuple[int, list[str], str]:
    vetoes: list[str] = []
    score = 70
    notes: list[str] = []
    if s.source == "coingecko":
        return 72, [], "Listed index asset. Contract-risk is lower, timing-risk remains."
    if s.liquidity_usd and s.liquidity_usd < 40_000:
        vetoes.append("Liquidity below $40k")
        score -= 25
    if s.price_usd <= 0:
        vetoes.append("No reliable mark price")
        score -= 40
    if s.pair_created_ms:
        age_h = max(0.0, (time.time() * 1000 - s.pair_created_ms) / 3_600_000)
        if age_h < 2:
            vetoes.append(f"Pool is {age_h:.1f}h old")
            score -= 20
            notes.append("Fresh pool. Rug and sniper games dominate.")
        elif age_h < 24:
            score -= 8
            notes.append("Under 24h old. Treat as speculative.")
        else:
            notes.append(f"Pool age ~{age_h:.0f}h.")
    if s.fdv and s.liquidity_usd and s.fdv / max(s.liquidity_usd, 1) > 80:
        score -= 12
        notes.append("FDV dwarfs liquidity. Easy to mark up, hard to exit.")
    if s.change_5m >= 25:
        vetoes.append("5m candle already +25%")
        score -= 15
    note = " ".join(notes) if notes else "No hard quality flags."
    return _clamp(score), vetoes, note


def build_signal(s: MarketSnapshot) -> SignalCard:
    liq_s, liq_n = _liq_score(s.liquidity_usd)
    vol_s, vol_n = _vol_score(s.volume_24h, s.liquidity_usd)
    mom_s, mom_n = _momentum_score(s)
    flow_s, flow_n = _flow_score(s)
    qual_s, vetoes, qual_n = _quality_score(s)

    factors = [
        Factor("Liquidity", liq_s, 0.22, liq_n),
        Factor("Activity", vol_s, 0.18, vol_n),
        Factor("Momentum", mom_s, 0.24, mom_n),
        Factor("Order flow", flow_s, 0.16, flow_n),
        Factor("Structure", qual_s, 0.20, qual_n),
    ]
    score = _clamp(sum(f.score * f.weight for f in factors))
    if vetoes:
        score = min(score, 54)

    if score >= 72 and mom_s >= 60 and flow_s >= 50:
        bias = "LONG"
    elif score <= 38 or (mom_s <= 35 and flow_s <= 40):
        bias = "AVOID"
    else:
        bias = "WATCH"

    stop_pct, take_pct = 4.5, 9.0
    if 0 < s.liquidity_usd < 250_000:
        stop_pct, take_pct = 7.0, 12.0
    if s.change_1h >= 10:
        stop_pct, take_pct = 6.5, 8.0

    if bias == "LONG":
        thesis = (
            f"{s.symbol} clears confluence. Liquidity and flow agree, "
            f"momentum is constructive, structure is not a hard veto."
        )
    elif bias == "AVOID":
        thesis = (
            f"{s.symbol} fails confluence. Either the tape is hostile "
            f"or the pool is too fragile to justify risk."
        )
    else:
        thesis = (
            f"{s.symbol} is mixed. One or two factors work, the rest do not. "
            f"No edge in forcing a market order here."
        )
    if vetoes:
        thesis += " Vetoes: " + "; ".join(vetoes) + "."

    return SignalCard(
        snapshot=s,
        score=score,
        bias=bias,
        factors=factors,
        vetoes=vetoes,
        stop_pct=round(stop_pct, 2),
        take_pct=round(take_pct, 2),
        thesis=thesis,
    )


def analyze(query: str) -> SignalCard:
    from price_fetcher import load_market

    return build_signal(load_market(query))
