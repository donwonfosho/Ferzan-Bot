"""FERZAN flagship launch, run by the droplet itself.
    prepare [--notify]  upload the logo + metadata once, then a full read-only rehearsal (simulates the pool)
    launch              at the launch minute: create the pool, hand the creator to Squads, register + announce
    status              show what has happened so far
Refuses to launch outside [launch time - 1 min, launch time + 2 h]. Safe to re-run: it resumes where it stopped."""
import calendar, hashlib, json, os, subprocess, sys, time
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

LAUNCH_AT = calendar.timegm((2026, 10, 15, 20, 0, 0))  # Thursday Oct 15 2026, 4:00 PM Eastern (20:00 UTC)
NAME, SYMBOL = "Ferzan", "FERZAN"
VAULT = "2vWqwX72ijo24vgvPQW6yBQh2qXE4jrEd18YDdEbWKLG"
LOGO_URL = "https://ferzan-factory.com/brand/ferzan-token.jpg"
LOGO_SHA = "200d514e10a37a27b79378b8a577b2e82b78256af7d5a8192692dd253837074c"
LINKS = {"website": "https://ferzan-factory.com", "x": "https://x.com/ferzaneco", "telegram": "https://t.me/Ferzan_Chat"}
DESC = ("FERZAN powers the Ferzan ecosystem: the Ferzan Factory launchpad and the Ferzan Telegram bots for launching, "
        "trading and tracking coins on Solana, Base, BNB, Ethereum and Robinhood Chain. Every day, part of Ferzan's "
        "platform fees buys FERZAN on the open market and burns it.")
STATE = Path("/opt/ferzan/dbc-keys/ferzan-flagship-state.json")
RPC = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"


def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def save(s: dict) -> None:
    STATE.write_text(json.dumps(s, indent=1)); os.chmod(STATE, 0o600)


def notify(text: str) -> None:
    token = os.environ.get("LAUNCHBOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    ids = {x.strip() for x in ((os.environ.get("FERZAN_ADMIN_IDS") or "") + "," + (os.environ.get("ADMIN_TELEGRAM_ID") or "")).split(",") if x.strip()}
    for chat in ids:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text[:3500],
                          "disable_web_page_preview": True}, timeout=15)
        except Exception:
            pass
    print(text)


def node(mode: str, uri: str) -> dict:
    p = subprocess.run(["node", str(HERE / "dbc" / "ferzan_launch.mjs")], cwd=str(HERE / "dbc"), capture_output=True, text=True, timeout=300,
                       input=json.dumps({"mode": mode, "rpc": RPC, "name": NAME, "symbol": SYMBOL, "uri": uri, "vault": VAULT}))
    out = (p.stdout or "").strip().splitlines()
    try:
        return json.loads(out[-1]) if out else {"ok": False, "error": (p.stderr or "no output")[-300:]}
    except ValueError:
        return {"ok": False, "error": (p.stdout or "")[-300:]}


def ensure_metadata(s: dict) -> dict:
    if s.get("uri"):
        return s
    img = requests.get(LOGO_URL, timeout=30).content
    if hashlib.sha256(img).hexdigest() != LOGO_SHA:
        raise SystemExit("ABORT: the logo on ferzan-factory.com is not the approved FERZAN logo")
    from irys_upload import upload_bytes
    image = LOGO_URL  # the approved logo on ferzan-factory.com (hash checked above); keeps the Irys upload free-size
    meta = {"name": NAME, "symbol": SYMBOL, "description": DESC, "image": image, "external_url": LINKS["website"],
            "extensions": {"website": LINKS["website"], "twitter": LINKS["x"], "telegram": LINKS["telegram"]},
            "properties": {"files": [{"uri": image, "type": "image/jpeg"}], "category": "image"}}
    uri = upload_bytes(json.dumps(meta).encode("utf-8"), tags=[("Content-Type", "application/json")])
    s.update(image=image, uri=uri); save(s)
    return s


