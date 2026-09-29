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
DEX_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/{addr}"
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


# CoinGecko refuses many datacenter IPs (HTTP 403). These are the wrapped native coins, priced from
# DexScreener (already used below) so the desk never has to guess a dollar amount.
_WRAPPED = {
    "binancecoin": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    "ethereum": "0x4200000000000000000000000000000000000006",
    "solana": "So11111111111111111111111111111111111111112",
    "tron": "TNUC9Qb1rRpS5CbWLmNMxXBjyFoydXjWFR",
    "the-open-network": "0x582d872A1B094FC48F5DE31D3B73F2D9bE47def1",
    "avalanche-2": "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
}
_LAST: dict[str, tuple[float, float]] = {}  # coin id -> (time, last good price)
_LAST_MAX_AGE = 1800.0


def _dex_native_price(coin_id: str) -> float:
    """USD price of a native coin from its most liquid wrapped-token pool. 0.0 if unavailable."""
    addr = _WRAPPED.get(coin_id)
    if not addr:
        return 0.0
    try:
        resp = requests.get(DEX_TOKEN.format(addr=addr), timeout=TIMEOUT)
        resp.raise_for_status()
        pairs = resp.json().get("pairs") or []
    except (requests.RequestException, ValueError):
        return 0.0
    best_liq, best_px = 0.0, 0.0
    for p in pairs:
        try:
            if str((p.get("baseToken") or {}).get("address", "")).lower() != addr.lower():
                continue
            liq = float((p.get("liquidity") or {}).get("usd") or 0)
            px = float(p.get("priceUsd") or 0)
        except (TypeError, ValueError):
            continue
        if px > 0 and liq > best_liq:
            best_liq, best_px = liq, px
    return best_px if best_liq >= 50_000 else 0.0


def _remember(coin_id: str, px: float) -> float:
    if px > 0:
        _LAST[coin_id] = (time.time(), px)
    return px


def _last_good(coin_id: str) -> float:
    t, px = _LAST.get(coin_id, (0.0, 0.0))
    return px if px > 0 and time.time() - t <= _LAST_MAX_AGE else 0.0


