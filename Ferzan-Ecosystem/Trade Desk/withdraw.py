"""Send funds OUT of a user's Ferzan wallet: SOL, SPL tokens, EVM native,
ERC-20, TON. Every function is blocking - call via the bot's _off() so the
per-user trade lock serializes it with trades.

Return shape everywhere: (ok, message) where ok is True (confirmed on
chain), False (definitely not sent / failed on chain) or None (sent, not
confirmed yet - the message says to check the link before retrying).
"""

from __future__ import annotations

import base64
import re
import time

import requests

import signer

LAMPORTS = 1_000_000_000
SOL_RENT_MIN = 890_880  # rent-exempt minimum for a 0-byte system account
ATA_RENT = 2_039_280  # rent for a new SPL token account
ATA_RENT_2022 = 2_500_000  # Token-2022 accounts carry extensions: budget more
SIG_FEE = 5_000
CU_PRICE_MICRO = 200_000  # priority fee: 200k micro-lamports per CU
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW7xmW7vM75xpcbhPPYt"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
TON_RESERVE_NANO = 30_000_000  # 0.03 TON left behind on a "send all"

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


# ----------------------------------------------------------- validation ----
def family_of(chain: str) -> str:
    c = (chain or "").lower()
    if c in {"sol", "solana"}:
        return "sol"
    if c == "ton":
        return "ton"
    return "evm"


def validate_address(family: str, addr: str) -> tuple[bool, str]:
    """(ok, normalized address or error text)."""
    addr = (addr or "").strip()
    if family == "sol":
        try:
            from solders.pubkey import Pubkey

            return True, str(Pubkey.from_string(addr))
        except Exception:
            return False, "That isn't a valid Solana address."
    if family == "evm":
        if not _EVM_RE.match(addr):
            return False, "That isn't a valid 0x address (0x + 40 hex characters)."
        body = addr[2:]
        if body != body.lower() and body != body.upper():
            try:
                from eth_utils import is_checksum_address

                if not is_checksum_address(addr):
                    return False, "Address checksum doesn't match — it may have a typo. Copy it again."
            except ImportError:
                pass
        return True, addr
    if family == "ton":
        try:
            from pytoniq_core import Address

            Address(addr)
            return True, addr
        except Exception:
            return False, "That isn't a valid TON address."
    return False, "Unknown network."


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if len(addr or "") > 12 else (addr or "")


# ------------------------------------------------------------------ SOL ----
def _sol_rpc(method: str, params: list, timeout: float = 15) -> dict:
    r = requests.post(
        signer._rpc(), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout
    )
    body = r.json() if r.content else {}
    if body.get("error"):
        raise RuntimeError(str(body["error"].get("message") if isinstance(body["error"], dict) else body["error"]))
    return body.get("result")


def _budget_ixs(cu_limit: int):
    from solders.instruction import Instruction
    from solders.pubkey import Pubkey

    prog = Pubkey.from_string(COMPUTE_BUDGET)
    return [
        Instruction(prog, bytes([2]) + int(cu_limit).to_bytes(4, "little"), []),
        Instruction(prog, bytes([3]) + int(CU_PRICE_MICRO).to_bytes(8, "little"), []),
    ]


def _priority_lamports(cu_limit: int) -> int:
    return -(-cu_limit * CU_PRICE_MICRO // 1_000_000)  # ceil


def _send_sol_tx(kp, ixs: list) -> tuple[bool | None, str]:
    from solders.hash import Hash
    from solders.message import Message
    from solders.transaction import Transaction

    bh = _sol_rpc("getLatestBlockhash", [{"commitment": "confirmed"}]) or {}
    val = bh.get("value") or {}
    blockhash, lvh = val.get("blockhash"), val.get("lastValidBlockHeight")
    if not blockhash:
        return False, "Couldn't get a recent blockhash — nothing sent. Try again."
    msg = Message.new_with_blockhash(ixs, kp.pubkey(), Hash.from_string(blockhash))
    tx = Transaction.new_unsigned(msg)
    tx.sign([kp], Hash.from_string(blockhash))
    sig = str(tx.signatures[0])
    wire = base64.b64encode(bytes(tx)).decode()
    try:
        _sol_rpc(
            "sendTransaction",
            [wire, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 5}],
            timeout=20,
        )
    except requests.RequestException:
        pass  # ambiguous: may have been forwarded -> confirm below decides
    except RuntimeError as exc:
        return False, f"Rejected before sending: {exc}"
    ok, why = signer._confirm(sig, lvh, timeout_s=75)
    link = f"https://solscan.io/tx/{sig}"
    if ok:
        return True, link
    if ok is False:
        return False, f"{why}\n{link}"
    return None, f"{why}\n{link}"