def prepare(tell: bool) -> int:
    s = ensure_metadata(load())
    r = node("plan", s["uri"])
    when = time.strftime("%a %b %d %Y %H:%M UTC", time.gmtime(LAUNCH_AT))
    lines = [f"FERZAN rehearsal: {'READY' if r.get('ok') else 'PROBLEM'}", f"Launch: {when} (4:00 PM Eastern)",
             f"Token address (keep private until launch): {r.get('mint')}", f"Pool: {r.get('pool')}",
             f"Launcher: {r.get('launcher')} ({float(r.get('launcher_sol') or 0):.4f} SOL)", f"Vault: {r.get('vault')} - {r.get('vault_check')}",
             f"Pool creation test: {r.get('create_sim') or ('pool already exists' if r.get('pool_exists') else '-')}",
             f"Metadata: {s.get('uri')}"]
    if r.get("error"):
        lines.append(f"Error: {r['error']}")
    if tell:
        notify("\n".join(lines))
    else:
        print("\n".join(lines)); print("Handover function:", r.get("transfer_fn", "")[:200])
    return 0 if r.get("ok") else 1


def launch() -> int:
    now = time.time()
    if now < LAUNCH_AT - 60 or now > LAUNCH_AT + 7200:
        notify(f"FERZAN launch refused: it is not launch time (now {time.strftime('%H:%M UTC', time.gmtime(now))}).")
        return 1
    s = ensure_metadata(load())
    if s.get("announced"):
        print("already launched and announced"); return 0
    plan = node("plan", s["uri"])
    if not plan.get("ok"):
        notify(f"FERZAN launch STOPPED before sending anything: {plan.get('error')}"); return 1
    import launch_bot_db as db
    if not s.get("request_id"):
        req = db.create_launch_request(0, 0, "solana", "meteora", NAME, SYMBOL, str(10**9 * 10**6), 6, DESC, s["image"],
                                       {"source": "site", "site_wallet": plan["launcher"], "flagship": True, **LINKS})
        db.update_status(req.id, "built", wallet_address=plan["launcher"])
        s["request_id"] = req.id; save(s)
    r = node("send", s["uri"])
    s.update({k: r[k] for k in ("mint", "pool", "create_sig", "transfer_sig", "creator_now") if r.get(k)}); save(s)
    if not r.get("ok"):
        notify(f"FERZAN launch PROBLEM: {r.get('error')}\nPool {r.get('pool')} | mint {r.get('mint')}\nRe-run: systemctl start ferzan-flagship"); return 1
    body = {"tx_hash": s.get("create_sig", ""), "result_token_address": s["mint"]}
    for _ in range(3):
        try:
            res = requests.post(f"http://127.0.0.1:8000/api/launch-requests/{s['request_id']}/complete", json=body, timeout=120)
            if res.status_code == 200:
                s["announced"] = True; save(s); break
        except Exception:
            pass
        time.sleep(10)
    if not s.get("live_posted"):  # the public launch post, once: channel, the Ferzan groups and X, with the IS LIVE graphic
        import ferzan_media as fm
        link = f"https://ferzan-factory.com/coin/solana/{s['mint']}"
        pic = fm.img("ferzan_live.jpg")
        text = (f"🚀 FERZAN IS LIVE\n\nCA: {s['mint']}\n\nTrade it on Ferzan Factory: {link}\n\n"
                "The fee starts at 99% and falls to 1% over the first 30 minutes, so buying in the first minutes costs far more. "
                "650M FERZAN are locked to the Ferzan multisig.\n\nThis is the only official contract address.")
        groups = [g.strip() for g in (os.environ.get("PROMO_GROUPS") or "@Ferzan_Trade_Ecosystem,@Ferzan_Chat").split(",") if g.strip()]
        for chat in [os.environ.get("FERZAN_LAUNCHES_CHANNEL") or ""] + groups:
            fm.tg_photo(chat, text, pic)
        try:
            ok, info = fm.x_post(f"🚀 $FERZAN is LIVE on Solana.\n\nCA: {s['mint']}\n\n{link}\n\n#Solana #Meteora", pic)
            if not ok:
                notify(f"FERZAN launch X post failed: {info}")
        except Exception as e:
            print("X post skipped:", e)
        s["live_posted"] = True; save(s)
    notify(f"FERZAN IS LIVE\nCA: {s['mint']}\nPool: {s['pool']}\nCreator now: {s.get('creator_now')} (Squads vault)\n"
           f"Announced: {'yes' if s.get('announced') else 'NO - re-run: systemctl start ferzan-flagship'}\n"
           f"Trade: https://ferzan-factory.com/coin/solana/{s['mint']}")
    return 0 if s.get("announced") else 1


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "prepare":
        sys.exit(prepare("--notify" in sys.argv))
    if cmd == "launch":
        sys.exit(launch())
    s = load()
    print(json.dumps({k: v for k, v in s.items()}, indent=1) if s else "nothing yet")