def get_price_usd(coin_id: str) -> float:
    try:
        resp = requests.get(
            f"{COINGECKO}/simple/price",
            params={"ids": coin_id, "vs_currencies": "usd"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if coin_id in data and "usd" in data[coin_id]:
            return _remember(coin_id, float(data[coin_id]["usd"]))
        reason = f"No USD price returned for {coin_id}"
    except (requests.RequestException, ValueError) as exc:
        reason = f"Price request failed: {exc}"
    px = _dex_native_price(coin_id) or _last_good(coin_id)
    if px > 0:
        return _remember(coin_id, px) if coin_id not in _LAST or px != _last_good(coin_id) else px
    raise PriceFetchError(reason)


def get_prices_usd(coin_ids: list[str]) -> dict[str, float]:
    if not coin_ids:
        return {}
    out: dict[str, float] = {}
    try:
        resp = requests.get(
            f"{COINGECKO}/simple/price",
            params={"ids": ",".join(sorted(set(coin_ids))), "vs_currencies": "usd"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        out = {cid: _remember(cid, float(v["usd"])) for cid, v in resp.json().items() if "usd" in v}
    except (requests.RequestException, ValueError):
        pass
    for cid in sorted(set(coin_ids)):
        if out.get(cid, 0) > 0:
            continue
        px = _dex_native_price(cid) or _last_good(cid)
        if px > 0:
            out[cid] = px
    if not out:
        raise PriceFetchError("Batch price request failed: no price source answered")
    return out


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
        extras={
            "info": pair.get("info") or {},
            "labels": pair.get("labels") or [],
        },
    )


def _looks_ca(query: str) -> bool:
    q = (query or "").strip()
    if q.startswith("0x") and len(q) == 42:
        return True
    if 32 <= len(q) <= 44 and not q.startswith("0x") and " " not in q:
        return True
    return False


def _pairs_for_token(addr: str) -> list[dict[str, Any]]:
    try:
        resp = requests.get(DEX_TOKEN.format(addr=addr), timeout=TIMEOUT)
        resp.raise_for_status()
        return (resp.json() or {}).get("pairs") or []
    except requests.RequestException:
        return []


def search_dex(query: str) -> MarketSnapshot | None:
    q = query.strip()
    ql = q.lower()
    pairs: list[dict[str, Any]] = []
    if _looks_ca(q):
        pairs = _pairs_for_token(q)
        pairs = [
            p
            for p in pairs
            if (p.get("baseToken") or {}).get("address", "").lower() == ql
            or (p.get("quoteToken") or {}).get("address", "").lower() == ql
        ]
        if not pairs:
            return None
    else:
        try:
            resp = requests.get(DEX_SEARCH, params={"q": query}, timeout=TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise PriceFetchError(f"DexScreener request failed: {exc}") from exc
        pairs = (resp.json() or {}).get("pairs") or []
    if not pairs:
        return None

    def rank(p: dict[str, Any]) -> tuple:
        base = p.get("baseToken") or {}
        quote = p.get("quoteToken") or {}
        addr = (base.get("address") or "").lower()
        qaddr = (quote.get("address") or "").lower()
        pair_addr = (p.get("pairAddress") or "").lower()
        exact = 1 if ql in {addr, qaddr, pair_addr} else 0
        liq = _num((p.get("liquidity") or {}).get("usd"))
        vol = _num((p.get("volume") or {}).get("h24"))
        return (exact, liq + vol * 0.25)

    snap = snapshot_from_pair(max(pairs, key=rank), query)
    if _looks_ca(q) and snap.token_address.lower() != ql:
        # Keep the pasted CA even if DexScreener listed it as quote.
        snap.token_address = q
    return snap


def _gecko_snap(query: str) -> MarketSnapshot | None:
    q = query.strip()
    nets = (
        (("solana", "solana"),)
        if not q.startswith("0x")
        else (
            ("bsc", "bsc"),
            ("base", "base"),
            ("eth", "ethereum"),
            ("arbitrum", "arbitrum"),
            ("avalanche", "avax"),
        )
    )
    for net, chain in nets:
        try:
            r = requests.get(
                f"https://api.geckoterminal.com/api/v2/networks/{net}/tokens/{q}",
                headers={"Accept": "application/json"},
                timeout=TIMEOUT,
            )
        except requests.RequestException:
            continue
        if r.status_code >= 400:
            continue
        attr = ((r.json() or {}).get("data") or {}).get("attributes") or {}
        px = _num(attr.get("price_usd"))
        if px <= 0:
            continue
        return MarketSnapshot(
            query=q,
            symbol=(attr.get("symbol") or "TOKEN").upper(),
            name=attr.get("name") or attr.get("symbol") or q[:8],
            chain=chain,
            dex="geckoterminal",
            pair_address="",
            token_address=q,
            price_usd=px,
            liquidity_usd=_num(attr.get("total_reserve_in_usd")),
            volume_24h=_num(attr.get("volume_usd", {}).get("h24") if isinstance(attr.get("volume_usd"), dict) else attr.get("volume_usd")),
            change_5m=0,
            change_1h=0,
            change_6h=0,
            change_24h=_num(attr.get("price_change_percentage", {}).get("h24") if isinstance(attr.get("price_change_percentage"), dict) else 0),
            fdv=_num(attr.get("fdv_usd") or attr.get("market_cap_usd")),
            buys_h1=0,
            sells_h1=0,
            pair_created_ms=None,
            url=f"https://www.geckoterminal.com/{net}/tokens/{q}",
            source="geckoterminal",
        )
    return None


def _jup_snap(query: str) -> MarketSnapshot | None:
    mint = query.strip()
    if mint.startswith("0x") or len(mint) < 32:
        return None
    sol = "So11111111111111111111111111111111111111112"
    try:
        r = requests.get(
            "https://quote-api.jup.ag/v6/quote",
            params={
                "inputMint": sol,
                "outputMint": mint,
                "amount": "100000000",
                "slippageBps": "300",
            },
            timeout=TIMEOUT,
        )
        q = r.json() if r.content else {}
    except requests.RequestException:
        return None
    out = _num(q.get("outAmount"))
    if out <= 0:
        return None
    # 0.1 SOL -> out tokens (raw). Price USD needs SOL usd — skip precise, mark tradable.
    return MarketSnapshot(
        query=mint,
        symbol="TOKEN",
        name="Jupiter-routable",
        chain="solana",
        dex="jupiter",
        pair_address="",
        token_address=mint,
        price_usd=0.0,
        liquidity_usd=0.0,
        volume_24h=0.0,
        change_5m=0,
        change_1h=0,
        change_6h=0,
        change_24h=0,
        fdv=0,
        buys_h1=0,
        sells_h1=0,
        pair_created_ms=None,
        url="",
        source="jupiter",
        extras={"tradable": True, "out_per_0p1_sol": out},
    )


def _ferzan_curve_snap(query: str) -> MarketSnapshot | None:
    """Ferzan launchpad token still on its bonding curve (no DEX pool until graduation)."""
    ca = (query or "").strip()
    if ca.startswith("T") and len(ca) == 34:
        return _ferzan_tron_curve_snap(ca)
    if ca.startswith(("EQ", "UQ", "kQ")) and len(ca) == 48:
        return _ferzan_ton_curve_snap(ca)
    if not (ca.lower().startswith("0x") and len(ca) == 42):
        return None
    try:
        import evm_signer

        ci = evm_signer.curve_info(ca)
        if not ci or ci.get("graduated"):
            return None
        px, chain, sym, name = evm_signer.curve_meta(ca)
        if px <= 0:
            return None
        rpc = evm_signer.CHAINS[chain]["rpc"]
        er = evm_signer._call_words(rpc, ci["curve"], "0xd62ccb3f") or [0]  # ethReserve()
        ts = evm_signer._call_words(rpc, ca, "0x18160ddd") or [0]  # totalSupply()
        native_usd = px / ci["price_native"] if ci.get("price_native") else 0.0
        liq = er[0] / 1e18 * native_usd
        fdv = ts[0] / 1e18 * px
    except Exception:
        return None
    return MarketSnapshot(
        query=ca, symbol=sym or "?", name=name or sym or "Ferzan launch", chain=chain,
        dex="ferzan-curve", pair_address=ci["curve"], token_address=ca, price_usd=px,
        liquidity_usd=liq, volume_24h=0.0, change_5m=0.0, change_1h=0.0, change_6h=0.0,
        change_24h=0.0, fdv=fdv, buys_h1=0, sells_h1=0, pair_created_ms=None,
        url=f"https://launch.ferzaneco.com/miniapp/curve.html?chain={ {'eth': 'ethereum', 'hood': 'robinhood'}.get(chain, chain)}&curve={ci['curve']}",
        source="ferzan", extras={"ferzan_curve": ci["curve"]},
    )


def _ton_curve_row(ca: str) -> dict:
    """The curve-index row for a Ferzan TON curve coin still on its curve ({} otherwise). The pasted address
    may be the coin (jetton) or its curve; both find the same row."""
    import sqlite3

    import ton_signer

    want = ton_signer._raw(ca)
    c = sqlite3.connect(f"file:{ton_signer._index_db()}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute("SELECT curve, token, name, symbol, price, mcap, real_eth, graduated FROM curves "
                         "WHERE chain = 'ton'").fetchall()
    finally:
        c.close()
    for r in rows:
        try:
            if want in (ton_signer._raw(r["token"]), ton_signer._raw(r["curve"])):
                return {} if r["graduated"] else dict(r)
        except Exception:
            continue
    return {}


def _ferzan_ton_curve_snap(ca: str) -> MarketSnapshot | None:
    try:
        row = _ton_curve_row(ca)
        if not row:
            return None
        ton = get_price_usd("the-open-network")  # falls back to DexScreener; raises rather than guess
        price_ton = float(row.get("price") or 0)
        px = price_ton * ton
        if px <= 0:
            return None
        liq = float(int(row.get("real_eth") or 0)) / 1e18 * ton  # index stores 9-decimal TON x 1e9
        fdv = float(row.get("mcap") or 0) * ton
    except Exception:
        return None
    return MarketSnapshot(
        query=ca, symbol=row.get("symbol") or "?", name=row.get("name") or row.get("symbol") or "Ferzan launch",
        chain="ton", dex="ferzan-curve", pair_address=row["curve"], token_address=row["token"], price_usd=px,
        liquidity_usd=liq, volume_24h=0.0, change_5m=0.0, change_1h=0.0, change_6h=0.0, change_24h=0.0, fdv=fdv,
        buys_h1=0, sells_h1=0, pair_created_ms=None, url=f"https://ferzan-factory.com/coin/ton/{row['curve']}",
        source="ferzan", extras={"ferzan_curve": row["curve"]},
    )


def _ferzan_tron_curve_snap(ca: str) -> MarketSnapshot | None:
    try:
        import tron_signer

        m = tron_signer.curve_meta(ca)
    except Exception:
        return None
    if not m or m.get("price_usd", 0) <= 0:
        return None
    return MarketSnapshot(
        query=ca, symbol=m.get("symbol") or "?", name=m.get("name") or m.get("symbol") or "Ferzan launch",
        chain="tron", dex="ferzan-curve", pair_address=tron_signer.to_b58(m["curve"]), token_address=ca,
        price_usd=m["price_usd"], liquidity_usd=m.get("liq_usd", 0.0), volume_24h=0.0, change_5m=0.0,
        change_1h=0.0, change_6h=0.0, change_24h=0.0, fdv=m.get("fdv_usd", 0.0), buys_h1=0, sells_h1=0,
        pair_created_ms=None, url=f"https://tronscan.org/#/token20/{ca}", source="ferzan",
        extras={"ferzan_curve": tron_signer.to_b58(m["curve"]), "curve_progress_bps": m.get("progress_bps", 0)},
    )


def _tron_plain_snap(ca: str) -> MarketSnapshot | None:
    """A normal TRC-20 (no Ferzan curve) priced straight from the SunSwap V2 pool."""
    ca = (ca or "").strip()
    if not (ca.startswith("T") and len(ca) == 34):
        return None
    try:
        import tron_signer

        m = tron_signer.plain_meta(ca)
    except Exception:
        return None
    if not m:
        return None
    if m.get("price_usd", 0) <= 0:
        raise PriceFetchError(
            f"{m.get('symbol') or 'This token'} is a TRON token but has no SunSwap pool with liquidity yet, "
            "so there is nothing to buy. Try again once liquidity is added."
        )
    return MarketSnapshot(
        query=ca, symbol=m.get("symbol") or "?", name=m.get("name") or m.get("symbol") or "TRON token",
        chain="tron", dex="sunswap", pair_address="", token_address=ca, price_usd=m["price_usd"],
        liquidity_usd=0.0, volume_24h=0.0, change_5m=0.0, change_1h=0.0, change_6h=0.0, change_24h=0.0,
        fdv=m.get("fdv_usd", 0.0), buys_h1=0, sells_h1=0, pair_created_ms=None,
        url=f"https://tronscan.org/#/token20/{ca}", source="sunswap", extras={"sunswap_direct": True},
    )


def _ston_asset_snap(ca: str) -> MarketSnapshot | None:
    """A TON jetton that trades on STON.fi but DexScreener has not indexed yet (a fresh graduation)."""
    ca = (ca or "").strip()
    if not (ca.startswith(("EQ", "UQ", "kQ")) and len(ca) == 48):
        return None
    try:
        a = ((requests.get(f"https://api.ston.fi/v1/assets/{ca}", timeout=TIMEOUT).json() or {}).get("asset") or {})
        px = float(a.get("dex_price_usd") or a.get("third_party_usd_price") or 0)
    except Exception:
        return None
    if px <= 0:
        return None
    sym = str(a.get("symbol") or "?")
    return MarketSnapshot(
        query=ca, symbol=sym, name=str(a.get("display_name") or sym), chain="ton", dex="ston.fi", pair_address="",
        token_address=ca, price_usd=px, liquidity_usd=0.0, volume_24h=0.0, change_5m=0.0, change_1h=0.0,
        change_6h=0.0, change_24h=0.0, fdv=0.0, buys_h1=0, sells_h1=0, pair_created_ms=None,
        url=f"https://tonviewer.com/{ca}", source="ston.fi", extras={"ston_asset": True},
    )


def _evm_token_probe(ca: str) -> list[tuple[str, str, str]]:
    """Which EVM chains have a token contract at this address? [(chain id, label, symbol)]. Checks every chain Ferzan
    knows, in parallel, with a short timeout; used only to explain a lookup that found no market."""
    from concurrent.futures import ThreadPoolExecutor

    from chains import CHAINS

    ca = ca.strip()

    def one(item):
        cid, c = item
        if c.get("kind") != "evm" or not c.get("rpc"):
            return None
        try:
            r = requests.post(c["rpc"], json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                              "params": [{"to": ca, "data": "0x95d89b41"}, "latest"]}, timeout=4)
            h = (r.json() or {}).get("result") or "0x"
            if len(h) < 130:
                return None
            ln = int(h[66:130], 16)
            sym = bytes.fromhex(h[130:130 + ln * 2]).decode("utf-8", "ignore").strip("\x00")
            return (cid, c.get("label") or cid, sym) if sym else None
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=8) as ex:
        return [x for x in ex.map(one, list(CHAINS.items())) if x]


def load_market(query: str) -> MarketSnapshot:
    snap = search_dex(query)
    if snap and snap.price_usd > 0:
        return snap
    fz = _ferzan_curve_snap(query)
    if fz:
        return fz
    tp = _tron_plain_snap(query)  # normal TRC-20 DexScreener has not indexed
    if tp:
        return tp
    ts = _ston_asset_snap(query)  # graduated TON coin DexScreener has not indexed yet
    if ts:
        return ts
    if _looks_ca(query):
        g = _gecko_snap(query)
        if g:
            return g
        j = _jup_snap(query)
        if j and j.price_usd > 0:
            return j
        try:
            resp = requests.get(DEX_SEARCH, params={"q": query.strip()}, timeout=TIMEOUT)
            pairs = (resp.json() or {}).get("pairs") or []
        except requests.RequestException:
            pairs = []
        if pairs:
            chosen = snapshot_from_pair(pairs[0], query)
            chosen.extras["resolved"] = True
            chosen.extras["pasted"] = query.strip()
            return chosen

    coins = search_coin(query)
    if not coins:
        q = (query or "").strip()
        if q.lower().startswith("0x") and len(q) == 42:
            try:
                found = _evm_token_probe(q)
            except Exception:
                found = []
            if found:
                where = ", ".join(f"{lbl} (${sym})" for _cid, lbl, sym in found[:3])
                raise PriceFetchError(
                    f"Found the token on {where}, but no DEX pool with liquidity for it is indexed yet, so there is "
                    "no price to trade against. It can be bought once a pool exists (DexScreener usually lists a "
                    "new pool within a few minutes)."
                )
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
