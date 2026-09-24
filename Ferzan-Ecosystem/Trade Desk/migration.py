"""pump.fun graduation ("migration") feed.

When a pump.fun bonding curve completes, the token gets a real AMM pool
(PumpSwap today; Raydium for older graduations). That moment is what the
migration sniper watches: GeckoTerminal's Solana new-pools list, filtered to
pools on a pump AMM (not the bonding curve itself) quoted in SOL, created in
the last few minutes.

Blocking (HTTP) - call via asyncio.to_thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

import requests

GT_NEW = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"
WSOL = "So11111111111111111111111111111111111111112"
MAX_POOL_AGE_S = 600  # alerts: show graduations up to 10 min old
MAX_BUY_AGE_S = 120  # auto-buy: only the first 2 minutes, never a backlog
# GeckoTerminal indexes the pump.fun bonding curve itself as a dex too;
# those pools are NOT graduations.
CURVE_DEX_IDS = {"pump-fun", "pumpfun", "pump-fun-bonding-curve"}


@dataclass
class Migration:
    mint: str
    pool: str
    dex: str
    symbol: str
    name: str
    liq_usd: float
    fdv_usd: float
    price_usd: float
    created_ts: int


def is_migration_pool(dex_id: str, mint: str, quote: str) -> bool:
    dex = (dex_id or "").lower()
    if quote and quote != WSOL:
        return False
    if dex in CURVE_DEX_IDS:
        return False
    if "pump" in dex:  # pumpswap / pump-fun-amm
        return True
    # Older graduations (and LaunchLab-style migrations) land on Raydium;
    # only count them for pump.fun mints.
    return dex.startswith("raydium") and mint.lower().endswith("pump")


def _ts(raw: str) -> int:
    try:
        return int(datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


def _id_tail(rel: dict, key: str) -> str:
    raw = str((((rel or {}).get(key) or {}).get("data") or {}).get("id") or "")
    return raw.split("_", 1)[1] if "_" in raw else raw


def parse_pools(payload: dict, now: float | None = None) -> list[Migration]:
    now = now or time.time()
    out = []
    for row in (payload or {}).get("data") or []:
        attrs = row.get("attributes") or {}
        rel = row.get("relationships") or {}
        dex_id = str((((rel.get("dex") or {}).get("data") or {}).get("id")) or "")
        mint = _id_tail(rel, "base_token")
        quote = _id_tail(rel, "quote_token")
        if not mint or not is_migration_pool(dex_id, mint, quote):
            continue
        created = _ts(attrs.get("pool_created_at"))
        if not created or now - created > MAX_POOL_AGE_S:
            continue

        def f(key: str) -> float:
            try:
                return float(attrs.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        name = str(attrs.get("name") or "")
        out.append(
            Migration(
                mint=mint,
                pool=str(attrs.get("address") or row.get("id") or ""),
                dex=dex_id,
                symbol=(name.split("/")[0].strip() or "?")[:16],
                name=name[:40],
                liq_usd=f("reserve_in_usd"),
                fdv_usd=f("fdv_usd") or f("market_cap_usd"),
                price_usd=f("base_token_price_usd"),
                created_ts=created,
            )
        )
    return out


def fetch_migrations(pages: int = 2) -> tuple[bool, list[Migration]]:
    """(ok, graduations). ok=False when the FIRST page failed (rate limit,
    timeout): callers must not treat that as 'nothing new'."""
    out: list[Migration] = []
    seen = set()
    for page in range(1, pages + 1):
        try:
            r = requests.get(
                GT_NEW,
                params={"page": page, "include": "dex,base_token,quote_token"},
                headers={"Accept": "application/json"},
                timeout=10,
            )
            if r.status_code != 200:
                if page == 1:
                    return False, []
                break
            batch = parse_pools(r.json())
        except Exception:
            if page == 1:
                return False, []
            break
        for m in batch:
            if m.pool not in seen:
                seen.add(m.pool)
                out.append(m)
    return True, out


def passes_filters(m: Migration, rep: dict, cfg: dict) -> tuple[bool, str]:
    """Gate one graduation for one user's settings. Unknown safety = no."""
    if not rep.get("ok"):
        return False, "safety check unavailable"
    if rep.get("mint_auth"):
        return False, "mint authority still on"
    if rep.get("freeze_auth"):
        return False, "freeze authority still on"
    if rep.get("block_flags"):
        return False, rep["block_flags"][0]
    top10 = rep.get("top10_pct")
    if top10 is None:
        return False, "holder data unavailable"
    if top10 > float(cfg.get("max_top10") or 30):
        return False, f"top 10 hold {top10:.0f}%"
    if m.liq_usd <= 0:
        return False, "liquidity unknown"
    if m.liq_usd < float(cfg.get("min_liq") or 0):
        return False, f"liquidity ${m.liq_usd:,.0f}"
    return True, ""
