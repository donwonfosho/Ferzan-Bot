"""Ferzan watchdog: every few minutes, checks what would hurt users if it quietly broke, and DMs the admins.

  * services   every ferzan-* service that should be running is running, and is not restart-looping
  * wallets    the automatic wallets have enough to do their job (flywheel SOL, FERZAN launcher SOL until the
               launch, Tron graduation keeper TRX when a Tron curve is close to filling)
  * networks   the Solana RPC, TronGrid and the Launch API answer
  * disk       the droplet is not filling up

Read-only: it never signs or sends a transaction. It alerts when something goes bad, repeats every 6 hours while
it stays bad, and says so once when it recovers.   Usage: ferzan_watchdog.py [run|status]
Settings (/opt/ferzan/.env): WATCHDOG_OFF=1, WATCH_WALLETS to override the wallet list
("sol:<addr>:<min>:<label>,tron:<addr>:<min>:<label>").
"""
import calendar, json, os, shutil, sqlite3, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for f in ("/opt/ferzan/.env", str(HERE / ".env")):
    if os.path.isfile(f):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
import requests  # noqa: E402

STATE = Path("/opt/ferzan/app/watchdog-state.json")
FERZAN_LAUNCH_AT = calendar.timegm((2026, 10, 9, 23, 0, 0))
REPEAT_S = 6 * 3600
SOL_RPC = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
TRONGRID = (os.environ.get("TRONGRID_URL") or "https://api.trongrid.io").rstrip("/")
LAUNCH_API = (os.environ.get("LAUNCH_API_URL") or "http://127.0.0.1:8000").rstrip("/")
INDEX_DB = os.environ.get("CURVE_INDEX_DB") or "/opt/ferzan/app/launch/curve_index.db"

# services that must always be up (timers' one-shot services are not listed)
SERVICES = ["ferzan-trade", "ferzan-trade-api", "ferzan-webapp", "ferzan-launch", "ferzan-launch-api",
            "ferzan-buy", "ferzan-guardian", "ferzan-curve-indexer", "ferzan-liq"]
DEFAULT_WALLETS = [
    ("sol", "J8yrufechFRtMfCz3MRdv4w1TTkv1eHqjiub3t3v6317", 0.05, "Flywheel wallet (daily claim, buy + burn)"),
    ("sol", "95Mu227mZ7cFjaRULv966B3erithUZHVEhSFUZ7YR8Yo", 0.08, "FERZAN launcher (creates the pool on Oct 9)"),
    ("tron", "TNtgqHLXQdLHevwNt14mzoTPuFqbPkaKRz", 260.0, "Tron graduation keeper"),
]


def wallets() -> list:
    raw = (os.environ.get("WATCH_WALLETS") or "").strip()
    if not raw:
        return DEFAULT_WALLETS
    out = []
    for part in raw.split(","):
        bits = part.strip().split(":", 3)
        if len(bits) >= 3:
            out.append((bits[0], bits[1], float(bits[2]), bits[3] if len(bits) > 3 else bits[1][:8]))
    return out


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


# ------------------------------------------------------------------ checks --
def check_services(prev: dict) -> dict:
    out = {}
    for s in SERVICES:
        try:
            if subprocess.run(["systemctl", "cat", f"{s}.service"], capture_output=True, timeout=10).returncode != 0:
                continue  # not installed on this droplet
            active = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True, timeout=10).stdout.strip()
            nr = int(subprocess.run(["systemctl", "show", "-p", "NRestarts", "--value", s], capture_output=True,
                                    text=True, timeout=10).stdout.strip() or 0)
        except Exception as e:
            out[f"svc:{s}"] = f"could not check ({type(e).__name__})"
            continue
        last = int((prev.get("restarts") or {}).get(s, nr))
        prev.setdefault("restarts", {})[s] = nr
        if active != "active":
            out[f"svc:{s}"] = f"{s} is {active or 'not running'}"
        elif nr - last >= 3:
            out[f"svc:{s}"] = f"{s} restarted {nr - last} times in the last check window (crash loop?)"
    return out


def sol_balance(addr: str) -> float:
    r = requests.post(SOL_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [addr]}, timeout=15).json()
    return int((r.get("result") or {}).get("value") or 0) / 1e9


def trx_balance(addr: str) -> float:
    h = {"TRON-PRO-API-KEY": os.environ["TRONGRID_API_KEY"]} if os.environ.get("TRONGRID_API_KEY") else {}
    r = requests.post(f"{TRONGRID}/wallet/getaccount", json={"address": addr, "visible": True}, headers=h, timeout=15).json()
    return int(r.get("balance") or 0) / 1e6


