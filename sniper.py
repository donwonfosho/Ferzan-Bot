"""Gated multi-chain sniper.

Maestro / Banana Gun fire as soon as a pair is tradable.
This module watches the same launches, then applies hard filters
before a paper fill. That is slower than a private-mempool bundle
and safer than a blind snipe.

Live first-block execution needs Flashbots/Jito + a signer. Not in
this process. The job here is: see the pool, refuse junk, fill paper
(or later attach a signed swap) only when gates pass.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

import db
import trading
from chains import CHAINS, resolve_chain
from confluence import build_signal
from price_fetcher import PriceFetchError, load_market, search_dex, snapshot_from_pair

GECKO_NEW = "https://api.geckoterminal.com/api/v2/networks/{network}/new_pools"
GECKO_TREND = "https://api.geckoterminal.com/api/v2/networks/{network}/trending_pools"
GECKO_POOLS = "https://api.geckoterminal.com/api/v2/networks/{network}/pools"
GECKO_NEW_ALL = "https://api.geckoterminal.com/api/v2/networks/new_pools"
DEX_PROFILES = "https://api.dexscreener.com/token-profiles/latest/v1"
DEX_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
TIMEOUT = 12


@dataclass
class Launch:
    chain: str
    symbol: str
    name: str
    token: str
    pool: str
    liquidity_usd: float
    created_at: str
    source: str
    query: str
    fdv_usd: float = 0.0
    price_usd: float = 0.0
    chg_1h: float = 0.0
    chg_24h: float = 0.0


def _headers() -> dict[str, str]:
    return {"Accept": "application/json", "User-Agent": "confluence-bot/1.0"}


def fetch_new_pools(chain: str | None = None, limit: int = 20) -> list[Launch]:
    gecko_id = None
    urls: list[tuple[str, str]] = []
    if chain:
        resolved = resolve_chain(chain)
        if not resolved:
            return []
        gecko_id = CHAINS[resolved]["gecko"]
        urls.append((gecko_id, GECKO_NEW.format(network=gecko_id)))
        urls.append((gecko_id, GECKO_TREND.format(network=gecko_id)))
        urls.append((gecko_id, GECKO_POOLS.format(network=gecko_id)))
    else:
        for cid in (
            "eth", "bsc", "base", "sol", "arb", "avax", "hood", "hype",
            "sonic", "monad", "pol", "pulse", "ink", "ton", "op", "linea", "trx",
        ):
            gid = CHAINS[cid]["gecko"]
            urls.append((gid, GECKO_NEW.format(network=gid)))
            urls.append((gid, GECKO_TREND.format(network=gid)))
            urls.append((gid, GECKO_POOLS.format(network=gid)))
    gecko_id = urls[0][0] if urls else ""
    out: list[Launch] = []
    for gid, url in urls:
        try:
            r = requests.get(url, headers=_headers(), timeout=TIMEOUT)
            r.raise_for_status()
            chunk = (r.json() or {}).get("data") or []
        except requests.RequestException:
            continue
        for row in chunk[:8]:
            row["_ferzan_net"] = gid
            data_row = row  # parsed below using same loop body
            attrs = (row.get("attributes") or {})
            rel = row.get("relationships") or {}
            net = (
                ((rel.get("network") or {}).get("data") or {}).get("id")
                or row.get("_ferzan_net")
                or gecko_id
                or ""
            )
            chain_id = _from_gecko(net)
            name = attrs.get("name") or "UNKNOWN"
            symbol = name.split("/")[0].strip() if name else "UNK"
            token = attrs.get("address") or ""
            base = ((rel.get("base_token") or {}).get("data") or {}).get("id") or ""
            if "_" in str(base):
                token = str(base).split("_", 1)[1] or token
            try:
                liq = float(attrs.get("reserve_in_usd") or 0)
            except (TypeError, ValueError):
                liq = 0.0
            try:
                fdv = float(attrs.get("fdv_usd") or attrs.get("market_cap_usd") or 0)
            except (TypeError, ValueError):
                fdv = 0.0
            try:
                price = float(attrs.get("base_token_price_usd") or 0)
            except (TypeError, ValueError):
                price = 0.0
            chg = attrs.get("price_change_percentage") or {}
            try:
                chg_1h = float(chg.get("h1") or 0)
            except (TypeError, ValueError):
                chg_1h = 0.0
            try:
                chg_24h = float(chg.get("h24") or 0)
            except (TypeError, ValueError):
                chg_24h = 0.0
            src = "geckoterminal"
            if "trending_pools" in url:
                src = "geckoterminal-trend"
            elif "/pools" in url and "new_pools" not in url:
                if chg_1h >= 8 or chg_24h >= 20:
                    src = "geckoterminal-mover"
                else:
                    continue
            out.append(
                Launch(
                    chain=chain_id or net,
                    symbol=symbol,
                    name=name,
                    token=token,
                    pool=attrs.get("address") or "",
                    liquidity_usd=liq,
                    created_at=str(attrs.get("pool_created_at") or ""),
                    source=src,
                    query=token or name,
                    fdv_usd=fdv,
                    price_usd=price,
                    chg_1h=chg_1h,
                    chg_24h=chg_24h,
                )
            )
    # DexScreener paid/hot profiles
    try:
        hot = requests.get("https://api.dexscreener.com/token-boosts/latest/v1", timeout=TIMEOUT)
        rows = hot.json() if hot.ok else []
    except requests.RequestException:
        rows = []
    if not isinstance(rows, list):
        rows = []
    for row in rows[:15]:
        ca = row.get("tokenAddress") or ""
        ds = (row.get("chainId") or "").lower()
        cid = _from_gecko(ds) or ds
        if chain and resolve_chain(chain) and cid != resolve_chain(chain):
            continue
        if not ca:
            continue
        out.append(
            Launch(
                chain=cid,
                symbol=(row.get("description") or "HOT")[:12],
                name="DexScreener hot",
                token=ca,
                pool=ca,
                liquidity_usd=0,
                created_at="",
                source="dexscreener-boost",
                query=ca,
            )
        )
    return out


def _from_gecko(net: str) -> str:
    for cid, meta in CHAINS.items():
        if meta["gecko"] == net or meta["dexscreener"] == net:
            return cid
    return net


def fetch_fresh_profiles(limit: int = 15) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for url in (DEX_PROFILES, DEX_BOOSTS):
        try:
            r = requests.get(url, headers=_headers(), timeout=TIMEOUT)
            r.raise_for_status()
            payload = r.json()
        except requests.RequestException:
            continue
        if isinstance(payload, list):
            rows.extend(payload[:limit])
        elif isinstance(payload, dict) and "data" in payload:
            rows.extend(payload.get("data") or [])
    return rows[:limit]


def inspect_target(query: str, chain: str | None = None):
    q = query.strip()
    if chain:
        resolved = resolve_chain(chain)
        if resolved:
            q = f"{q} {CHAINS[resolved]['dexscreener']}"
    snap = search_dex(q)
    if snap is None:
        snap = load_market(query)
    return build_signal(snap)


def gates_for(order: dict[str, Any], card) -> tuple[bool, str]:
    s = card.snapshot
    want = resolve_chain(order.get("chain") or "")
    if want:
        ds = CHAINS[want]["dexscreener"]
        if s.chain not in {ds, want, CHAINS[want]["gecko"]} and s.source != "coingecko":
            # DexScreener chain ids are 'ethereum', 'solana', ...
            if s.chain != ds:
                return False, f"Wrong chain ({s.chain}, armed for {want})"
    min_liq = float(order.get("min_liq") or 0)
    if min_liq and s.liquidity_usd < min_liq:
        return False, f"Liq ${s.liquidity_usd:,.0f} < min ${min_liq:,.0f}"
    max_age_h = order.get("max_age_h")
    if max_age_h and s.pair_created_ms:
        age_h = max(0.0, (time.time() * 1000 - s.pair_created_ms) / 3_600_000)
        if age_h > float(max_age_h):
            return False, f"Pool age {age_h:.1f}h > {max_age_h}h"
    min_score = int(order.get("min_score") or 0)
    if card.score < min_score:
        return False, f"Score {card.score} < floor {min_score}"
    if order.get("require_long") and card.bias != "LONG":
        return False, f"Bias {card.bias}, not LONG"
    if card.vetoes and order.get("block_veto", 1):
        return False, "Veto: " + "; ".join(card.vetoes)
    return True, "gates passed"


def arm(
    user_id: int,
    query: str,
    chain: str | None,
    usd: float,
    min_liq: float,
    min_score: int,
    max_age_h: float | None,
    require_long: bool,
) -> int:
    return db.add_snipe(
        user_id=user_id,
        query=query,
        chain=chain or "",
        usd=usd,
        min_liq=min_liq,
        min_score=min_score,
        max_age_h=max_age_h,
        require_long=1 if require_long else 0,
    )


def try_fill(order: dict[str, Any]) -> tuple[str, str]:
    """Returns (status, message). status is armed|filled|miss|error."""
    try:
        card = inspect_target(order["query"], order.get("chain") or None)
    except PriceFetchError as exc:
        return "armed", f"Waiting on market data: {exc}"
    ok, reason = gates_for(order, card)
    if not ok:
        return "armed", reason
    filled, msg = trading.paper_buy(
        int(order["user_id"]),
        card,
        force=True,
        override_usd=float(order["usd"]) if order.get("usd") else None,
    )
    if not filled:
        return "miss", msg
    live_line = ""
    try:
        import evm_signer
        import signer
        import user_wallets

        uid = int(order["user_id"])
        mint = (card.snapshot.token_address or order.get("query") or "").strip()
        usd = min(signer.max_usd(), float(order.get("usd") or signer.max_usd()))
        sol_secret, evm_secret = user_wallets.secrets(uid)
        if mint.startswith("0x"):
            _ok, live_line = evm_signer.buy_evm(order.get("chain") or "base", mint, usd, key_hex=evm_secret)
        elif mint:
            _ok, live_line = signer.buy_sol(mint, usd, secret=sol_secret)
    except Exception as exc:
        live_line = f"Live snipe skipped: {exc}"
    msg = msg + ("\n" + live_line if live_line else "")
    db.finish_snipe(int(order["id"]), "filled", msg)
    return "filled", msg


def scan_armed() -> list[tuple[int, int, str, str]]:
    notices = []
    for order in db.active_snipes():
        status, msg = try_fill(order)
        notices.append((int(order["user_id"]), int(order["id"]), status, msg))
    return notices
