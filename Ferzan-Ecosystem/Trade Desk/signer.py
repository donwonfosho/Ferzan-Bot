"""Hot-wallet helper for THIS droplet only.

SIGNER_KEY (preferred) or SIGNER_MNEMONIC in .env.
Live sends stay off until LIVE_BUYS=1.
Capped by SIGNER_MAX_USD (default 10).
"""

from __future__ import annotations

import base64
import logging
import os

import requests

log = logging.getLogger("signer")

SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
# Jito block engine (docs.jito.wtf "Low Latency Transaction Send").
# bundleOnly=true = revert protection: the tx only lands if it succeeds, and
# never touches the public path where it could be sandwiched.
JITO_TX = os.getenv("JITO_BLOCK_ENGINE", "https://mainnet.block-engine.jito.wtf") + "/api/v1/transactions?bundleOnly=true"
JITO_MIN_TIP = 1_000  # lamports, Jito's floor
DEFAULT_FEE_LAMPORTS = 1_000_000  # 0.001 SOL — what the bot always spent


def exec_opts(user_id: int | None) -> dict:
    """Per-user execution settings from /settings + the buy panel:
    anti_mev (default ON) -> Jito route; the panel's per-chain "⛽ Gas" SOL
    value -> Jito tip (MEV route) or priority fee (normal route)."""
    anti_mev, gas_sol = True, 0.0
    if user_id:
        try:
            import db

            anti_mev = db.flag_on(int(user_id), "anti_mev", 1)
            gas_sol = float(db.get_chain_trade(int(user_id), "sol").get("gas") or 0)
        except Exception:
            pass
    fee = int(gas_sol * 1_000_000_000) if gas_sol > 0 else int(
        os.getenv("PRIORITY_FEE_LAMPORTS", str(DEFAULT_FEE_LAMPORTS))
    )
    return {"anti_mev": bool(anti_mev), "fee_lamports": max(JITO_MIN_TIP, fee)}


def sol_usd() -> float:
    """SOL price, never guessed: CoinGecko, then a live Jupiter quote
    (1 SOL -> USDC). Raises if both fail so no buy is sized off a made-up
    price."""
    try:
        from price_fetcher import get_price_usd

        px = float(get_price_usd("solana") or 0)
        if px > 0:
            return px
    except Exception:
        pass
    try:
        r = requests.get(
            JUP_QUOTE,
            params={"inputMint": SOL_MINT, "outputMint": USDC_MINT, "amount": "1000000000", "slippageBps": "50"},
            timeout=10,
        )
        out = int((r.json() or {}).get("outAmount") or 0)
        if out > 0:
            return out / 1e6
    except Exception:
        pass
    raise RuntimeError("SOL price unavailable (CoinGecko + Jupiter) — nothing sent.")


def _status(sig: str, history: bool = False) -> tuple[str, str]:
    """One getSignatureStatuses call -> ("ok"|"err"|"none"|"unknown", detail).
    "none" means the RPC answered and has NO record of the tx; "unknown"
    means we couldn't get an answer (network error, 429, malformed)."""
    try:
        st = requests.post(
            _rpc(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getSignatureStatuses",
                "params": [[sig], {"searchTransactionHistory": bool(history)}],
            },
            timeout=10,
        ).json()
    except Exception as exc:
        return "unknown", str(exc)
    res = st.get("result") if isinstance(st, dict) else None
    if not isinstance(res, dict) or not isinstance(res.get("value"), list):
        return "unknown", str((st or {}).get("error") if isinstance(st, dict) else st)[:120]
    row = (res["value"] or [None])[0]
    if row is None:
        return "none", ""
    if row.get("err"):
        return "err", str(row["err"])
    if row.get("confirmationStatus") in {"confirmed", "finalized"}:
        return "ok", ""
    return "none", ""  # seen but only "processed" -> keep waiting


def _block_height() -> int | None:
    try:
        h = requests.post(
            _rpc(),
            json={"jsonrpc": "2.0", "id": 1, "method": "getBlockHeight", "params": [{"commitment": "confirmed"}]},
            timeout=10,
        ).json()
        v = h.get("result")
        return int(v) if v is not None else None
    except Exception:
        return None


