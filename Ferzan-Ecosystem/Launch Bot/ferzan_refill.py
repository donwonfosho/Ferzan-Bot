"""Ferzan gas tank: tops up the automatic wallets from one small hot wallet, so they never stall.

The tank is a wallet on the droplet (keys in /opt/ferzan/dbc-keys/gas-tank.json) that you fill by hand with a
limited amount. The treasury is never touched. Every 30 minutes (ferzan-refill.timer):
  * Flywheel wallet    below 0.01 SOL  -> topped up to 0.03 SOL
  * FERZAN launcher    below 0.10 SOL  -> topped up to 0.12 SOL (until the Oct 9 launch only)
  * Tron keeper        below 260 TRX while a Tron curve is 80%+ full -> topped up to 300 TRX
Destinations are fixed in this file; nothing else can receive from the tank. Daily caps: 0.2 SOL and 400 TRX.
The admins get a DM for every top-up, and when the tank itself runs low.

  ferzan_refill.py create   make the tank (prints its two public addresses; the keys never leave the file)
  ferzan_refill.py plan     show what it would send now, send nothing
  ferzan_refill.py run      top up what needs it (REFILL_OFF=1 in /opt/ferzan/.env stops it)
"""
import calendar, json, os, secrets, sqlite3, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for f in ("/opt/ferzan/.env", str(HERE / ".env")):
    if os.path.isfile(f):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
sys.path.insert(0, str(HERE.parent / "Trade Desk"))
import requests  # noqa: E402

TANK = Path("/opt/ferzan/dbc-keys/gas-tank.json")
STATE = Path("/opt/ferzan/ops/refill_state.json")
SOL_RPC = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
INDEX_DB = os.environ.get("CURVE_INDEX_DB") or "/opt/ferzan/app/launch/curve_index.db"
FERZAN_LAUNCH_AT = calendar.timegm((2026, 10, 9, 23, 0, 0))
DAY_CAP = {"sol": 0.2, "tron": 400.0}
TANK_LOW = {"sol": 0.1, "tron": 300.0}
# (chain, address, refill below, refill up to, label)
TARGETS = [
    ("sol", "J8yrufechFRtMfCz3MRdv4w1TTkv1eHqjiub3t3v6317", 0.01, 0.03, "Flywheel wallet"),
    ("sol", "95Mu227mZ7cFjaRULv966B3erithUZHVEhSFUZ7YR8Yo", 0.10, 0.12, "FERZAN launcher"),
    ("tron", "TNtgqHLXQdLHevwNt14mzoTPuFqbPkaKRz", 260.0, 300.0, "Tron graduation keeper"),
]


def admins(text: str) -> None:
    token = os.environ.get("LAUNCHBOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    ids = {x.strip() for x in ((os.environ.get("FERZAN_ADMIN_IDS") or "") + "," + (os.environ.get("ADMIN_TELEGRAM_ID") or "")).split(",") if x.strip()}
    for chat in ids:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:3500], "disable_web_page_preview": True}, timeout=15)
        except Exception:
            pass
    print(text)


def sol_rpc(method: str, params: list) -> dict:
    r = requests.post(SOL_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20).json()
    if "error" in r:
        raise RuntimeError(str(r["error"])[:160])
    return r.get("result")


def sol_balance(addr: str) -> float:
    return int((sol_rpc("getBalance", [addr]) or {}).get("value") or 0) / 1e9


def trx_balance(addr: str) -> float:
    import tron_signer as ts
    return ts._trx_balance(ts._to_hex(addr)) / 1e6


def tank() -> dict:
    return json.loads(TANK.read_text())


# ------------------------------------------------------------------ create --
def create() -> int:
    if TANK.exists():
        t = tank()
        print(f"Gas tank already exists.\n  SOL: {t['sol_address']}\n  TRX: {t['tron_address']}")
        return 0
    from solders.keypair import Keypair
    import tron_signer as ts

    kp = Keypair()
    tron_key = secrets.token_hex(32)
    tron_addr, _ = ts.evm_key_to_tron(tron_key)
    TANK.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        TANK.write_text(json.dumps({"sol_address": str(kp.pubkey()), "sol_secret": bytes(kp).hex(),
                                    "tron_address": tron_addr, "tron_key": tron_key, "created": int(time.time())}))
    finally:
        os.umask(old)
    os.chmod(TANK, 0o600)
    print(f"Gas tank created (keys saved root-only in {TANK}, included in the nightly + offsite backups).\n"
          f"  SOL: {kp.pubkey()}\n  TRX: {tron_addr}")
    return 0