def sol_balance(owner: str) -> int:
    return signer.sol_balance_lamports(owner)


def send_sol(secret: str, dest: str, lamports: int | None) -> tuple[bool | None, str]:
    """lamports=None sends everything (balance minus the network fee)."""
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer

    kp = signer.keypair_from_secret(secret)
    me = str(kp.pubkey())
    if dest == me:
        return False, "That's this wallet's own address."
    cu = 800
    fee = SIG_FEE + _priority_lamports(cu)
    bal = sol_balance(me)
    amt = bal - fee if lamports is None else int(lamports)
    if amt <= 0:
        return False, f"Not enough SOL (balance {bal / LAMPORTS:.6f})."
    left = bal - amt - fee
    if left < 0:
        return False, f"Not enough SOL: sending {amt / LAMPORTS:.6f} + fee needs more than {bal / LAMPORTS:.6f}."
    if 0 < left < SOL_RENT_MIN:
        return False, (
            f"Solana needs at least {SOL_RENT_MIN / LAMPORTS:.5f} SOL left behind (or send 100%). "
            "Pick a smaller amount or send all."
        )
    if amt < SOL_RENT_MIN and sol_balance(dest) == 0:
        return False, f"A brand-new address needs at least {SOL_RENT_MIN / LAMPORTS:.5f} SOL."
    ixs = _budget_ixs(cu) + [
        transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Pubkey.from_string(dest), lamports=amt))
    ]
    ok, info = _send_sol_tx(kp, ixs)
    head = f"Sent {amt / LAMPORTS:.6f} SOL → {short(dest)}" if ok else f"{amt / LAMPORTS:.6f} SOL → {short(dest)}"
    return ok, f"{head}\n{info}"


def spl_holding(owner: str, mint: str) -> dict:
    """{'program', 'decimals', 'account', 'raw'} for the owner's biggest
    token account of `mint` (raw=0 if none)."""
    mi = _sol_rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}]) or {}
    val = mi.get("value") or {}
    program = val.get("owner") or ""
    if program not in {TOKEN_PROGRAM, TOKEN_2022}:
        raise RuntimeError("Not an SPL token mint.")
    decimals = int(((((val.get("data") or {}).get("parsed") or {}).get("info")) or {}).get("decimals") or 0)
    res = _sol_rpc("getTokenAccountsByOwner", [owner, {"mint": mint}, {"encoding": "jsonParsed"}]) or {}
    best, best_raw = "", 0
    for acc in res.get("value") or []:
        info = (((acc.get("account") or {}).get("data") or {}).get("parsed") or {}).get("info") or {}
        raw = int((info.get("tokenAmount") or {}).get("amount") or 0)
        if raw > best_raw:
            best, best_raw = acc.get("pubkey") or "", raw
    return {"program": program, "decimals": decimals, "account": best, "raw": best_raw}


def _ata(owner, mint, program):
    from solders.pubkey import Pubkey

    return Pubkey.find_program_address(
        [bytes(owner), bytes(program), bytes(mint)], Pubkey.from_string(ATA_PROGRAM)
    )[0]