def _confirm(sig: str, last_valid_height: int | None, timeout_s: float = 90.0) -> tuple[bool | None, str]:
    """Wait until `sig` lands (ok / failed on-chain) or PROVABLY can't land.
    "Expired" requires all of: the status call in the same round succeeded
    and returned no record, block height is past lastValidBlockHeight, and a
    final full-history lookup also finds nothing. Anything short of that
    (rate limits, RPC errors) stays "unknown" -- never a false "expired",
    because callers treat expired as "safe to resend".
    Returns (True, ""), (False, reason) or (None, reason)."""
    import time as _t

    deadline = _t.monotonic() + timeout_s
    while _t.monotonic() < deadline:
        _t.sleep(2)
        state, detail = _status(sig)
        if state == "ok":
            return True, ""
        if state == "err":
            return False, f"Failed on-chain: {detail}"
        if state != "none" or not last_valid_height:
            continue
        height = _block_height()
        # +10 blocks of margin: height and status may come from different
        # backend nodes behind a load-balanced RPC.
        if height is None or height <= int(last_valid_height) + 10:
            continue
        final, detail = _status(sig, history=True)
        if final == "ok":
            return True, ""
        if final == "err":
            return False, f"Failed on-chain: {detail}"
        if final == "none":
            return False, "Expired without landing (nothing spent)."
    return None, "Not confirmed yet — check the link before retrying."


# Error text meaning "resend with a bigger priority fee", not "this trade is
# broken" (bad slippage, insufficient balance, etc. should NOT retry).
_RETRYABLE_HINTS = (
    "blockhash not found",
    "block height exceeded",
    "node is behind",
)
# Replies that don't prove the tx was NOT forwarded: confirm before resend.
_AMBIGUOUS_HINTS = (
    "timed out",
    "timeout",
    "too many requests",
    "rate limit",
)


def _swap_send_with_retry(quote: dict, kp, opts: dict | None = None) -> tuple[bool, str]:
    """Builds + signs + sends a Jupiter swap, then waits for it to land.

    anti_mev (default): Jupiter adds a Jito tip and we send to Jito's block
    engine with bundleOnly (revert-protected, no public path). Otherwise the
    normal RPC path with a priority fee, retried with a bumped fee if the
    RPC rejects it for a reason a higher fee / fresh blockhash would fix.
    Returns (ok, signature) — ok only once the swap is CONFIRMED on-chain."""
    opts = opts if opts is not None else exec_opts(None)
    fee = int(opts["fee_lamports"])
    if not opts.get("anti_mev"):
        opts["route_used"] = "priority fee"
        return _swap_send_rpc(quote, kp, fee)
    opts["route_used"] = "Jito · MEV-protected"
    ok, res = _swap_send_jito(quote, kp, fee, opts)
    if ok or not res.startswith(_JITO_EXPIRED) or not _jito_fallback_on():
        return ok, res
    # The Jito tx PROVABLY can't land any more (blockhash expired, full-history
    # lookup found nothing), so a fresh send can't double-trade. Re-quote --
    # the old quote is ~a minute stale -- and go the normal route.
    log.warning("jito tx expired unlanded; falling back to priority-fee route")
    fresh = _requote(quote)
    if fresh is None:
        return False, res + "\nFallback re-quote failed — nothing else sent."
    opts["route_used"] = "priority fee (Jito didn't land, resent)"
    return _swap_send_rpc(fresh, kp, fee)


_JITO_EXPIRED = "Expired without landing"


def _jito_fallback_on() -> bool:
    return (os.getenv("JITO_FALLBACK", "1").strip().lower()) not in {"0", "false", "off", "no"}


def _requote(quote: dict) -> dict | None:
    """Fresh Jupiter quote with the same pair, size and slippage."""
    try:
        params = {
            "inputMint": str(quote["inputMint"]),
            "outputMint": str(quote["outputMint"]),
            "amount": str(int(quote["inAmount"])),
            "slippageBps": str(int(quote.get("slippageBps") or 1000)),
        }
        qr = requests.get(JUP_QUOTE, params=params, timeout=15)
        fresh = qr.json() if qr.content else {}
    except Exception as exc:
        log.warning("fallback re-quote failed: %s", exc)
        return None
    if qr.status_code >= 400 or not isinstance(fresh, dict) or fresh.get("error") or not fresh.get("outAmount"):
        log.warning("fallback re-quote rejected: %s", str(fresh)[:200])
        return None
    return fresh