def tron_curve_near_full() -> str:
    """'' or the name of a Tron curve at 80%+ of its target (only then does the keeper need its TRX)."""
    try:
        c = sqlite3.connect(f"file:{INDEX_DB}?mode=ro", uri=True, timeout=10)
        r = c.execute("SELECT symbol, CAST(real_eth AS REAL) / MAX(CAST(grad_target AS REAL), 1) AS p FROM curves "
                      "WHERE chain = 'tron' AND graduated = 0 ORDER BY p DESC LIMIT 1").fetchone()
        c.close()
    except sqlite3.Error:
        return ""
    return f"${r[0]} is {r[1] * 100:.0f}% full" if r and r[1] >= 0.8 else ""


def check_wallets() -> dict:
    out = {}
    now = time.time()
    for chain, addr, need, label in wallets():
        if "launcher" in label.lower() and now > FERZAN_LAUNCH_AT + 86400:
            continue  # its job is done once FERZAN is live
        why = ""
        if chain == "tron" and "keeper" in label.lower():
            why = tron_curve_near_full()
            if not why:
                continue  # the keeper only spends TRX when a curve fills
        try:
            bal = sol_balance(addr) if chain == "sol" else trx_balance(addr)
        except Exception as e:
            out[f"bal:{addr}"] = f"{label}: balance read failed ({type(e).__name__})"
            continue
        if bal < need:
            unit = "SOL" if chain == "sol" else "TRX"
            out[f"bal:{addr}"] = (f"{label} is low: {bal:,.4g} {unit}, needs {need:g} {unit}"
                                  + (f" ({why})" if why else "") + f"\nSend {unit} to {addr}")
    return out


def check_networks() -> dict:
    out = {}
    try:
        r = requests.post(SOL_RPC, json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"}, timeout=12).json()
        if r.get("result") != "ok":
            out["net:sol"] = f"Solana RPC unhealthy: {str(r.get('error') or r)[:120]}"
    except Exception as e:
        out["net:sol"] = f"Solana RPC not answering ({type(e).__name__})"
    try:
        h = {"TRON-PRO-API-KEY": os.environ["TRONGRID_API_KEY"]} if os.environ.get("TRONGRID_API_KEY") else {}
        r = requests.post(f"{TRONGRID}/wallet/getnowblock", headers=h, timeout=12)
        if r.status_code != 200:
            out["net:tron"] = f"TronGrid answered HTTP {r.status_code}" + (" (API key limit?)" if r.status_code in (403, 429) else "")
    except Exception as e:
        out["net:tron"] = f"TronGrid not answering ({type(e).__name__})"
    try:
        r = requests.get(f"{LAUNCH_API}/api/launches", params={"limit": 1}, timeout=12)
        if r.status_code != 200:
            out["net:api"] = f"Launch API answered HTTP {r.status_code}"
    except Exception as e:
        out["net:api"] = f"Launch API not answering ({type(e).__name__})"
    return out


def check_disk() -> dict:
    u = shutil.disk_usage("/")
    pct = u.used * 100 / u.total
    return {"disk": f"Disk is {pct:.0f}% full ({u.free / 1e9:.1f} GB free)"} if pct >= 85 else {}


# -------------------------------------------------------------------- main --
def run() -> int:
    if os.environ.get("WATCHDOG_OFF") == "1":
        print("watchdog is off (WATCHDOG_OFF=1)")
        return 0
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    problems = {}
    for fn in (lambda: check_services(st), check_wallets, check_networks, check_disk):
        try:
            problems.update(fn())
        except Exception as e:
            problems[f"check:{getattr(fn, '__name__', 'services')}"] = f"a check crashed: {type(e).__name__}: {str(e)[:100]}"
    open_ = st.get("open") or {}
    now = int(time.time())
    new, repeat, fixed = [], [], []
    for k, msg in problems.items():
        o = open_.get(k)
        if not o:
            new.append(msg)
            open_[k] = {"since": now, "sent": now, "msg": msg}
        elif now - int(o.get("sent") or 0) >= REPEAT_S:
            repeat.append(f"{msg} (since {time.strftime('%b %d %H:%M UTC', time.gmtime(o['since']))})")
            o.update(sent=now, msg=msg)
    for k in [k for k in open_ if k not in problems]:
        fixed.append(open_.pop(k)["msg"].split("\n")[0])
    st.update(open=open_, last_run=now)
    STATE.write_text(json.dumps(st, indent=1))
    parts = []
    if new:
        parts.append("🚨 Ferzan watchdog\n" + "\n\n".join(new))
    if repeat:
        parts.append("⏰ Still broken\n" + "\n\n".join(repeat))
    if fixed:
        parts.append("✅ Recovered\n" + "\n".join(fixed))
    if parts:
        admins("\n\n".join(parts))
    else:
        print(f"all good ({len(SERVICES)} services, {len(wallets())} wallets, 3 networks, disk)")
    return 0


if __name__ == "__main__":
    if (sys.argv[1] if len(sys.argv) > 1 else "run") == "status":
        try:
            print(json.dumps(json.loads(STATE.read_text()), indent=1))
        except Exception:
            print("{}")
        sys.exit(0)
    sys.exit(run())
