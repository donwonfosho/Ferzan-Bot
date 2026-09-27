"""Ferzan auto-posting, run every 10 minutes by a timer. Decides what is due and posts each thing once:
  - FERZAN countdown: 7 days, 3 days, 1 day, 6 hours, 1 hour and 10 minutes before launch
  - daily recap (23:30 UTC): new launches and the top coins by 24h volume
  - rotating promos for the site and each bot, every 8 hours
Where: the @Ferzan_Launches channel and X. Preview mode (default) sends everything to the admins only;
set PROMO_LIVE=1 to post publicly, PROMO_OFF=1 to stop. X posts are capped by PROMO_X_PER_DAY (default 4)."""
import calendar, json, os, sys, time
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

LAUNCH_AT = calendar.timegm((2026, 10, 9, 23, 0, 0))
STATE = Path("/opt/ferzan/promo-state.json")
LIVE = os.environ.get("PROMO_LIVE") == "1"
SITE = "https://ferzan-factory.com"
API = "http://127.0.0.1:8000/api"
COUNTDOWN = [(7 * 86400, "7 days"), (3 * 86400, "3 days"), (86400, "24 hours"), (6 * 3600, "6 hours"), (3600, "1 hour"), (600, "10 minutes")]
PROMOS = [
    f"🚀 Launch a coin in a minute on Ferzan Factory: Solana, Base, BNB, Ethereum and Robinhood Chain. Sign in with email, Google or X; your wallet is built in.\n{SITE}/launch",
    "🤖 Prefer Telegram? @Ferzan_Launch_Bot launches your coin from chat, on the same curves as the website.\nhttps://t.me/Ferzan_Launch_Bot",
    f"📈 Every Ferzan coin has its own chart, market cap and on-site trading. No redirects, no extra apps.\n{SITE}",
    "⚡ @Ferzan_Trade_Bot: buy and sell Ferzan coins and more straight from Telegram.\nhttps://t.me/Ferzan_Trade_Bot",
    "🔔 Add @Ferzan_Buy_Bot to your group for live buy alerts, market cap and trending.\nhttps://t.me/Ferzan_Buy_Bot",
    "🛡️ @Ferzan_Guardian_Bot keeps Telegram groups clean: spam, raids and scam links handled for you.\nhttps://t.me/Ferzan_Guardian_Bot",
    f"🔥 FERZAN: every day part of Ferzan's platform fees buys FERZAN on the open market and burns it. Receipts are posted with the transaction links.\n{SITE}/ferzan",
    f"🎓 Ferzan curves graduate into locked liquidity. Watch coins climb on the King of the Hill board.\n{SITE}",
]


def load() -> dict:
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {"done": [], "x_log": [], "promo_i": 0, "last_promo": 0}


def save(s: dict) -> None:
    s["done"] = s["done"][-500:]; s["x_log"] = [t for t in s["x_log"] if t > time.time() - 86400]
    STATE.write_text(json.dumps(s))


def tg(chat: str, text: str) -> bool:
    token = os.environ.get("LAUNCHBOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    if not token or not chat:
        return False
    try:
        return requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text[:4000],
                             "disable_web_page_preview": False}, timeout=15).status_code == 200
    except Exception:
        return False


def admins(text: str) -> None:
    for chat in {x.strip() for x in ((os.environ.get("FERZAN_ADMIN_IDS") or "") + "," + (os.environ.get("ADMIN_TELEGRAM_ID") or "")).split(",") if x.strip()}:
        tg(chat, text)


def post(s: dict, key: str, text: str, x_text: str | None = None) -> None:
    """Posts once per key. Preview mode sends it to the admins only."""
    if key in s["done"]:
        return
    s["done"].append(key)
    if not LIVE:
        admins("PREVIEW (not posted publicly):\n\n" + text); print("preview:", key); return
    tg(os.environ.get("FERZAN_LAUNCHES_CHANNEL") or "", text)
    cap = int(os.environ.get("PROMO_X_PER_DAY") or 4)
    if x_text is not None and len(s["x_log"]) < cap:
        try:
            import x_poster
            keys = x_poster.keys_from_env()
            if keys:
                ok, info = x_poster.tweet(keys, x_text[:280])
                if ok:
                    s["x_log"].append(time.time())
        except Exception as e:
            print("X skipped:", e)
    print("posted:", key)


def countdown(s: dict, now: float) -> None:
    for secs, label in COUNTDOWN:
        due = LAUNCH_AT - secs
        if due <= now < due + 1800:  # within 30 minutes of the moment; never a stale post
            et = "Friday Oct 9, 7:00 PM Eastern"
            text = (f"⏳ FERZAN launches in {label} — {et}.\n\nThe launch is automatic. The fee starts at 99% and falls to 1% over 30 minutes, "
                    f"so sniping the open costs almost everything. 650M of the supply is locked in a multisig by Meteora.\n\n"
                    f"The contract address is posted here and at {SITE}/ferzan the moment it goes live. Anything posted before that is not FERZAN.")
            post(s, f"countdown:{secs}", text, f"⏳ FERZAN launches in {label}: {et}. Contract address only from @ferzaneco and {SITE}/ferzan at launch.")


def recap(s: dict, now: float) -> None:
    day = time.strftime("%Y-%m-%d", time.gmtime(now))
    if time.gmtime(now).tm_hour != 23 or time.gmtime(now).tm_min < 30 or f"recap:{day}" in s["done"]:
        return
    try:
        new = requests.get(f"{API}/launches", params={"sort": "new", "limit": 60}, timeout=20).json().get("items", [])
        vol = requests.get(f"{API}/launches", params={"sort": "volume", "limit": 3}, timeout=20).json().get("items", [])
    except Exception as e:
        print("recap skipped:", e); return
    since = now - 86400
    n_new = sum(1 for i in new if (i.get("launched_ts") or 0) > since)
    tops = [f"{k + 1}. ${i.get('symbol')} · ${float(i.get('vol24_usd') or 0):,.0f} volume · ${float(i.get('mcap_usd') or 0):,.0f} mcap"
            for k, i in enumerate(vol) if float(i.get("vol24_usd") or 0) > 0]
    if n_new == 0 and not tops:
        s["done"].append(f"recap:{day}"); return  # nothing worth posting
    text = "\n".join([f"📊 Ferzan today", f"New launches: {n_new}"] + (["Top by 24h volume:"] + tops if tops else []) + [f"\n{SITE}"])
    post(s, f"recap:{day}", text, f"📊 Ferzan today: {n_new} new launches." + (f" Top: {tops[0].split(' · ')[0][3:]}" if tops else "") + f" {SITE}")


def promo(s: dict, now: float) -> None:
    if now - float(s.get("last_promo") or 0) < 8 * 3600:
        return
    if LAUNCH_AT - 3 * 3600 < now < LAUNCH_AT + 3 * 3600:
        return  # keep launch hours clear
    i = int(s.get("promo_i") or 0) % len(PROMOS)
    post(s, f"promo:{int(now // (8 * 3600))}", PROMOS[i], PROMOS[i])
    s["promo_i"] = i + 1; s["last_promo"] = now


if __name__ == "__main__":
    if os.environ.get("PROMO_OFF") == "1":
        print("promos are off (PROMO_OFF=1)"); sys.exit(0)
    s = load(); now = time.time()
    countdown(s, now); recap(s, now); promo(s, now)
    save(s)