def _jito_bundle_status(bundle_id: str) -> str:
    """Best-effort Jito verdict on a bundle, for the logs only."""
    if not bundle_id:
        return "no bundle id"
    base = os.getenv("JITO_BLOCK_ENGINE", "https://mainnet.block-engine.jito.wtf")
    payload = {"jsonrpc": "2.0", "id": 1, "method": "getInflightBundleStatuses", "params": [[bundle_id]]}
    for path in ("/api/v1/getInflightBundleStatuses", "/api/v1/bundles"):
        try:
            r = requests.post(base + path, json=payload, timeout=8)
            if r.status_code == 404:
                continue
            body = r.json() if r.content else {}
            rows = ((body.get("result") or {}).get("value")) or []
            if rows:
                row = rows[0] or {}
                return f"{row.get('status')} (landed_slot={row.get('landed_slot')})"
            return str(body.get("error") or body)[:200]
        except Exception as exc:
            return f"status lookup failed: {exc}"
    return "status endpoint not found"


def _jupiter_swap_tx(quote: dict, kp, fee_field) -> dict:
    sr = requests.post(
        JUP_SWAP,
        json={
            "quoteResponse": quote,
            "userPublicKey": str(kp.pubkey()),
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": fee_field,
        },
        timeout=20,
    )
    return sr.json() if sr.content else {}


def _sign(raw_tx: str, kp) -> tuple[str, str]:
    """(base64 wire tx, its signature). The signature is known BEFORE we
    send, so a send that errors or times out can still be checked on-chain
    instead of being blindly resent."""
    from solders.transaction import VersionedTransaction

    tx = VersionedTransaction.from_bytes(base64.b64decode(raw_tx))
    signed = VersionedTransaction(tx.message, [kp])
    return base64.b64encode(bytes(signed)).decode(), str(signed.signatures[0])


def _swap_send_jito(quote: dict, kp, tip: int, opts: dict | None = None) -> tuple[bool, str]:
    import time as _t

    try:
        swap = _jupiter_swap_tx(quote, kp, {"jitoTipLamports": max(JITO_MIN_TIP, tip)})
    except requests.RequestException as exc:
        return False, f"Jupiter swap failed: {exc}"
    raw_tx = swap.get("swapTransaction")
    if not raw_tx:
        return False, str(swap.get("error") or swap.get("message") or "Jupiter returned no transaction")
    try:
        wire, sig = _sign(raw_tx, kp)
    except Exception as exc:
        return False, f"Signing failed: {exc}"
    # Jito's default limit is ~1 req/s per IP. Resending the IDENTICAL signed
    # tx is always safe (one signature can only land once), so rate limits
    # just back off and resend.
    maybe_sent = False
    last_err = ""
    bundle_id = ""
    for attempt in range(5):
        try:
            resp = requests.post(
                JITO_TX,
                json={"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": [wire, {"encoding": "base64"}]},
                timeout=20,
            )
            body = resp.json() if resp.content else {}
        except Exception as exc:
            maybe_sent, last_err = True, f"Jito send: {exc}"  # may have been accepted
            break
        err = body.get("error") if isinstance(body, dict) else None
        msg = str(err.get("message") if isinstance(err, dict) else err or "")
        if resp.status_code == 429 or "rate" in msg.lower() or "too many" in msg.lower():
            last_err = "Jito rate-limited"
            _t.sleep(1.2 * (attempt + 1))
            continue
        if err:
            if not maybe_sent:
                return False, "Jito: " + msg[:200]
            break
        maybe_sent = True
        try:
            bundle_id = str(resp.headers.get("x-bundle-id") or "")
        except Exception:
            bundle_id = ""
        log.info("jito accepted tx %s bundle=%s tip=%s", sig, bundle_id or "?", max(JITO_MIN_TIP, tip))
        break
    if not maybe_sent:
        return False, (last_err or "Jito accepted nothing.") + " — nothing sent, safe to retry."
    # Give Jito a few seconds alone (MEV-protected). If it hasn't landed by
    # then, broadcast the SAME signed tx through the normal RPC as well: one
    # signature can only ever land once, so this can't double-trade -- it just
    # stops a dropped bundle from costing the user the whole ~40s expiry wait.
    solo = _jito_solo_s()
    if solo > 0:
        end = _t.monotonic() + solo
        while _t.monotonic() < end:
            _t.sleep(1.5)
            state, detail = _status(sig)
            if state == "ok":
                return True, sig
            if state == "err":
                return False, f"Failed on-chain: {detail}\nhttps://solscan.io/tx/{sig}"
        ok_b, err_b = _rpc_broadcast(wire)
        if ok_b:
            log.info("jito tx %s not landed after %ss; same tx also sent via RPC", sig, solo)
            if opts is not None:
                opts["route_used"] = "Jito tip + RPC backup"
        else:
            log.warning("jito tx %s not landed after %ss; RPC backup refused: %s", sig, solo, err_b)
    landed, why = _confirm(sig, swap.get("lastValidBlockHeight"))
    if landed:
        return True, sig
    log.warning("jito tx %s not landed (%s); jito says: %s", sig, why, _jito_bundle_status(bundle_id))
    return False, f"{why}\nhttps://solscan.io/tx/{sig}"


