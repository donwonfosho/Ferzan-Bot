"""Ferzan flywheel: once a day, claim Ferzan's Solana trading fees, buy FERZAN with 30%, burn it, send the rest
to the treasury, and post the receipt. Plan mode (default) only reports what it would do.
Settings (in /opt/ferzan/.env): FLYWHEEL_LIVE=1 to act for real, FLYWHEEL_SHARE_BPS (3000 = 30%),
FLYWHEEL_MAX_BUY_SOL (2), FLYWHEEL_OFF=1 to stop it.   Usage: ferzan_flywheel.py [run|status]"""
import calendar, json, os, subprocess, sys, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for f in ("/opt/ferzan/.env", str(HERE / ".env")):
    if os.path.isfile(f):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
os.chdir(HERE); sys.path.insert(0, str(HERE))
import requests  # noqa: E402

LAUNCH_AT = calendar.timegm((2026, 10, 15, 20, 0, 0))
STATE = Path("/opt/ferzan/dbc-keys/flywheel-state.json")
RPC = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
LIVE = os.environ.get("FLYWHEEL_LIVE") == "1"
env_f = lambda k, d: float(os.environ.get(k) or d)


def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"carry_lamports": 0, "days": {}, "totals": {"claimed_sol": 0, "bought_sol": 0, "burned_raw": "0", "forward_sol": 0}}


def save(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=1)); os.chmod(STATE, 0o600)


def tg(chat: str, text: str) -> None:
    token = os.environ.get("LAUNCHBOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    if not token or not chat:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text[:3500],
                      "disable_web_page_preview": True}, timeout=15)
    except Exception:
        pass


def admins(text: str) -> None:
    for chat in {x.strip() for x in ((os.environ.get("FERZAN_ADMIN_IDS") or "") + "," + (os.environ.get("ADMIN_TELEGRAM_ID") or "")).split(",") if x.strip()}:
        tg(chat, text)
    print(text)


def run() -> int:
    if os.environ.get("FLYWHEEL_OFF") == "1":
        print("flywheel is switched off (FLYWHEEL_OFF=1)"); return 0
    s = load()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if LIVE and s["days"].get(today, {}).get("live"):
        print("already ran live today"); return 0
    now = time.time()
    payload = {"mode": "live" if LIVE else "plan", "rpc": RPC,
               "treasury": (os.environ.get("PLATFORM_TREASURY_SOL") or os.environ.get("TREASURY_SOL") or "").strip(),
               "buyShareBps": int(env_f("FLYWHEEL_SHARE_BPS", 3000)), "reserveLamports": 20_000_000,
               "minBuyLamports": 10_000_000, "maxBuyLamports": int(env_f("FLYWHEEL_MAX_BUY_SOL", 2) * 1e9),
               "carryLamports": int(s.get("carry_lamports") or 0), "skipBuy": now < LAUNCH_AT + 3600}
    p = subprocess.run(["node", str(HERE / "dbc" / "flywheel.mjs")], cwd=str(HERE / "dbc"), input=json.dumps(payload),
                       capture_output=True, text=True, timeout=900)
    lines = (p.stdout or "").strip().splitlines()
    try:
        r = json.loads(lines[-1]) if lines else {}
    except ValueError:
        r = {}
    if not r.get("ok"):
        admins(f"Flywheel {'LIVE' if LIVE else 'plan'} run FAILED: {r.get('error') or (p.stderr or '')[-300:]}\nNothing more was done today.")
        return 1
    if LIVE:
        s["carry_lamports"] = int(r.get("carry_lamports") or 0)
        t = s["totals"]
        t["claimed_sol"] += r.get("claimed_sol", 0); t["bought_sol"] += r.get("bought_sol", 0); t["forward_sol"] += r.get("forward_sol", 0)
        t["burned_raw"] = str(int(t.get("burned_raw") or 0) + int(r.get("burned_raw") or 0))
    s["days"][today] = {"live": LIVE, **{k: r.get(k) for k in ("claimed_sol", "bought_sol", "burned_raw", "forward_sol", "claim_sigs", "buy_sig", "burn_sig", "forward_sig")}}
    s["days"] = dict(sorted(s["days"].items())[-120:])
    save(s)
    burned = int(r.get("burned_raw") or 0) / 1e6
    head = "🔥 Ferzan flywheel" if LIVE else "Ferzan flywheel (PLAN ONLY, nothing sent)"
    msg = [head, f"Fees claimed: {r.get('claimed_sol', 0):.4f} SOL", f"Bought FERZAN with: {r.get('bought_sol', 0):.4f} SOL",
           f"Burned: {burned:,.0f} FERZAN", f"To treasury: {r.get('forward_sol', 0):.4f} SOL"]
    if r.get("burn_sig"):
        msg.append(f"Burn tx: https://solscan.io/tx/{r['burn_sig']}")
    if r.get("buy_sig"):
        msg.append(f"Buy tx: https://solscan.io/tx/{r['buy_sig']}")
    notes = r.get("steps") or []
    admins("\n".join(msg + ([""] + notes if notes else []) + [f"Keeper: {r.get('keeper')} ({r.get('keeper_sol_start', 0):.4f} SOL)"]))
    if LIVE and (r.get("burn_sig") or r.get("buy_sig")):  # public receipt only when something happened
        public = "\n".join(msg)
        import ferzan_media as fm
        pic = fm.img("burn.jpg")
        groups = [g.strip() for g in (os.environ.get("PROMO_GROUPS") or "@Ferzan_Trade_Ecosystem,@Ferzan_Chat").split(",") if g.strip()]
        for chat in [os.environ.get("FERZAN_LAUNCHES_CHANNEL") or ""] + groups:
            fm.tg_photo(chat, public, pic)
        try:
            xt = f"🔥 Ferzan flywheel: {r.get('bought_sol', 0):.3f} SOL of fees bought $FERZAN today and {burned:,.0f} FERZAN were burned."
            if r.get("burn_sig"):
                xt += f"\nBurn: https://solscan.io/tx/{r['burn_sig']}"
            ok, info = fm.x_post(xt + "\n\n#Solana #buyback", pic)
            if not ok and info != "no X keys":
                admins(f"Flywheel X post failed: {info}")
        except Exception as e:
            print("X post skipped:", e)
    return 0


if __name__ == "__main__":
    if (sys.argv[1] if len(sys.argv) > 1 else "run") == "status":
        print(json.dumps(load(), indent=1)); sys.exit(0)
    sys.exit(run())
