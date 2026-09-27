"""Ferzan auto-posting, run every 10 minutes by a timer. Decides what is due and posts each thing once:
  - FERZAN countdown: 7 days, 3 days, 1 day, 6 hours, 1 hour and 10 minutes before launch
  - daily recap (23:30 UTC): new launches and the top coins by 24h volume
  - 14 rotating feature promos, every 8 hours (X copies carry hashtags and the $FERZAN cashtag where relevant)
Where: the @Ferzan_Launches channel and X get everything; the groups in PROMO_GROUPS
(default @Ferzan_Trade_Ecosystem and @Ferzan_Chat) get the countdown and the recap, not the promos. Preview mode (default) sends everything to the admins only;
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
import ferzan_media as fm  # noqa: E402

LAUNCH_AT = calendar.timegm((2026, 10, 9, 23, 0, 0))
STATE = Path("/opt/ferzan/promo-state.json")
LIVE = os.environ.get("PROMO_LIVE") == "1"
SITE = "https://ferzan-factory.com"
API = "http://127.0.0.1:8000/api"
COUNTDOWN = [(7 * 86400, "7 days"), (3 * 86400, "3 days"), (86400, "24 hours"), (6 * 3600, "6 hours"), (3600, "1 hour"), (600, "10 minutes")]
COUNTDOWN_IMG = {7 * 86400: "countdown_7d.jpg", 3 * 86400: "countdown_3d.jpg", 86400: "countdown_24h.jpg", 6 * 3600: "countdown_6h.jpg", 3600: "countdown_1h.jpg", 600: "countdown_10m.jpg"}
# (Telegram text, X text, X hashtags). X gets its own shorter copy; hashtags only go on X.
PROMOS = [
    (f"🚀 Launch a coin in a minute on Ferzan Factory: Solana, Base, BNB, Ethereum and Robinhood Chain. Sign in with email, Google or X; your wallet is built in.\n{SITE}/launch",
     f"🚀 Launch a coin in a minute on Solana, Base, BNB, Ethereum or Robinhood Chain. Sign in with email, Google or X. Wallet built in.\n{SITE}/launch",
     "#memecoin #Solana"),
    ("🤖 Prefer Telegram? @Ferzan_Launch_Bot launches your coin from chat, on the same curves as the website.\nhttps://t.me/Ferzan_Launch_Bot",
     "🤖 Launch a coin without leaving Telegram. @Ferzan_Launch_Bot puts it on the same curves as the website.\nhttps://t.me/Ferzan_Launch_Bot",
     "#Telegram #crypto"),
    (f"📈 Every Ferzan coin has its own chart, market cap and on-site trading. No redirects, no extra apps.\n{SITE}",
     f"📈 Every Ferzan coin gets its own chart, market cap and on-site trading. No redirects, no extra apps.\n{SITE}",
     "#DeFi #memecoins"),
    ("⚡ @Ferzan_Trade_Bot: buy and sell Ferzan coins and more straight from Telegram.\nhttps://t.me/Ferzan_Trade_Bot",
     "⚡ Buy and sell straight from Telegram with @Ferzan_Trade_Bot.\nhttps://t.me/Ferzan_Trade_Bot",
     "#TradingBot #Solana"),
    ("🔔 Add @Ferzan_Buy_Bot to your group for live buy alerts, market cap and trending.\nhttps://t.me/Ferzan_Buy_Bot",
     "🔔 Live buy alerts, market cap and trending for your Telegram group: add @Ferzan_Buy_Bot.\nhttps://t.me/Ferzan_Buy_Bot",
     "#crypto #Telegram"),
    ("🛡️ @Ferzan_Guardian_Bot keeps Telegram groups clean: spam, raids and scam links handled for you.\nhttps://t.me/Ferzan_Guardian_Bot",
     "🛡️ Spam, raids and scam links handled for your Telegram group. Meet @Ferzan_Guardian_Bot.\nhttps://t.me/Ferzan_Guardian_Bot",
     "#CryptoSecurity #Web3"),
    (f"🔥 FERZAN: every day part of Ferzan's platform fees buys FERZAN on the open market and burns it. Receipts are posted with the transaction links.\n{SITE}/ferzan",
     f"🔥 Every day, 30% of Ferzan's Solana fees buy $FERZAN on the open market and burn it. Receipts posted with tx links.\n{SITE}/ferzan",
     "#buyback #Solana"),
    (f"🎓 Ferzan curves graduate into locked liquidity. Watch coins climb on the King of the Hill board.\n{SITE}",
     f"🎓 Ferzan curves graduate into locked liquidity. Watch coins climb the King of the Hill board.\n{SITE}",
     "#memecoin #DeFi"),
    (f"💸 Launch it, earn from it. On Base, BNB, Ethereum and Robinhood Chain, half of the 1% trading fee goes straight to the creator's wallet on every trade. Nothing to claim.\n{SITE}/launch",
     f"💸 Launch it, earn from it. Creators get half of the 1% fee on every trade, paid straight to their wallet on Base, BNB, Ethereum and Robinhood Chain.\n{SITE}/launch",
     "#Base #BNBChain"),
    (f"🟣 Solana launches run on Meteora bonding curves. Your share of the trading fees builds up and you claim it in one tap from Rewards.\n{SITE}/account",
     f"🟣 Solana coins on Ferzan run on Meteora curves. Creator fees build up; claim them in one tap.\n{SITE}",
     "#Solana #Meteora"),
    (f"🤝 Refer traders and earn: when a trade names your wallet as the referrer, 10% of its fee is yours. Get your link with /refer in @Ferzan_Launch_Bot.\n{SITE}",
     f"🤝 Refer traders, earn 10% of their fees. Get your link with /refer in @Ferzan_Launch_Bot.\n{SITE}",
     "#crypto #referral"),
    (f"👛 One account, every chain. Your Ferzan portfolio shows what you hold, what you launched and what you earned, in one place.\n{SITE}/account",
     f"👛 One account, every chain: holdings, launches and earnings in one Ferzan portfolio.\n{SITE}",
     "#Web3 #crypto"),
    (f"🎯 FERZAN is built against snipers: the fee starts at 99% and falls to 1% over the first 30 minutes, and 650M of the supply is locked in a multisig by Meteora.\n{SITE}/ferzan",
     f"🎯 $FERZAN is built against snipers: a 99% fee at open that falls to 1% over 30 minutes, and 650M supply locked by Meteora.\n{SITE}/ferzan",
     "#Solana #FairLaunch"),
    ("💬 Questions, ideas, alpha? The Ferzan community is here.\nhttps://t.me/Ferzan_Chat",
     "💬 Questions, ideas, alpha? Join the Ferzan community on Telegram.\nhttps://t.me/Ferzan_Chat",
     "#cryptocommunity #Web3"),
]
GENERAL_TAGS = ["#crypto", "#altcoins", "#Web3", "#cryptocurrency", "#DeFi"]


def x_len(text: str) -> int:
    """X counts every link as 23 characters."""
    return sum(23 if w.startswith("http") else len(w) for w in text.split(" ")) + text.count(" ")


def with_tags(body: str, tags: str, extra: str = "") -> str:
    tag_list = tags.split() + ([extra] if extra and extra not in tags.split() else [])
    out = body + "\n\n" + " ".join(tag_list)
    return out if x_len(out) <= 280 else body + "\n\n" + " ".join(tags.split())


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


def _admin_ids() -> set:
    return {x.strip() for x in ((os.environ.get("FERZAN_ADMIN_IDS") or "") + "," + (os.environ.get("ADMIN_TELEGRAM_ID") or "")).split(",") if x.strip()}


def admins(text: str) -> None:
    for chat in _admin_ids():
        tg(chat, text)


GROUPS = [g.strip() for g in (os.environ.get("PROMO_GROUPS") or "@Ferzan_Trade_Ecosystem,@Ferzan_Chat").split(",") if g.strip()]


def post(s: dict, key: str, text: str, x_text: str | None = None, groups: bool = False, image: str = "") -> None:
    """Posts once per key, with its graphic when promo_img/<image> exists. Preview mode sends it to the admins only."""
    if key in s["done"]:
        return
    s["done"].append(key)
    pic = fm.img(image) if image else None
    if not LIVE:
        where = "channel + X" + (" + " + ", ".join(GROUPS) if groups else "")
        for chat in _admin_ids():
            fm.tg_photo(chat, f"PREVIEW (would go to {where}):\n\n" + text, pic)
        print("preview:", key); return
    fm.tg_photo(os.environ.get("FERZAN_LAUNCHES_CHANNEL") or "", text, pic)
    if groups:
        for g in GROUPS:
            if not fm.tg_photo(g, text, pic):
                admins(f"Could not post in {g}: add @Ferzan_Launch_Bot to it (admin in a channel, member in a group).")
    cap = int(os.environ.get("PROMO_X_PER_DAY") or 4)
    if x_text is not None and len(s["x_log"]) < cap:
        try:
            ok, info = fm.x_post(x_text[:280], pic)
            if ok:
                s["x_log"].append(time.time())
            elif info != "no X keys":
                admins(f"X post failed for {key}: {info}")
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
            post(s, f"countdown:{secs}", text, with_tags(f"⏳ $FERZAN launches in {label}: {et}. Anti-sniper fee at open, 650M locked. Contract address only from @ferzaneco and {SITE}/ferzan at launch.", "#Solana #Meteora"), groups=True, image=COUNTDOWN_IMG.get(secs, ""))


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
    post(s, f"recap:{day}", text, with_tags(f"📊 Ferzan today: {n_new} new launches." + (f" Top: {tops[0].split(' · ')[0][3:]}" if tops else "") + f" {SITE}", "#memecoin #crypto"), groups=True, image="recap.jpg")


def promo(s: dict, now: float) -> None:
    if now - float(s.get("last_promo") or 0) < 8 * 3600:
        return
    if LAUNCH_AT - 3 * 3600 < now < LAUNCH_AT + 3 * 3600:
        return  # keep launch hours clear
    i = int(s.get("promo_i") or 0) % len(PROMOS)
    tg_text, x_body, tags = PROMOS[i]
    n = int(s.get("promo_n") or 0)  # rotating extra tag keeps repeat cycles from being identical (X rejects duplicates)
    post(s, f"promo:{int(now // (8 * 3600))}", tg_text, with_tags(x_body, tags, GENERAL_TAGS[n % len(GENERAL_TAGS)]), image=f"promo_{i + 1:02d}.jpg")
    s["promo_n"] = n + 1
    s["promo_i"] = i + 1; s["last_promo"] = now


if __name__ == "__main__":
    if os.environ.get("PROMO_OFF") == "1":
        print("promos are off (PROMO_OFF=1)"); sys.exit(0)
    s = load(); now = time.time()
    countdown(s, now); recap(s, now); promo(s, now)
    save(s)