def _jito_solo_s() -> float:
    """Seconds Jito gets alone before the same tx also goes out via RPC.
    JITO_SOLO_S=0 keeps anti-MEV Jito-only (then the expiry fallback)."""
    try:
        return max(0.0, min(30.0, float(os.getenv("JITO_SOLO_S", "6"))))
    except ValueError:
        return 6.0


def _rpc_broadcast(wire: str) -> tuple[bool, str]:
    """Send an already-signed tx through the normal RPC. Preflight stays on:
    a swap that would fail is refused here instead of burning a fee."""
    try:
        r = requests.post(
            _rpc(),
            json={"jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
                  "params": [wire, {"encoding": "base64", "skipPreflight": False, "maxRetries": 5}]},
            timeout=15,
        )
        body = r.json() if r.content else {}
    except Exception as exc:
        return False, str(exc)[:200]
    if body.get("error"):
        err = body["error"]
        return False, str(err.get("message") if isinstance(err, dict) else err)[:300]
    return True, ""


def _swap_send_rpc(quote: dict, kp, base_fee: int) -> tuple[bool, str]:
    bumps = (1.0, 2.0, 3.5)
    last_err = "RPC accepted nothing."
    for attempt, bump in enumerate(bumps):
        try:
            swap = _jupiter_swap_tx(quote, kp, int(base_fee * bump))
        except requests.RequestException as exc:
            last_err = f"Jupiter swap failed: {exc}"
            if attempt < len(bumps) - 1:
                continue
            return False, last_err
        raw_tx = swap.get("swapTransaction")
        if not raw_tx:
            return False, str(swap.get("error") or swap.get("message") or "Jupiter returned no transaction")
        try:
            wire, sig = _sign(raw_tx, kp)
        except Exception as exc:
            return False, f"Signing failed: {exc}"
        try:
            send = requests.post(
                _rpc(),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "sendTransaction",
                    "params": [wire, {"encoding": "base64", "skipPreflight": False}],
                },
                timeout=20,
            )
            body = send.json() if send.content else {}
        except Exception as exc:
            # The RPC may have forwarded it before failing/timing out: check
            # the chain before ANY resend (a blind rebuild could double-trade).
            landed, why = _confirm(sig, swap.get("lastValidBlockHeight"))
            if landed:
                return True, sig
            if landed is False and "Expired" in why and attempt < len(bumps) - 1:
                last_err = f"Broadcast failed ({exc}); tx expired unlanded"
                continue
            return False, f"Broadcast error: {exc}. {why}\nhttps://solscan.io/tx/{sig}"
        if body.get("error"):
            err = body["error"]
            last_err = str(err.get("message") if isinstance(err, dict) else err)
            low = last_err.lower()
            if any(h in low for h in _AMBIGUOUS_HINTS):
                # A timeout / rate-limit reply can come AFTER the node already
                # forwarded the tx: check the chain before any rebuild.
                landed, why = _confirm(sig, swap.get("lastValidBlockHeight"))
                if landed:
                    return True, sig
                if landed is False and "Expired" in why and attempt < len(bumps) - 1:
                    continue
                return False, f"{last_err}. {why}\nhttps://solscan.io/tx/{sig}"
            if attempt < len(bumps) - 1 and any(h in low for h in _RETRYABLE_HINTS):
                # Preflight rejections (stale blockhash etc.): never forwarded.
                log.warning("swap send retryable (attempt %s), bumping priority fee: %s", attempt + 1, last_err)
                continue
            return False, last_err
        if not body.get("result"):
            # No error but no signature: treat as "maybe sent" and verify.
            landed, why = _confirm(sig, swap.get("lastValidBlockHeight"))
            if landed:
                return True, sig
            if landed is False and "Expired" in why and attempt < len(bumps) - 1:
                continue
            return False, f"{why}\nhttps://solscan.io/tx/{sig}"
        landed, why = _confirm(sig, swap.get("lastValidBlockHeight"))
        if landed:
            return True, sig
        if landed is False and "Expired" in why and attempt < len(bumps) - 1:
            log.warning("swap expired unlanded (attempt %s), rebuilding with bumped fee", attempt + 1)
            continue  # provably dropped -> safe to rebuild; a failed tx is NOT retried
        return False, f"{why}\nhttps://solscan.io/tx/{sig}"
    return False, last_err