# ------------------------------------------------------------------- sends --
def send_sol(to: str, amount: float) -> str:
    from solders.hash import Hash
    from solders.keypair import Keypair
    from solders.message import Message
    from solders.pubkey import Pubkey
    from solders.system_program import TransferParams, transfer
    from solders.transaction import Transaction

    kp = Keypair.from_bytes(bytes.fromhex(tank()["sol_secret"]))
    lamports = int(round(amount * 1e9))
    ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=Pubkey.from_string(to), lamports=lamports))
    bh = Hash.from_string(sol_rpc("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]["blockhash"])
    tx = Transaction([kp], Message.new_with_blockhash([ix], kp.pubkey(), bh), bh)
    import base64
    sig = sol_rpc("sendTransaction", [base64.b64encode(bytes(tx)).decode(), {"encoding": "base64"}])
    for _ in range(20):
        time.sleep(2)
        st = (sol_rpc("getSignatureStatuses", [[sig]]) or {}).get("value") or [None]
        if st[0] and st[0].get("confirmationStatus") in ("confirmed", "finalized"):
            if st[0].get("err"):
                raise RuntimeError(f"failed on-chain: {st[0]['err']}")
            return f"https://solscan.io/tx/{sig}"
    raise RuntimeError(f"not confirmed yet, check https://solscan.io/tx/{sig}")


def send_trx(to: str, amount: float) -> str:
    import tron_signer as ts

    t = tank()
    built = ts._post("/wallet/createtransaction", {"owner_address": ts._to_hex(t["tron_address"]),
                                                   "to_address": ts._to_hex(to), "amount": int(round(amount * 1e6))})
    result, link, _b = ts._send_and_wait(built, t["tron_key"])
    if result not in ("SUCCESS", "unconfirmed") or not link:
        raise RuntimeError(result)
    return link


# --------------------------------------------------------------------- run --
def tron_curve_near_full() -> str:
    try:
        c = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True, timeout=10)
        r = c.execute("SELECT symbol, CAST(real_eth AS REAL) / MAX(CAST(grad_target AS REAL), 1) AS p FROM curves "
                      "WHERE chain = 'tron' AND graduated = 0 ORDER BY p DESC LIMIT 1").fetchone()
        c.close()
    except sqlite3.Error:
        return ""
    return f"${r[0]} is {r[1] * 100:.0f}% full" if r and r[1] >= 0.8 else ""


def run(live: bool) -> int:
    if os.environ.get("REFILL_OFF") == "1":
        print("refill is off (REFILL_OFF=1)")
        return 0
    if not TANK.exists():
        print("no gas tank yet: run 'ferzan_refill.py create'")
        return 1
    t = tank()
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    today = time.strftime("%Y-%m-%d", time.gmtime())
    used = st.setdefault("used", {}).setdefault(today, {"sol": 0.0, "tron": 0.0})
    st["used"] = {k: v for k, v in st["used"].items() if k >= time.strftime("%Y-%m-%d", time.gmtime(time.time() - 7 * 86400))}
    tank_bal = {"sol": sol_balance(t["sol_address"]), "tron": trx_balance(t["tron_address"])}
    lines = []
    for chain, addr, below, upto, label in TARGETS:
        if "launcher" in label.lower() and time.time() > FERZAN_LAUNCH_AT:
            continue
        why = ""
        if chain == "tron":
            why = tron_curve_near_full()
            if not why:
                continue
        bal = sol_balance(addr) if chain == "sol" else trx_balance(addr)
        if bal >= below:
            continue
        unit = "SOL" if chain == "sol" else "TRX"
        need = round(upto - bal, 6 if chain == "sol" else 2)
        room = DAY_CAP[chain] - used[chain]
        fee_spare = 0.00001 if chain == "sol" else 1.2  # network fee paid by the tank
        amount = min(need, room, max(0.0, tank_bal[chain] - fee_spare))
        if amount <= (0.001 if chain == "sol" else 1):
            why_not = "daily cap reached" if room < need else "the gas tank is empty"
            lines.append(f"⚠️ {label} needs {need:g} {unit} ({bal:.4g} now) but {why_not}.")
            continue
        if not live:
            lines.append(f"PLAN: would send {amount:g} {unit} to {label} ({bal:.4g} {unit} now){' — ' + why if why else ''}")
            continue
        try:
            link = send_sol(addr, amount) if chain == "sol" else send_trx(addr, amount)
            used[chain] += amount
            tank_bal[chain] -= amount
            lines.append(f"⛽ Topped up {label}: +{amount:g} {unit} (was {bal:.4g}){' — ' + why if why else ''}\n{link}")
        except Exception as e:
            lines.append(f"⚠️ Top-up of {label} failed: {str(e)[:160]}. Nothing more sent this round.")
            break
    low = [f"{v:.4g} {'SOL' if k == 'sol' else 'TRX'}" for k, v in tank_bal.items() if v < TANK_LOW[k]]
    if low and live and time.time() - float(st.get("low_note", 0)) > 12 * 3600:
        st["low_note"] = time.time()
        lines.append(f"🪫 Gas tank is running low ({', '.join(low)}). Refill:\n  SOL → {t['sol_address']}\n  TRX → {t['tron_address']}")
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st))
    if lines:
        (admins if live else print)("\n\n".join(lines))
    else:
        print(f"nothing to do (tank {tank_bal['sol']:.4f} SOL, {tank_bal['tron']:.2f} TRX)")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if cmd == "create":
        sys.exit(create())
    sys.exit(run(live=cmd == "run"))
