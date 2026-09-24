"""
solana_mm.py

Volume-generation market-making loop for Solana tokens -- ANY Solana
token, not just one launchpad. Pastes a mint, alternates small buy/sell
round-trips against the user's OWN linked Ferzan wallet (the same
non-custodial Solana wallet Ferzan Trade Bot already generated for them
via user_wallets.py -- we read it, we never create or hold a separate
one), signing and broadcasting every transaction ourselves.

Routing, in order:
  1. Jupiter's aggregator (lite-api.jup.ag, no API key -- this is the
     exact endpoint Trade Desk's signer.py already uses in production
     for /buy and /livesell, so this is a proven-working path, not a
     new integration). Covers anything with real liquidity on Orca,
     Raydium, Meteora, migrated pump.fun pools, etc. -- i.e. "any
     Solana project" in the general sense.
  2. PumpPortal's local-transaction API (pumpportal.fun, no API key)
     as a fallback specifically for brand-new launchpad tokens that
     are still on a bonding curve and not yet indexed by Jupiter --
     pump.fun, pump-amm, LaunchLab, Raydium/Raydium CPMM, and Bonk
     pools, via its 'auto' pool selection.
  Both return a transaction for US to sign locally with the user's own
  key -- neither service ever touches a private key. This mirrors the
  exact sign-locally pattern bridge.py already uses for deBridge.

NOT implemented in this version: actual liquidity provision (LPing).
Volume mode only, same scope as basestonk_mm.py.

Money moves here. Test with a $2-3 budget on a token you don't mind
losing gas on before trusting this with anything bigger.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import random
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import requests

log = logging.getLogger("solana_mm")

# ---- endpoints (no API key needed for either) -------------------------

JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"
PUMPPORTAL_TRADE_LOCAL = "https://pumpportal.fun/api/trade-local"

SOL_MINT = "So11111111111111111111111111111111111111112"

MM_DB_PATH = Path(os.getenv("MM_DB_PATH", str(Path(__file__).resolve().parent / "liq_mm.db")))

# session guardrails -- same values as basestonk_mm.py for consistency
MIN_TRADE_USD = 1.0
MAX_TRADE_USD = 25.0
MIN_BUDGET_USD = 5.0
MAX_BUDGET_USD = 1000.0
MAX_MINUTES = 6 * 60
DWELL_MIN_S = 20
DWELL_MAX_S = 90
MAX_CONSECUTIVE_FAILS = 3
SLIPPAGE_PCT = 3.0  # 300 bps, matches basestonk_mm.py's default
PRIORITY_FEE_SOL = float(os.getenv("SOL_MM_PRIORITY_FEE_SOL", "0.0015"))

_active: dict[int, dict] = {}  # user_id -> {"task": Task, "stop": bool}


def _rpc() -> str:
    """Same resolution order as Trade Desk's signer.py -- reuses whatever
    RPC is already configured for the rest of Ferzan, no new setup."""
    url = (os.getenv("SOLANA_RPC_URL") or "").strip()
    if url:
        return url
    key = (os.getenv("HELIUS_API_KEY") or "").strip()
    if key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    return "https://api.mainnet-beta.solana.com"


# ---- shared wallet (same DB + encryption Ferzan Trade Bot writes to) --

FERZAN_DB_PATH = Path(os.getenv("DB_PATH", "/opt/ferzan/app/ferzan.db"))
MASTER_PATH = Path(os.getenv("FERZAN_MASTER_PATH", "/opt/ferzan/app/.master"))


def _fernet():
    from cryptography.fernet import Fernet

    secret = (os.getenv("FERZAN_MASTER_KEY") or "").strip()
    if not secret:
        if MASTER_PATH.exists():
            secret = MASTER_PATH.read_text().strip()
        else:
            raise RuntimeError("No FERZAN_MASTER_KEY and no .master file -- can't read the shared wallet store.")
    digest = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def get_linked_sol_key(user_id: int) -> tuple[str, str] | None:
    """Returns (sol_address, sol_secret_base58_or_b64) for the user's
    existing Ferzan wallet, or None if they haven't opened Trade Bot yet.
    Same table/encryption user_wallets.py already uses -- this only reads
    it, it never creates a wallet (Trade Bot owns that)."""
    if not FERZAN_DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(FERZAN_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT sol_pub, sol_key FROM user_wallets WHERE user_id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    if not row or not row["sol_pub"]:
        return None
    secret = _fernet().decrypt(row["sol_key"].encode()).decode()
    return row["sol_pub"], secret


def _keypair_from_secret(secret: str):
    from solders.keypair import Keypair

    secret = (secret or "").strip()
    try:
        return Keypair.from_base58_string(secret)
    except Exception:
        return Keypair.from_bytes(base64.b64decode(secret))


# ---- mm session log (same schema basestonk_mm.py uses -- CREATE TABLE
# IF NOT EXISTS makes sharing the one liq_mm.db file safe between the
# two modules; rows are told apart by the `chain` column) -------------


@contextmanager
def _mmdb():
    conn = sqlite3.connect(str(MM_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_mmdb():
    with _mmdb() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS mm_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                chain TEXT NOT NULL,
                token TEXT NOT NULL,
                started_at REAL NOT NULL,
                ended_at REAL,
                budget_usd REAL NOT NULL,
                spent_usd REAL NOT NULL DEFAULT 0,
                trades INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running'
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS mm_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                side TEXT NOT NULL,
                ts REAL NOT NULL,
                usd REAL,
                tx_hash TEXT,
                ok INTEGER NOT NULL,
                note TEXT
            )"""
        )


