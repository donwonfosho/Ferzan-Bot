"""Real pool data from DexScreener. No synthetic book."""

from __future__ import annotations

from dataclasses import dataclass

import requests

DEX = "https://api.dexscreener.com/latest/dex/tokens/{addr}"
SEARCH = "https://api.dexscreener.com/latest/dex/search"


class DexDataError(Exception):
    pass


@dataclass
class Pool:
    chain: str
    dex: str
    pair: str
    base_symbol: str
    quote_symbol: str
    price_usd: float
    liquidity_usd: float
    volume_24h_usd: float
    price_change_24h_pct: float
    fdv: float
    url: str


def fetch_token_pools(address: str) -> list[Pool]:
    addr = (address or "").strip()
    if not addr:
        raise DexDataError("No address.")
    try:
        r = requests.get(DEX.format(addr=addr), timeout=12)
        data = r.json() if r.content else {}
    except requests.RequestException as exc:
        raise DexDataError(f"DexScreener failed: {exc}") from exc
    pairs = data.get("pairs") or []
    if not pairs:
        try:
            r = requests.get(SEARCH, params={"q": addr}, timeout=12)
            data = r.json() if r.content else {}
            pairs = data.get("pairs") or []
        except requests.RequestException:
            pairs = []
    if not pairs:
        raise DexDataError("No pool indexed for that CA.")
    out: list[Pool] = []
    for p in pairs:
        liq = float((p.get("liquidity") or {}).get("usd") or 0)
        out.append(
            Pool(
                chain=str(p.get("chainId") or ""),
                dex=str(p.get("dexId") or ""),
                pair=str(p.get("pairAddress") or ""),
                base_symbol=str((p.get("baseToken") or {}).get("symbol") or "?"),
                quote_symbol=str((p.get("quoteToken") or {}).get("symbol") or "?"),
                price_usd=float(p.get("priceUsd") or 0),
                liquidity_usd=liq,
                volume_24h_usd=float((p.get("volume") or {}).get("h24") or 0),
                price_change_24h_pct=float((p.get("priceChange") or {}).get("h24") or 0),
                fdv=float(p.get("fdv") or p.get("marketCap") or 0),
                url=str(p.get("url") or ""),
            )
        )
    out.sort(key=lambda x: x.liquidity_usd, reverse=True)
    return out


def estimate_price_impact_pct(liquidity_usd: float, trade_usd: float) -> float:
    """Constant-product rough impact. Not a CLMM quote."""
    if liquidity_usd <= 0:
        return 100.0
    # x*y=k, even split pool, trade against half the TVL as a proxy
    reserve = max(liquidity_usd / 2.0, 1e-9)
    # dy = y * dx / (x + dx)
    filled = reserve * trade_usd / (reserve + trade_usd)
    if trade_usd <= 0:
        return 0.0
    return max(0.0, (1.0 - filled / trade_usd) * 100.0)


def liquidity_score(pool: Pool) -> int:
    liq = pool.liquidity_usd
    vol = pool.volume_24h_usd
    score = 0
    if liq >= 1_000_000:
        score += 40
    elif liq >= 250_000:
        score += 30
    elif liq >= 50_000:
        score += 20
    elif liq >= 10_000:
        score += 10
    elif liq >= 2_000:
        score += 5
    if vol >= 1_000_000:
        score += 30
    elif vol >= 100_000:
        score += 20
    elif vol >= 20_000:
        score += 12
    elif vol >= 2_000:
        score += 6
    impact_1k = estimate_price_impact_pct(liq, 1_000)
    if impact_1k < 1:
        score += 30
    elif impact_1k < 3:
        score += 20
    elif impact_1k < 8:
        score += 10
    return max(0, min(100, score))