def _phrase() -> str:
    return " ".join((os.getenv("SIGNER_MNEMONIC") or "").split())


def _raw_key() -> str:
    return (os.getenv("SIGNER_KEY") or "").strip()


def configured() -> bool:
    return bool(_phrase() or _raw_key())


def live_enabled() -> bool:
    raw = os.getenv("LIVE_BUYS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def max_usd() -> float:
    try:
        return max(1.0, min(5000.0, float(os.getenv("SIGNER_MAX_USD", "500"))))
    except ValueError:
        return 500.0


def _rpc() -> str:
    url = (os.getenv("SOLANA_RPC_URL") or "").strip()
    if url:
        return url
    key = (os.getenv("HELIUS_API_KEY") or "").strip()
    if key:
        return f"https://mainnet.helius-rpc.com/?api-key={key}"
    return "https://api.mainnet-beta.solana.com"


def _keypair():
    from solders.keypair import Keypair

    raw = _raw_key()
    if raw:
        try:
            return Keypair.from_base58_string(raw)
        except Exception:
            pass
    phrase = _phrase()
    if phrase:
        return Keypair.from_seed_phrase_and_passphrase(phrase, "")
    raise RuntimeError("Set SIGNER_KEY or SIGNER_MNEMONIC in .env")


def public_sol() -> str:
    return str(_keypair().pubkey())


def status_text() -> str:
    if not configured():
        return "Signer empty. Add SIGNER_KEY on the droplet."
    try:
        addr = public_sol()
    except Exception as exc:
        return f"Signer present but could not derive address.\n{exc}"
    cap = max_usd()
    flag = "ON" if live_enabled() else "OFF (set LIVE_BUYS=1)"
    return (
        "Signer loaded on this box.\n"
        f"Solana: {addr}\n"
        f"Live buys: {flag}\n"
        f"Max per live tap: ${cap:.0f}\n"
        "Solana only. Floor still applies."
    )


def keypair_from_secret(secret: str):
    from solders.keypair import Keypair

    secret = (secret or "").strip()
    try:
        return Keypair.from_base58_string(secret)
    except Exception:
        raw = base64.b64decode(secret)
        return Keypair.from_bytes(raw)


def buy_sol(
    output_mint: str,
    usd: float,
    secret: str | None = None,
    slip_bps: int | None = None,
    user_id: int | None = None,
) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live buys are OFF. Add LIVE_BUYS=1 on the droplet, then restart."
    if not secret and not configured():
        return False, "No signer key on this box."
    mint = (output_mint or "").strip()
    if len(mint) < 32:
        return False, "Need a Solana mint."
    usd = min(max(1.0, float(usd)), max_usd())
    try:
        from solders.transaction import VersionedTransaction
        from price_fetcher import get_price_usd
    except Exception as exc:
        return False, f"Signer deps missing: {exc}"

    try:
        kp = keypair_from_secret(secret) if secret else _keypair()
    except Exception as exc:
        return False, str(exc)

    try:
        sol_px = sol_usd()
    except Exception as exc:
        return False, str(exc)
    lamports = max(10_000, int((usd / sol_px) * 1_000_000_000))

    try:
        qr = requests.get(
            JUP_QUOTE,
            params={
                "inputMint": SOL_MINT,
                "outputMint": mint,
                "amount": str(lamports),
                "slippageBps": str(int(slip_bps if slip_bps is not None else 1000)),
            },
            timeout=15,
        )
        quote = qr.json() if qr.content else {}
    except requests.RequestException as exc:
        return False, f"Jupiter quote failed: {exc}"
    if qr.status_code >= 400 or quote.get("error"):
        return False, str(quote.get("error") or quote.get("message") or qr.text[:180])

    opts = exec_opts(user_id)
    ok, res = _swap_send_with_retry(quote, kp, opts)
    if not ok:
        return False, res
    route = opts.get("route_used") or ("Jito · MEV-protected" if opts["anti_mev"] else "priority fee")
    return True, f"Live SOL buy ~${usd:.2f} · confirmed · {route}\nhttps://solscan.io/tx/{res}"


def _token_raw_balance(mint: str, kp=None) -> int:
    kp = kp or _keypair()
    r = requests.post(
        _rpc(),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "getTokenAccountsByOwner",
            "params": [
                str(kp.pubkey()),
                {"mint": mint},
                {"encoding": "jsonParsed"},
            ],
        },
        timeout=20,
    )
    try:
        data = r.json() if r.content else {}
    except Exception:
        return 0
    total = 0
    for acc in (data.get("result") or {}).get("value") or []:
        info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        amt = (info.get("tokenAmount") or {}).get("amount") or "0"
        try:
            total += int(amt)
        except ValueError:
            pass
    return total


