"""
Market data.

Keeps Claude's CoinGecko helpers for price alerts, and adds DexScreener
lookups so signal scoring can see liquidity, flow, and pool age — things
CoinGecko does not expose well for new DEX pairs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

COINGECKO = "https://api.coingecko.com/api/v3"
DEX_SEARCH = "https://api.dexscreener.com/latest/dex/search"
TIMEOUT = 10


class PriceFetchError(Exception):
    pass


def search_coin(query: str) -> list[dict]:
    try:
        resp = requests.get(
            f"{COINGECKO}/search", params={"query": query}, timeout=TIMEOUT
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise PriceFetchError(f"Search request failed: {exc}") from exc

    coins = resp.json().get("coins", [])
    exact = [c for c in coins if c["symbol"].lower() == query.lower()]
    return exact if exact else coins[:10]


def get_price_usd(coin_id: str) -> float:
    try:
        resp = requests.get(
            f"{COINGECKO}/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise PriceFetchError(f"Price request failed: {exc}") from exc

    data = resp.json()
    if coin_id not in data or "usd" not in data[coin_id]:
        raise PriceFetchError(f"No USD price returned for {coin_id}")
    return float(data[coin_id]["usd"])


def get_prices_usd(coin_ids: list[str]) -> dict[str, float]:
    if not coin_ids:
        return {}
    try:
        resp = requests.get(
            f"{COINGECKO}/simple/price",
            params={"ids": ",".join(sorted(set(coin_ids))), "vs_currencies": "usd"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise PriceFetchError(f"Batch price request failed: {exc}") from exc

    data = resp.json()
    return {cid: v["usd"] for cid, v in data.items() if "usd" in v}


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return default


@dataclass
class MarketSnapshot:
    query: str
    symbol: str
    name: str
    chain: str
    dex: str
    pair_address: str
    token_address: str
    price_usd: float
    liquidity_usd: float
    volume_24h: float
    change_5m: float
    change_1h: float
    change_6h: float
    change_24h: float
    fdv: float
    buys_h1: int
    sells_h1: int
    pair_created_ms: int | None
    url: str
    source: str
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def buy_sell_ratio(self) -> float:
        if self.sells_h1 <= 0:
            return float(self.buys_h1)
        return self.buys_h1 / max(self.sells_h1, 1)


def snapshot_from_pair(pair: dict[str, Any], query: str) -> MarketSnapshot:
    base = pair.get("baseToken") or {}
    txns = pair.get("txns") or {}
    h1 = txns.get("h1") or {}
    ch = pair.get("priceChange") or {}
    liq = pair.get("liquidity") or {}
    vol = pair.get("volume") or {}
    return MarketSnapshot(
        query=query,
        symbol=(base.get("symbol") or query).upper(),
        name=base.get("name") or base.get("symbol") or query,
        chain=pair.get("chainId") or "unknown",
        dex=pair.get("dexId") or "unknown",
        pair_address=pair.get("pairAddress") or "",
        token_address=base.get("address") or "",
        price_usd=_num(pair.get("priceUsd")),
        liquidity_usd=_num(liq.get("usd")),
        volume_24h=_num(vol.get("h24")),
        change_5m=_num(ch.get("m5")),
        change_1h=_num(ch.get("h1")),
        change_6h=_num(ch.get("h6")),
        change_24h=_num(ch.get("h24")),
        fdv=_num(pair.get("fdv") or pair.get("marketCap")),
        buys_h1=_int(h1.get("buys")),
        sells_h1=_int(h1.get("sells")),
        pair_created_ms=_int(pair.get("pairCreatedAt")) or None,
        url=pair.get("url") or "",
        source="dexscreener",
    )


def search_dex(query: str) -> MarketSnapshot | None:
    try:
        resp = requests.get(DEX_SEARCH, params={"q": query}, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise PriceFetchError(f"DexScreener request failed: {exc}") from exc

    pairs = (resp.json() or {}).get("pairs") or []
    if not pairs:
        return None

    q = query.strip().lower()

    def rank(p: dict[str, Any]) -> tuple:
        base = p.get("baseToken") or {}
        sym = (base.get("symbol") or "").lower()
        addr = (base.get("address") or "").lower()
        pair_addr = (p.get("pairAddress") or "").lower()
        exact = 1 if q in {sym, addr, pair_addr} else 0
        liq = _num((p.get("liquidity") or {}).get("usd"))
        vol = _num((p.get("volume") or {}).get("h24"))
        return (exact, liq + vol * 0.25)

    return snapshot_from_pair(max(pairs, key=rank), query)


def load_market(query: str) -> MarketSnapshot:
    snap = search_dex(query)
    if snap and snap.price_usd > 0:
        return snap

    coins = search_coin(query)
    if not coins:
        raise PriceFetchError(f"No market data for '{query}'")
    coin = coins[0]
    price = get_price_usd(coin["id"])
    return MarketSnapshot(
        query=query,
        symbol=coin["symbol"].upper(),
        name=coin["name"],
        chain="coingecko",
        dex="spot",
        pair_address="",
        token_address=coin["id"],
        price_usd=price,
        liquidity_usd=0.0,
        volume_24h=0.0,
        change_5m=0.0,
        change_1h=0.0,
        change_6h=0.0,
        change_24h=0.0,
        fdv=0.0,
        buys_h1=0,
        sells_h1=0,
        pair_created_ms=None,
        url=f"https://www.coingecko.com/en/coins/{coin['id']}",
        source="coingecko",
        extras={"coin_id": coin["id"]},
    )


def quote_price(query: str) -> float:
    try:
        snap = search_dex(query)
        if snap and snap.price_usd > 0:
            return snap.price_usd
    except PriceFetchError:
        pass
    coins = search_coin(query)
    if not coins:
        raise PriceFetchError(f"No price for '{query}'")
    return get_price_usd(coins[0]["id"])
