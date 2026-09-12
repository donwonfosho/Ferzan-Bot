"""Hot-wallet helper for THIS droplet only.

SIGNER_KEY (preferred) or SIGNER_MNEMONIC in .env.
Live sends stay off until LIVE_BUYS=1.
Capped by SIGNER_MAX_USD (default 10).
"""

from __future__ import annotations

import base64
import os

import requests

SOL_MINT = "So11111111111111111111111111111111111111112"
JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
JUP_SWAP = "https://lite-api.jup.ag/swap/v1/swap"


def _phrase() -> str:
    return " ".join((os.getenv("SIGNER_MNEMONIC") or "").split())


def _raw_key() -> str:
    return (os.getenv("SIGNER_KEY") or "").strip()


def configured() -> bool:
    return bool(_phrase() or _raw_key())


def live_enabled() -> bool:
    return os.getenv("LIVE_BUYS", "").strip().lower() in {"1", "true", "yes", "on"}


def max_usd() -> float:
    try:
        return max(1.0, min(50.0, float(os.getenv("SIGNER_MAX_USD", "10"))))
    except ValueError:
        return 10.0


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


def buy_sol(output_mint: str, usd: float, secret: str | None = None) -> tuple[bool, str]:
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
        sol_px = float(get_price_usd("solana") or 100)
    except Exception:
        sol_px = 100.0
    lamports = max(10_000, int((usd / max(sol_px, 1e-9)) * 1_000_000_000))

    try:
        qr = requests.get(
            JUP_QUOTE,
            params={
                "inputMint": SOL_MINT,
                "outputMint": mint,
                "amount": str(lamports),
                "slippageBps": "150",
            },
            timeout=15,
        )
        quote = qr.json() if qr.content else {}
    except requests.RequestException as exc:
        return False, f"Jupiter quote failed: {exc}"
    if qr.status_code >= 400 or quote.get("error"):
        return False, str(quote.get("error") or quote.get("message") or qr.text[:180])

    try:
        sr = requests.post(
            JUP_SWAP,
            json={
                "quoteResponse": quote,
                "userPublicKey": str(kp.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            },
            timeout=20,
        )
        swap = sr.json() if sr.content else {}
    except requests.RequestException as exc:
        return False, f"Jupiter swap failed: {exc}"
    raw_tx = swap.get("swapTransaction")
    if not raw_tx:
        return False, str(swap.get("error") or swap.get("message") or "Jupiter returned no transaction")

    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(raw_tx))
        signed = VersionedTransaction(tx.message, [kp])
        wire = base64.b64encode(bytes(signed)).decode()
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
        return False, f"Broadcast failed: {exc}"
    if body.get("error"):
        err = body["error"]
        return False, str(err.get("message") if isinstance(err, dict) else err)
    sig = body.get("result") or ""
    if not sig:
        return False, "RPC accepted nothing."
    return True, f"Live SOL buy ~${usd:.2f}\nhttps://solscan.io/tx/{sig}"


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
                    str(kp.pubkey()),
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


def sell_sol(input_mint: str, secret: str | None = None, pct: int = 100) -> tuple[bool, str]:
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
                "slippageBps": "200",
            },
            timeout=15,
        )
        quote = qr.json() if qr.content else {}
    except requests.RequestException as exc:
        return False, f"Jupiter quote failed: {exc}"
    if qr.status_code >= 400 or quote.get("error"):
        return False, str(quote.get("error") or quote.get("message") or qr.text[:180])
    try:
        sr = requests.post(
            JUP_SWAP,
            json={
                "quoteResponse": quote,
                "userPublicKey": str(kp.pubkey()),
                "wrapAndUnwrapSol": True,
                "dynamicComputeUnitLimit": True,
                "prioritizationFeeLamports": "auto",
            },
            timeout=20,
        )
        swap = sr.json() if sr.content else {}
    except requests.RequestException as exc:
        return False, f"Jupiter swap failed: {exc}"
    raw_tx = swap.get("swapTransaction")
    if not raw_tx:
        return False, str(swap.get("error") or swap.get("message") or "Jupiter returned no transaction")
    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(raw_tx))
        signed = VersionedTransaction(tx.message, [kp])
        wire = base64.b64encode(bytes(signed)).decode()
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
        return False, f"Broadcast failed: {exc}"
    if body.get("error"):
        err = body["error"]
        return False, str(err.get("message") if isinstance(err, dict) else err)
    sig = body.get("result") or ""
    if not sig:
        return False, "RPC accepted nothing."
    return True, f"Live SOL sell (full bag)\nhttps://solscan.io/tx/{sig}"