_init_mmdb()


# ---- low-level chain plumbing ------------------------------------------


def _post_rpc(method: str, params: list) -> dict:
    r = requests.post(_rpc(), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
    return r.json() if r.content else {}


def _sol_balance_lamports(addr: str) -> int:
    body = _post_rpc("getBalance", [addr])
    return int(((body.get("result") or {}).get("value")) or 0)


def _token_raw_balance(mint: str, owner: str) -> int:
    body = _post_rpc("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    total = 0
    for acc in (body.get("result") or {}).get("value") or []:
        info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        try:
            total += int((info.get("tokenAmount") or {}).get("amount") or "0")
        except ValueError:
            pass
    return total


def _native_usd() -> float:
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            timeout=10,
        }
        )
        return float((r.json() or {}).get("solana", {}).get("usd") or 0)
    except Exception:
        return 0.0


def _sign_and_send_versioned(kp, raw_tx_bytes: bytes) -> tuple[bool, str]:
    """Deserializes an unsigned (or Jupiter/PumpPortal-built) versioned
    transaction, resigns it with the user's own key, and broadcasts.
    Same pattern bridge.py already proves out for deBridge -- nothing
    new here, just reused."""
    from solders.transaction import VersionedTransaction

    try:
        tx = VersionedTransaction.from_bytes(raw_tx_bytes)
        signed = VersionedTransaction(tx.message, [kp])
    except Exception as exc:
        return False, f"could not sign transaction: {exc}"
    body = _post_rpc(
        "sendTransaction",
        [base64.b64encode(bytes(signed)).decode(), {"encoding": "base64", "skipPreflight": False}],
    )
    if body.get("error"):
        err = body["error"]
        return False, str(err.get("message") if isinstance(err, dict) else err)
    sig = body.get("result") or ""
    if not sig:
        return False, "RPC accepted nothing."
    return True, sig


def _wait_confirmed(sig: str, tries: int = 12, delay: float = 3.0) -> bool | None:
    """True = confirmed, False = failed, None = still unknown after tries."""
    for _ in range(tries):
        body = _post_rpc("getSignatureStatuses", [[sig], {"searchTransactionHistory": True}])
        vals = (body.get("result") or {}).get("value") or [None]
        st = vals[0]
        if st:
            if st.get("err"):
                return False
            if st.get("confirmationStatus") in ("confirmed", "finalized"):
                return True
        time.sleep(delay)
    return None


# ---- leg 1: Jupiter (the general-purpose router) -----------------------


def _jupiter_swap_tx(input_mint: str, output_mint: str, amount_raw: int, user_pubkey: str, slippage_bps: int) -> bytes | None:
    """Returns raw unsigned tx bytes, or None if Jupiter has no route for
    this pair (typically: token too new / not yet indexed -- caller
    should fall back to PumpPortal, not treat this as a hard failure)."""
    try:
        qr = requests.get(
            JUP_QUOTE,
            params={
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount_raw),
                "slippageBps": str(slippage_bps),
            },
            timeout=15,
        )
        quote = qr.json() if qr.content else {}
    except requests.RequestException:
        return None
    if qr.status_code >= 400 or quote.get("error") or not quote.get("routePlan"):
        return None
    try:
        sr = requests.post(
            JUP_SWAP,
            json={
                "quoteResponse": quote,
                "userPublicKey": user_pubkey,
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": int(PRIORITY_FEE_SOL * 1_000_000_000),
            },
            timeout=20,
        )
        swap = sr.json() if sr.content else {}
    except requests.RequestException:
        return None
    raw_tx = swap.get("swapTransaction")
    if not raw_tx:
        return None
    try:
        return base64.b64decode(raw_tx)
    except Exception:
        return None