_TOKEN_PROGRAMS = (
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
)


def holdings(secret: str | None = None) -> list[dict]:
    kp = keypair_from_secret(secret) if secret else _keypair()
    return holdings_pub(str(kp.pubkey()))


def holdings_pub(owner: str, strict: bool = False) -> list[dict]:
    """SPL holdings for a PUBLIC address — no key needed. strict=True raises
    on any RPC error response instead of returning a (falsely) empty list;
    exits use it so a 429 can't shrink a position and fire a false stop."""
    out = []
    seen = set()
    for program in _TOKEN_PROGRAMS:
        r = requests.post(
            _rpc(),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "getTokenAccountsByOwner",
                "params": [
                    owner,
                    {"programId": program},
                    {"encoding": "jsonParsed"},
                ],
            },
            timeout=20,
        )
        try:
            data = r.json() if r.content else {}
        except Exception as exc:
            raise RuntimeError(f"RPC holdings failed: {exc}") from exc
        if strict and (r.status_code >= 400 or data.get("error") or not isinstance(data.get("result"), dict)):
            raise RuntimeError(f"RPC holdings error: {str(data.get('error') or r.status_code)[:120]}")
        for acc in (data.get("result") or {}).get("value") or []:
            info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
            tok = info.get("tokenAmount") or {}
            mint = (info.get("mint") or "").strip()
            try:
                ui = float(tok.get("uiAmount") or 0)
            except (TypeError, ValueError):
                ui = 0.0
            if mint and ui > 0 and mint not in seen:
                seen.add(mint)
                out.append({"mint": mint, "amount": ui})
    return out


def sol_balance_lamports(addr: str) -> int:
    r = requests.post(
        _rpc(),
        json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [addr]},
        timeout=15,
    )
    data = r.json() if r.content else {}
    return int(((data.get("result") or {}).get("value")) or 0)


def holdings_text(secret: str | None = None) -> str:
    try:
        kp = keypair_from_secret(secret) if secret else _keypair()
        addr = str(kp.pubkey())
        rows = holdings(secret)
        lamports = sol_balance_lamports(addr)
    except Exception as exc:
        return f"Could not read bag.\n{exc}"
    lines = [
        f"Live bag on {addr}",
        f"SOL {lamports / 1_000_000_000:.6f}",
        f"{len(rows)} token account(s)",
    ]
    if not rows:
        lines.append("No SPL tokens. Only SOL, or the last buy never landed.")
        return "\n".join(lines)
    for i, row in enumerate(rows[:12], 1):
        lines.append(f"{i}. {row['amount']:g}")
        lines.append(f"   {row['mint']}")
    lines.append("\n/livesell <mint>  sells that bag to SOL")
    return "\n".join(lines)