def send_spl(secret: str, mint: str, dest: str, raw: int | None) -> tuple[bool | None, str]:
    """raw=None sends the whole balance of the biggest token account."""
    from solders.instruction import AccountMeta, Instruction
    from solders.pubkey import Pubkey

    kp = signer.keypair_from_secret(secret)
    me = str(kp.pubkey())
    if dest == me:
        return False, "That's this wallet's own address."
    hold = spl_holding(me, mint)
    if hold["raw"] <= 0:
        return False, "No balance of that token in this wallet."
    amt = hold["raw"] if raw is None else int(raw)
    if amt <= 0 or amt > hold["raw"]:
        return False, "Amount is more than this wallet holds."
    dest_info = (_sol_rpc("getAccountInfo", [dest, {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}]) or {}).get("value")
    if dest_info and dest_info.get("owner") in {TOKEN_PROGRAM, TOKEN_2022}:
        return False, "That's a token account, not a wallet. Paste the recipient's wallet address."
    program = Pubkey.from_string(hold["program"])
    mint_pk = Pubkey.from_string(mint)
    dest_pk = Pubkey.from_string(dest)
    dest_ata = _ata(dest_pk, mint_pk, program)
    ata_exists = bool((_sol_rpc("getAccountInfo", [str(dest_ata), {"encoding": "base64", "dataSlice": {"offset": 0, "length": 0}}]) or {}).get("value"))
    cu = 60_000
    rent = ATA_RENT_2022 if hold["program"] == TOKEN_2022 else ATA_RENT
    need = SIG_FEE + _priority_lamports(cu) + (0 if ata_exists else rent)
    if sol_balance(me) < need:
        return False, f"Need {need / LAMPORTS:.5f} SOL in this wallet for the network fee" + (
            " and the recipient's token account." if not ata_exists else "."
        )
    create = Instruction(
        Pubkey.from_string(ATA_PROGRAM),
        bytes([1]),  # CreateIdempotent: no-op if it already exists
        [
            AccountMeta(kp.pubkey(), True, True),
            AccountMeta(dest_ata, False, True),
            AccountMeta(dest_pk, False, False),
            AccountMeta(mint_pk, False, False),
            AccountMeta(Pubkey.from_string(SYSTEM_PROGRAM), False, False),
            AccountMeta(program, False, False),
        ],
    )
    xfer = Instruction(
        program,
        bytes([12]) + amt.to_bytes(8, "little") + bytes([hold["decimals"]]),  # TransferChecked
        [
            AccountMeta(Pubkey.from_string(hold["account"]), False, True),
            AccountMeta(mint_pk, False, False),
            AccountMeta(dest_ata, False, True),
            AccountMeta(kp.pubkey(), True, False),
        ],
    )
    ok, info = _send_sol_tx(kp, _budget_ixs(cu) + [create, xfer])
    ui = amt / (10 ** hold["decimals"])
    head = f"Sent {ui:,.6g} tokens → {short(dest)}" if ok else f"{ui:,.6g} tokens → {short(dest)}"
    return ok, f"{head}\n{info}"


# ------------------------------------------------------------------ EVM ----
L2_RESERVE_WEI = {"base": 5 * 10**12, "arb": 5 * 10**12, "op": 5 * 10**12, "hood": 5 * 10**12}
# Arbitrum-style chains price L1 data into gas units: a plain transfer needs
# far more than 21000, so always estimate there.
ESTIMATE_GAS_CHAINS = {"arb", "hood"}


def _addr_link(meta: dict, addr: str) -> str:
    tpl = meta.get("explorer_addr") or ""
    return tpl.format(addr=addr) if tpl else addr


def _send_ambiguous(meta: dict, addr: str, head: str, exc: Exception) -> tuple[None, str]:
    return None, (
        f"{head}: the network didn't answer cleanly ({str(exc)[:100]}). It MAY have gone out — "
        f"check {_addr_link(meta, addr)} before trying again."
    )


def _evm_acct(key_hex: str):
    from eth_account import Account

    raw = (key_hex or "").replace("0x", "").replace("0X", "")
    return Account.from_key("0x" + raw)


def _evm_meta(chain: str) -> tuple[str, dict]:
    import evm_signer
    from chains import CHAINS, resolve_chain

    cid = resolve_chain(chain) or chain
    meta = CHAINS.get(cid)
    if not meta or not meta.get("rpc") or not meta.get("chain_id"):
        raise RuntimeError(f"Withdrawals aren't set up for {chain}.")
    _ = evm_signer  # imported for side effects/tests
    return cid, meta


def _wait_receipt(rpc: str, txh: str, timeout_s: float = 90) -> bool | None:
    import evm_signer

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(3)
        try:
            rec = (evm_signer._rpc(rpc, "eth_getTransactionReceipt", [txh]) or {}).get("result")
        except Exception:
            continue
        if rec:
            return int(rec.get("status") or "0x0", 16) == 1
    return None


def _txh_from(link: str) -> str:
    m = re.search(r"(0x[0-9a-fA-F]{64})", link or "")
    return m.group(1) if m else ""


def evm_native_balance(chain: str, owner: str) -> int:
    import evm_signer

    _cid, meta = _evm_meta(chain)
    return int((evm_signer._rpc(meta["rpc"], "eth_getBalance", [owner, "latest"]) or {}).get("result") or "0x0", 16)