# ---- leg 2: PumpPortal (launchpad-native fallback for brand-new tokens) --


def _pumpportal_tx(
    public_key: str, action: str, mint: str, amount, denominated_in_sol: bool, slippage_pct: float, pool: str = "auto"
) -> bytes | None:
    """PumpPortal's local-transaction API returns the serialized
    transaction as the raw HTTP response body (not JSON, not base64) --
    per their own docs example (VersionedTransaction.from_bytes(response.content)).
    priorityFee is in SOL, slippage is a percentage -- both confirmed
    against PumpPortal's own documented example, not guessed."""
    try:
        r = requests.post(
            PUMPPORTAL_TRADE_LOCAL,
            data={
                "publicKey": public_key,
                "action": action,
                "mint": mint,
                "amount": amount,
                "denominatedInSol": "true" if denominated_in_sol else "false",
                "slippage": slippage_pct,
                "priorityFee": PRIORITY_FEE_SOL,
                "pool": pool,
            },
            timeout=20,
        )
    except requests.RequestException:
        return None
    if r.status_code >= 400 or not r.content:
        return None
    return r.content


# ---- unified buy/sell: try Jupiter, fall back to PumpPortal ---------


def _do_buy(kp, address: str, mint: str, lamports_in: int) -> tuple[bool, str, str | None]:
    slippage_bps = int(SLIPPAGE_PCT * 100)
    raw = _jupiter_swap_tx(SOL_MINT, mint, lamports_in, address, slippage_bps)
    via = "jupiter"
    if raw is None:
        raw = _pumpportal_tx(address, "buy", mint, lamports_in / 1e9, True, SLIPPAGE_PCT)
        via = "pumpportal"
    if raw is None:
        return False, "no route found on Jupiter or PumpPortal for this token", None
    ok, res = _sign_and_send_versioned(kp, raw)
    if not ok:
        return False, f"{via} buy send failed: {res}", None
    if _wait_confirmed(res) is False:
        return False, f"{via} buy reverted", res
    return True, "ok", res


def _do_sell(kp, address: str, mint: str) -> tuple[bool, str, str | None]:
    bal = _token_raw_balance(mint, address)
    if bal <= 0:
        return False, "no balance of this token to sell", None
    slippage_bps = int(SLIPPAGE_PCT * 100)
    raw = _jupiter_swap_tx(mint, SOL_MINT, bal, address, slippage_bps)
    via = "jupiter"
    if raw is None:
        # PumpPortal accepts a percentage directly for sells -- no need
        # to know the exact raw balance ourselves for this leg.
        raw = _pumpportal_tx(address, "sell", mint, "100%", False, SLIPPAGE_PCT)
        via = "pumpportal"
    if raw is None:
        return False, "no route found on Jupiter or PumpPortal for this token", None
    ok, res = _sign_and_send_versioned(kp, raw)
    if not ok:
        return False, f"{via} sell send failed: {res}", None
    if _wait_confirmed(res) is False:
        return False, f"{via} sell reverted", res
    return True, "ok", res


# ---- the loop ----------------------------------------------------------


async def _run(user_id: int, token: str, trade_usd: float, budget_usd: float, minutes: int, notify):
    try:
        await _run_inner(user_id, token, trade_usd, budget_usd, minutes, notify)
    except Exception as exc:
        log.exception("Solana MM session for user %s crashed", user_id)
        try:
            await notify(f"MM session hit an unexpected error and stopped: {type(exc).__name__}: {exc}")
        except Exception:
            pass
    finally:
        _active.pop(user_id, None)