def send_sol(dest: str, secret: str | None = None, lamports: int | None = None) -> tuple[bool, str]:
    dest = (dest or "").strip()
    if len(dest) < 32:
        return False, "Need a Solana address."
    try:
        from solders.hash import Hash
        from solders.instruction import Instruction
        from solders.message import Message
        from solders.pubkey import Pubkey
        from solders.system_program import TransferParams, transfer
        from solders.transaction import Transaction
    except Exception as exc:
        return False, str(exc)
    kp = keypair_from_secret(secret) if secret else _keypair()
    to = Pubkey.from_string(dest)
    bag = sol_balance_lamports(str(kp.pubkey()))
    if lamports is None:
        send_amt = bag - 5000
    else:
        send_amt = int(lamports)
    if send_amt <= 0 or send_amt + 5000 > bag:
        return False, "Not enough SOL to send (need rent + fee)."
    bh = requests.post(
        _rpc(),
        json={"jsonrpc": "2.0", "id": 1, "method": "getLatestBlockhash", "params": [{"commitment": "finalized"}]},
        timeout=15,
    ).json()
    blockhash = ((bh.get("result") or {}).get("value") or {}).get("blockhash")
    if not blockhash:
        return False, "No blockhash."
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=to, lamports=send_amt))
    msg = Message.new_with_blockhash([ix], kp.pubkey(), Hash.from_string(blockhash))
    tx = Transaction.new_unsigned(msg)
    tx.sign([kp], Hash.from_string(blockhash))
    raw = bytes(tx).hex()
    body = requests.post(
        _rpc(),
        json={"jsonrpc": "2.0", "id": 1, "method": "sendTransaction", "params": [raw, {"encoding": "hex"}]},
        timeout=20,
    ).json()
    if body.get("error"):
        return False, str(body["error"])
    sig = body.get("result") or ""
    return True, f"Collected {send_amt / 1e9:.6f} SOL\nhttps://solscan.io/tx/{sig}"


def sell_sol(
    input_mint: str,
    secret: str | None = None,
    pct: int = 100,
    slip_bps: int | None = None,
    user_id: int | None = None,
) -> tuple[bool, str]:
    if not live_enabled():
        return False, "Live sells are OFF. Add LIVE_BUYS=1 and restart."
    if not secret and not configured():
        return False, "No signer key on this box."
    mint = (input_mint or "").strip()
    if len(mint) < 32:
        return False, "Need a Solana mint to sell."
    try:
        from solders.transaction import VersionedTransaction
    except Exception as exc:
        return False, f"Signer deps missing: {exc}"
    try:
        kp = keypair_from_secret(secret) if secret else _keypair()
        raw_amt = _token_raw_balance(mint, kp)
    except Exception as exc:
        return False, str(exc)
    pct = max(1, min(100, int(pct)))
    raw_amt = raw_amt * pct // 100
    if raw_amt <= 0:
        return False, "Wallet holds 0 of that token. Nothing to sell."
    try:
        qr = requests.get(
            JUP_QUOTE,
            params={
                "inputMint": mint,
                "outputMint": SOL_MINT,
                "amount": str(raw_amt),
                "slippageBps": str(int(slip_bps if slip_bps is not None else 1000)),
            },
            timeout=15,
        )
        quote = qr.json() if qr.content else {}
    except requests.RequestException as exc:
        return False, f"Jupiter quote failed: {exc}"
    if qr.status_code >= 400 or quote.get("error"):
        return False, str(quote.get("error") or quote.get("message") or qr.text[:180])
    opts = exec_opts(user_id)
    ok, res = _swap_send_with_retry(quote, kp, opts)
    if not ok:
        return False, res
    bag_note = "full bag" if pct >= 100 else f"{pct}% of bag"
    route = opts.get("route_used") or ("Jito · MEV-protected" if opts["anti_mev"] else "priority fee")
    return True, f"Live SOL sell ({bag_note}) · confirmed · {route}\nhttps://solscan.io/tx/{res}"