def send_evm_native(key_hex: str, chain: str, dest: str, wei: int | None) -> tuple[bool | None, str]:
    import evm_signer

    cid, meta = _evm_meta(chain)
    acct = _evm_acct(key_hex)
    if dest.lower() == acct.address.lower():
        return False, "That's this wallet's own address."
    rpc = meta["rpc"]
    bal = int((evm_signer._rpc(rpc, "eth_getBalance", [acct.address, "latest"]) or {}).get("result") or "0x0", 16)
    code = (evm_signer._rpc(rpc, "eth_getCode", [dest, "latest"]) or {}).get("result") or "0x"
    plain = code in ("0x", "0x0", "") and cid not in ESTIMATE_GAS_CHAINS
    gas = 21_000 if plain else evm_signer._estimate_gas(rpc, acct.address, dest, "0x", 1)
    gas_price = int(evm_signer._gas_price(rpc) * 1.2)  # what _broadcast signs with first
    fee = gas * gas_price + L2_RESERVE_WEI.get(cid, 0)
    value = bal - fee if wei is None else int(wei)
    sym = meta.get("native") or "ETH"
    if value <= 0 or value + fee > bal:
        return False, f"Not enough {sym} (balance {bal / 1e18:.6f}, fee ≈ {fee / 1e18:.6f})."
    head = f"{value / 1e18:.6f} {sym} → {short(dest)}"
    try:
        ok, link = evm_signer._broadcast(acct, meta, dest, "0x", value, gas_limit=gas)
    except Exception as exc:  # a timeout here doesn't prove nothing was sent
        return _send_ambiguous(meta, acct.address, head, exc)
    if not ok:
        return False, f"Not sent: {link}"
    done = _wait_receipt(rpc, _txh_from(link))
    if done:
        return True, f"Sent {head}\n{link}"
    if done is False:
        return False, f"Failed on-chain: {head}\n{link}"
    return None, f"Sent, not confirmed yet: {head}\n{link}"


def erc20_holding(chain: str, token: str, owner: str) -> tuple[int, int, str]:
    """(raw balance, decimals, symbol)."""
    import evm_signer

    _cid, meta = _evm_meta(chain)
    rpc = meta["rpc"]
    raw = evm_signer._erc20_balance(rpc, token, owner)
    try:
        d = (evm_signer._rpc(rpc, "eth_call", [{"to": token, "data": "0x313ce567"}, "latest"]) or {}).get("result")
        decimals = int(d, 16) if d and d != "0x" else 18
    except Exception:
        decimals = 18
    sym = ""
    try:
        s = (evm_signer._rpc(rpc, "eth_call", [{"to": token, "data": "0x95d89b41"}, "latest"]) or {}).get("result") or ""
        blob = bytes.fromhex(s[2:]) if s.startswith("0x") else b""
        if len(blob) >= 96:
            n = int.from_bytes(blob[32:64], "big")
            sym = blob[64 : 64 + n].decode("utf-8", "ignore")
        elif blob:
            sym = blob.rstrip(b"\x00").decode("utf-8", "ignore")
    except Exception:
        sym = ""
    return int(raw), max(0, min(36, decimals)), sym[:12]


def send_erc20(key_hex: str, chain: str, token: str, dest: str, raw: int | None) -> tuple[bool | None, str]:
    import evm_signer

    cid, meta = _evm_meta(chain)
    acct = _evm_acct(key_hex)
    if dest.lower() == acct.address.lower():
        return False, "That's this wallet's own address."
    rpc = meta["rpc"]
    bal, decimals, sym = erc20_holding(cid, token, acct.address)
    if bal <= 0:
        return False, "No balance of that token in this wallet."
    amt = bal if raw is None else int(raw)
    if amt <= 0 or amt > bal:
        return False, "Amount is more than this wallet holds."
    data = "0xa9059cbb" + dest[2:].lower().zfill(64) + hex(amt)[2:].zfill(64)
    gas = evm_signer._estimate_gas(rpc, acct.address, token, data, 0)
    need = gas * int(evm_signer._gas_price(rpc) * 1.2) + L2_RESERVE_WEI.get(cid, 0)
    native = int((evm_signer._rpc(rpc, "eth_getBalance", [acct.address, "latest"]) or {}).get("result") or "0x0", 16)
    if native < need:
        return False, f"Need about {need / 1e18:.6f} {meta.get('native') or 'ETH'} for gas."
    head = f"{amt / 10 ** decimals:,.6g} {sym or 'tokens'} → {short(dest)}"
    try:
        ok, link = evm_signer._broadcast(acct, meta, token, data, 0, gas_limit=gas)
    except Exception as exc:
        return _send_ambiguous(meta, acct.address, head, exc)
    if not ok:
        return False, f"Not sent: {link}"
    done = _wait_receipt(rpc, _txh_from(link))
    if done:
        return True, f"Sent {head}\n{link}"
    if done is False:
        return False, f"Failed on-chain: {head}\n{link}"
    return None, f"Sent, not confirmed yet: {head}\n{link}"


# ------------------------------------------------------------------ TON ----
def send_ton(secret: str, dest: str, nano: int | None) -> tuple[bool | None, str]:
    import ton_signer

    return ton_signer.send_ton_native(secret, dest, nano, reserve_nano=TON_RESERVE_NANO)