async def _run_inner(user_id: int, token: str, trade_usd: float, budget_usd: float, minutes: int, notify):
    wallet = get_linked_sol_key(user_id)
    if not wallet:
        await notify("No linked wallet found. Open Ferzan Trade Bot, send /start once (it generates your wallet), then try /mm again.")
        return
    address, secret = wallet
    kp = _keypair_from_secret(secret)

    px = _native_usd() or 150.0
    bal_lamports = await asyncio.to_thread(_sol_balance_lamports, address)
    bal_usd = (bal_lamports / 1e9) * px
    if bal_usd < trade_usd * 2 + 2:
        await notify(
            f"Wallet {address} only has ~${bal_usd:.2f} of SOL. "
            f"Fund it with at least ${trade_usd * 2 + 2:.2f} worth of SOL before running MM."
        )
        return

    with _mmdb() as c:
        cur = c.execute(
            "INSERT INTO mm_sessions (user_id, chain, token, started_at, budget_usd) VALUES (?,?,?,?,?)",
            (user_id, "solana", token, time.time(), budget_usd),
        )
        session_id = cur.lastrowid

    deadline = time.time() + minutes * 60
    spent = 0.0
    trades = 0
    fails = 0
    entry = _active[user_id]

    await notify(f"MM started on Solana: {token[:10]}… · ${trade_usd:.2f}/round · budget ${budget_usd:.2f} · {minutes}m")

    while not entry.get("stop") and time.time() < deadline and spent < budget_usd:
        lamports_in = int((trade_usd / max(px, 1e-9)) * 1e9)

        ok, detail, sig = await asyncio.to_thread(_do_buy, kp, address, token, lamports_in)
        with _mmdb() as c:
            c.execute(
                "INSERT INTO mm_trades (session_id, side, ts, usd, tx_hash, ok, note) VALUES (?,?,,?,,?, ?, ?)",
                (session_id, "buy", time.time(), trade_usd, sig, int(ok), detail),
            )
        if not ok:
            fails += 1
            log.warning("solana mm buy failed user=%s: %s", user_id, detail)
            if fails >= MAX_CONSECUTIVE_FAILS:
                await notify(f"Stopping: {fails} failed trades in a row. Last error: {detail}")
                break
            await asyncio.sleep(15)
            continue
        fails = 0
        spent += trade_usd
        trades += 1

        await asyncio.sleep(random.uniform(DWELL_MIN_S, DWELL_MAX_S))
        if entry.get("stop"):
            break

        ok, detail, sig = await asyncio.to_thread(_do_sell, kp, address, token)
        with _mmdb() as c:
            c.execute(
                "INSERT INTO mm_trades (session_id, side, ts, usd, tx_hash, ok, note) VALUES (?,?,?,?,?,?,?)",
                (session_id, "sell", time.time(), None, sig, int(ok), detail),
            )
        if not ok:
            fails += 1
            log.warning("solana mm sell failed user=%s: %s", user_id, detail)
            if fails >= MAX_CONSECUTIVE_FAILS:
                await notify(f"Stopping: sells failing ({detail}). Your tokens are still in your wallet -- sell manually if needed.")
                break
        else:
            fails = 0
            trades += 1

        with _mmdb() as c:
            c.execute("UPDATE mm_sessions SET spent_usd=?, trades=? WHERE id=?", (spent, trades, session_id))

        await asyncio.sleep(random.uniform(DWELL_MIN_S, DWELL_MAX_S))

    with _mmdb() as c:
        c.execute(
            "UPDATE mm_sessions SET ended_at=?, spent_usd=?, trades=?, status=? WHERE id=?",
            (time.time(), spent, trades, "stopped" if entry.get("stop") else "finished", session_id),
        )

    await notify(f"MM ended on Solana: {trades} legs, ~${spent:.2f} of volume routed. /mm again to run another round.")
    _active.pop(user_id, None)


def start(user_id: int, token: str, trade_usd: float, budget_usd: float, minutes: int, notify) -> str | None:
    """Returns an error string if it couldn't start, else None and a task is running."""
    if user_id in _active:
        return "You already have an MM session running. /mmstop first."
    token = (token or "").strip()
    if token.startswith("0x") or len(token) < 32 or len(token) > 44:
        return "Need a Solana mint address (base58, not a 0x address)."
    trade_usd = max(MIN_TRADE_USD, min(MAX_TRADE_USD, trade_usd))
    budget_usd = max(MIN_BUDGET_USD, min(MAX_BUDGET_USD, budget_usd))
    minutes = max(5, min(MAX_MINUTES, minutes))

    entry = {"stop": False}
    _active[user_id] = entry
    task = asyncio.create_task(_run(user_id, token, trade_usd, budget_usd, minutes, notify))
    entry["task"] = task
    return None


def stop(user_id: int) -> bool:
    entry = _active.get(user_id)
    if not entry:
        return False
    entry["stop"] = True
    return True


def status(user_id: int) -> dict | None:
    with _mmdb() as c:
        row = c.execute(
            "SELECT * FROM mm_sessions WHERE user_id=? AND chain='solana' ORDER BY id DESC LIMIT 1", (user_id,)
        ).fetchone()
    if not row:
        return None
    return dict(row)
