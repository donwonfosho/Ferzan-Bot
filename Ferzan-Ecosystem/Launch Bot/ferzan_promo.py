"""Ferzan auto-posting, run every 10 minutes by a timer. Decides what is due and posts each thing once:
  - FERZAN countdown: 14 and 10 days, 7, 5, 3 and 2 days, 24, 12, 6 and 3 hours, 1 hour, 30, 10 and 5 minutes before launch,
    each with its own graphic and a different fact about FERZAN
  - daily recap (23:30 UTC): new launches and the top coins by 24h volume
  - 44 rotating feature promos (some with video), every PROMO_EVERY_HOURS hours (default 4; X copies carry hashtags and the $FERZAN cashtag where relevant)
Where: the @Ferzan_Launches channel and X get everything; the groups in PROMO_GROUPS
(default @Ferzan_Trade_Ecosystem and @Ferzan_Chat) get everything too (PROMO_GROUP_PROMOS=0 keeps promos out of them). Preview mode (default) sends everything to the admins only;
set PROMO_LIVE=1 to post publicly, PROMO_OFF=1 to stop. X posts are capped by PROMO_X_PER_DAY (default 6)."""
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

LAUNCH_AT = calendar.timegm((2026, 10, 15, 20, 0, 0))
STATE = Path("/opt/ferzan/promo-state.json")
LIVE = os.environ.get("PROMO_LIVE") == "1"
SITE = "https://ferzan-factory.com"
API = "http://127.0.0.1:8000/api"
D, H, M = 86400, 3600, 60
COUNTDOWN = [(14 * D, "14 days"), (10 * D, "10 days"), (7 * D, "7 days"), (5 * D, "5 days"), (3 * D, "3 days"), (2 * D, "2 days"),
             (D, "24 hours"), (12 * H, "12 hours"), (6 * H, "6 hours"), (3 * H, "3 hours"), (H, "1 hour"), (30 * M, "30 minutes"),
             (10 * M, "10 minutes"), (5 * M, "5 minutes")]
COUNTDOWN_IMG = {14 * D: "countdown_14d.jpg", 10 * D: "countdown_10d.jpg", 7 * D: "countdown_7d.jpg", 5 * D: "countdown_5d.jpg",
                 3 * D: "countdown_3d.jpg", 2 * D: "countdown_2d.jpg", D: "countdown_24h.jpg", 12 * H: "countdown_12h.jpg",
                 6 * H: "countdown_6h.jpg", 3 * H: "countdown_3h.jpg", H: "countdown_1h.jpg", 30 * M: "countdown_30m.jpg",
                 10 * M: "countdown_10m.jpg", 5 * M: "countdown_5m.jpg"}
# one real fact per countdown post, so each one teaches something (all of it is on the FERZAN page)
COUNTDOWN_FACTS = {
    14 * D: "Supply is fixed at 1,000,000,000 and the mint authority is removed at launch. Nobody can make more.",
    10 * D: "No presale and no dev buy: 28% of the supply is bought on the curve, by anyone, at the same price.",
    7 * D: "Liquidity is locked forever. At graduation the pool's liquidity is locked so it can never be pulled.",
    5 * D: "The team's 3% sits behind a 12-month cliff, then pays out monthly. The lock link will be posted for anyone to check.",
    3 * D: "60% of the supply is locked for 24 months and only starts releasing after graduation, 25M a month, to a 2-of-3 multisig.",
    2 * D: "Every day part of Ferzan's Solana fees buys FERZAN on the open market and burns it, with receipts posted.",
    D: "The fee at open is 99% and falls to the normal 1% over 30 minutes. Patient buyers win.",
    12 * H: "The contract address is posted only here, on X from @ferzaneco and on ferzan-factory.com. Any other address is fake.",
    6 * H: "Launch is automatic, created on-chain by Ferzan's own launcher and handed to the multisig.",
    3 * H: "Get your wallet ready on Solana. Sign in at ferzan-factory.com or use @Ferzan_Trade_Bot.",
    H: "One hour. Fee at open: 99%. Minute 1: 85%. Minute 5: 46%. Minute 30: 1%.",
    30 * M: "30 minutes. Remember: the contract address only comes from @ferzaneco, this channel and ferzan-factory.com/ferzan.",
    10 * M: "10 minutes. The fee starts at 99%. Don't rush the first minutes.",
    5 * M: "5 minutes. Stay in this channel: the contract address drops the moment FERZAN is live.",
}
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
    (f"📊 FERZAN supply, 1,000,000,000 fixed: 28% public curve, 7% graduation liquidity (locked forever), 5% unlocks at graduation (20M rewards, 30M team lock), 60% locked for 24 months at 25M a month. Mint authority removed.\n{SITE}/ferzan",
     f"📊 $FERZAN supply, 1B fixed: 28% public curve, 7% locked liquidity, 5% at graduation, 60% locked 24 months. Mint authority removed.\n{SITE}/ferzan",
     "#tokenomics #Solana"),
    (f"🔒 The team's FERZAN: 3% (30M), split equally across three public wallets. Nothing moves for 12 months, then monthly releases. After graduation the multisig locks it on-chain and posts the link so anyone can check.\n{SITE}/ferzan",
     f"🔒 Team allocation is 3%, equal across three public wallets, 12-month cliff, then monthly. Locked on-chain after graduation, link posted.\n{SITE}/ferzan",
     "#transparency #Solana"),
    (f"🎯 Anti-sniper fee on FERZAN: 99% at open, 85% after 1 minute, 46% at 5, 21% at 10, 5% at 20, and the normal 1% at 30 minutes. Sniping the open costs almost everything.\n{SITE}/ferzan",
     f"🎯 $FERZAN fee: 99% at open, 46% at 5 min, 5% at 20 min, 1% at 30 min. Sniping the open costs almost everything.\n{SITE}/ferzan",
     "#FairLaunch #Solana"),
    (f"🔥 Watch the burn: every day 30% of Ferzan's Solana fees buys FERZAN on the open market and burns it. Every buy and burn is posted with its transaction link.\n{SITE}/transparency",
     f"🔥 Watch the $FERZAN burn: 30% of Ferzan's Solana fees buy and burn daily. Every transaction is public.\n{SITE}/transparency",
     "#burn #Solana"),
    ("😈 New in @Ferzan_Trade_Bot: Degen mode. Paste a CA and it buys, with bigger default sizes, looser slippage, TP +300%, SL -25% and a 20% trailing stop on every buy. Rug Guard stays on, honeypots stay blocked, and one tap restores your old settings.\nhttps://t.me/Ferzan_Trade_Bot",
     "😈 Degen mode in @Ferzan_Trade_Bot: paste a CA and it buys, TP/SL/trail set automatically. Rug Guard on, honeypots blocked, off in one tap.\nhttps://t.me/Ferzan_Trade_Bot",
     "#TradingBot #memecoins"),
    ("🌐 @Ferzan_Trade_Bot trades across 19 chains from one desk: one wallet screen, one token card, one tap to buy.\nhttps://t.me/Ferzan_Trade_Bot",
     "🌐 19 chains, one trading desk in Telegram. @Ferzan_Trade_Bot\nhttps://t.me/Ferzan_Trade_Bot",
     "#multichain #crypto"),
    ("👀 Paste any contract address into @Ferzan_Trade_Bot and see the token card before you buy: safety verdict, liquidity, holder concentration, price impact and what you already hold. One tap to Protect with TP and SL.\nhttps://t.me/Ferzan_Trade_Bot",
     "👀 See it before you buy: paste a CA in @Ferzan_Trade_Bot for safety, liquidity, holders and price impact. One tap Protect.\nhttps://t.me/Ferzan_Trade_Bot",
     "#DYOR #TradingBot"),
    (f"🏆 Top traders and top callers on every Ferzan coin, every chain, ranked weekly. See where you stand.\n{SITE}/compete",
     f"🏆 Weekly leaderboard: top traders and callers on every Ferzan coin, every chain.\n{SITE}/compete",
     "#leaderboard #crypto"),
    (f"🌉 Bridge and buy: move value across chains and buy in one flow, inside @Ferzan_Trade_Bot or on the website.\n{SITE}/bridge",
     f"🌉 Bridge and buy in one flow, in Telegram or on the web.\n{SITE}/bridge",
     "#bridge #DeFi"),
    ("💬 Questions, ideas, alpha? The Ferzan community is here.\nhttps://t.me/Ferzan_Chat",
     "💬 Questions, ideas, alpha? Join the Ferzan community on Telegram.\nhttps://t.me/Ferzan_Chat",
     "#cryptocommunity #Web3"),
    # ---- 24-38: second graphics set, hook-first copy (image N is promo_N.jpg, in this order) ----
    (f"👋 Partnerships, listings, press, ideas? Say hello: Ferzantrade@gmail.com\n{SITE}",
     f"👋 Building something with us? Partnerships, listings, press: Ferzantrade@gmail.com\n{SITE}",
     "#Web3 #crypto"),
    (f"🧭 One board. Coins launched on the website and coins launched from the bot land in the same list, with the same charts and the same trading.\n{SITE}",
     f"🧭 Web launch or bot launch, every Ferzan coin lands on the same board. One list. One place to trade.\n{SITE}",
     "#memecoins #Solana"),
    (f"🏭 The Factory is OPEN. 8 chains live. The bots are live too. Launch a coin in a minute, trade it, watch it climb.\n{SITE}",
     f"🏭 The Factory is OPEN. 8 chains live, bots live too. Launch a coin in a minute on @solana, @base, @BNBCHAIN or @ethereum.\n{SITE}/launch",
     "#memecoin #Web3"),
    (f"🔒 Liquidity stays. On Ferzan the LP is burned, or locked on Solana. Nobody gets to pull it out from under holders.\n{SITE}",
     f"🔒 Liquidity stays. LP is burned, or locked on Solana with @MeteoraAG. Check it on-chain yourself.\n{SITE}",
     "#rugpull #Solana"),
    ("🛡️ Fake contract address in your Telegram group? Deleted. @Ferzan_Guardian_Bot removes unofficial contracts before anyone buys the wrong coin.\nhttps://t.me/Ferzan_Guardian_Bot",
     "🛡️ Fake contract address in your group? Deleted. @Ferzan_Guardian_Bot removes unofficial contracts in Telegram before anyone buys the wrong coin.\nhttps://t.me/Ferzan_Guardian_Bot",
     "#CryptoScam #Telegram"),
    (f"👛 Your own wallet. The Ferzan site is open now: sign in, launch and trade.\n{SITE}",
     f"👛 Your own wallet. The Ferzan site is open now: sign in, launch, trade.\n{SITE}",
     "#Web3 #DeFi"),
    ("⚡ Every coin on the board, one tap away. Trade all of it from Telegram with @Ferzan_Trade_Bot.\nhttps://t.me/Ferzan_Trade_Bot",
     "⚡ Every coin on the Ferzan board, tradable from Telegram. @Ferzan_Trade_Bot\nhttps://t.me/Ferzan_Trade_Bot",
     "#TradingBot #Solana"),
    ("📋 Paste the contract. Pick the chain. Trade it. @Ferzan_Trade_Bot shows safety, liquidity and price impact before you buy.\nhttps://t.me/Ferzan_Trade_Bot",
     "📋 Paste a contract. Pick the chain. Trade it. Safety, liquidity and price impact shown BEFORE you buy. @Ferzan_Trade_Bot\nhttps://t.me/Ferzan_Trade_Bot",
     "#DYOR #TradingBot"),
    (f"🔥 Buyback and burn: 30% of Ferzan's Solana fees buy $FERZAN on the open market and burn it. Every buy and burn is posted with its transaction link.\n{SITE}/transparency",
     f"🔥 30% of Ferzan's Solana fees buy $FERZAN and burn it. Every transaction posted. Nothing hidden.\n{SITE}/transparency",
     "#buyback #burn #Solana"),
    ("🛑 Rug Guard in @Ferzan_Trade_Bot: it sells if liquidity is pulled or the dev dumps, so you don't have to be awake for it.\nhttps://t.me/Ferzan_Trade_Bot",
     "🛑 Rug Guard sells if liquidity is pulled or the dev dumps. You don't have to be awake for it. @Ferzan_Trade_Bot\nhttps://t.me/Ferzan_Trade_Bot",
     "#RugGuard #TradingBot"),
    (f"🧠 Creator score, shown before you buy: the dev's past launches, how many graduated, how much they hold.\n{SITE}",
     f"🧠 Know the dev before you buy. Creator score shows past launches, graduations and dev holdings on every Ferzan coin.\n{SITE}",
     "#DYOR #memecoins"),
    (f"💸 Creators keep half. Every trade pays a 1% fee and half of it goes to the creator.\n{SITE}/launch",
     f"💸 Launch a coin, keep HALF of every trade's 1% fee. Paid straight to the creator's wallet.\n{SITE}/launch",
     "#memecoin #Base #BNBChain"),
    ("🤖 Launch from the bot. @Ferzan_Launch_Bot puts your coin on the same curves as the website, without leaving Telegram.\nhttps://t.me/Ferzan_Launch_Bot",
     "🤖 Launch a coin without leaving Telegram. @Ferzan_Launch_Bot, same curves as the website.\nhttps://t.me/Ferzan_Launch_Bot",
     "#Telegram #memecoin"),
    (f"🌐 Launch on the web. Sign in, launch and trade with your own wallet.\n{SITE}/launch",
     f"🌐 Launch on the web: sign in, launch, trade with your own wallet. No Telegram needed.\n{SITE}/launch",
     "#memecoin #Web3"),
    (f"🌍 Eight chains. One board. Solana, Base, BNB, Ethereum, Robinhood, Arc, Tron and TON.\n{SITE}",
     f"🌍 Eight chains. One board. @solana, @base, @BNBCHAIN, Ethereum, Robinhood Chain, Arc, Tron and @ton_blockchain.\n{SITE}",
     "#multichain #crypto"),
    # ---- 39-44: third set (images 39-43 and the build-drop video 44), each with its own hook ----
    (f"🧭 Two doors, one room. Launch from Telegram with @Ferzan_Launch_Bot or from the website: both land on the same Ferzan board, and @Ferzan_Trade_Bot can trade every coin on it.\n{SITE}",
     f"🧭 Telegram or web, your coin lands on the same board. Launch with @Ferzan_Launch_Bot or the site, trade it with @Ferzan_Trade_Bot.\n{SITE}",
     "#Telegram #memecoin"),
    (f"💰 The fee isn't just the platform's. Every Ferzan trade pays 1%, and half of that 1% belongs to the creator of the coin.\n{SITE}/launch",
     f"💰 Every trade pays a 1% fee. Half of it goes to the coin's creator. Launch yours.\n{SITE}/launch",
     "#creators #memecoin"),
    (f"🏭 Doors open, machines running. Eight chains are live on Ferzan Factory: launch it, trade it, and keep half of the trading fee as the creator.\n{SITE}",
     f"🏭 The factory floor is live: 8 chains, launch it, trade it, keep half the creator fee.\n{SITE}",
     "#Web3 #Solana"),
    (f"🪙 Make a coin, earn from its volume. Creators keep half of the 1% trade fee on Ferzan, so a coin people trade pays the person who launched it.\n{SITE}/launch",
     f"🪙 Your coin, your cut: creators keep half of the 1% trade fee on Ferzan.\n{SITE}/launch",
     "#BuildInPublic #crypto"),
    (f"🚪 Get in. Solana, Base, BNB, Ethereum, Robinhood Chain, Arc, Tron and TON, all on one board, all tradable from Telegram and the web.\n{SITE}",
     f"🚪 Eight chains, one board. Pick yours and get in.\n{SITE}",
     "#multichain #DeFi"),
    (f"🎬 Build drop. Creators keep half the 1% trade fee. 30% of Ferzan's Solana fees buy $FERZAN and burn it. Eight chains, one board. Watch, then get in.\n{SITE}",
     f"🎬 Build drop: creators keep half the fee, 30% of Ferzan's Solana fees buy and burn $FERZAN, eight chains on one board.\n{SITE}",
     "#buyback #burn #Solana"),
    # ---- 45-50: fourth set (six graphics; the date on 49 is skipped automatically after launch) ----
    (f"👑 Creators get paid on every trade. Launch a coin on Ferzan Factory and half of the 1% trading fee is yours on every buy and every sell.\n{SITE}/launch",
     f"👑 Creators get paid on every trade. Launch on Ferzan and keep half of the 1% fee.\n{SITE}/launch",
     "#CreatorEconomy #crypto"),
    (f"🚀 Liftoff on eight chains: Solana, Base, BNB, Ethereum, Robinhood Chain, Arc, Tron and TON. One launchpad, one board, the same charts and trading everywhere.\n{SITE}/launch",
     f"🚀 Eight chains, one launchpad: Solana, Base, BNB, Ethereum, Robinhood, Arc, Tron and TON.\n{SITE}/launch",
     "#Solana #Ethereum"),
    (f"🔁 Launch. Trade. Repeat. Make a coin on the web or with @Ferzan_Launch_Bot, trade it with @Ferzan_Trade_Bot, then do it again on any of eight chains.\n{SITE}",
     f"🔁 Launch. Trade. Repeat. Web or Telegram, eight chains, one board.\n{SITE}",
     "#memecoin #DeFi"),
    (f"🏁 The Factory is open: launch and trade on 8 chains. Pick a chain, name your coin, and it lands on the board with its own chart and market cap.\n{SITE}/launch",
     f"🏁 The Factory is open. Launch and trade on 8 chains.\n{SITE}/launch",
     "#Web3 #memecoin"),
    (f"✅ How it works: 1) Launch on the site or with @Ferzan_Launch_Bot. 2) They trade, on any of 8 chains, all on one board. 3) You get paid on every single trade. FERZAN goes live Thu Oct 15, 4:00 PM ET; the contract is only on the site and @Ferzan_Launches.\n{SITE}",
     f"✅ 1) Launch. 2) They trade, any of 8 chains. 3) You get paid on every trade. $FERZAN: Thu Oct 15, 4 PM ET, contract only on the site and @Ferzan_Launches.\n{SITE}",
     "#memecoin #FairLaunch"),
    (f"📣 Doors open at ferzan-factory.com. Sign in, launch a coin in about a minute and trade across eight chains, from the web or from Telegram.\n{SITE}",
     f"📣 Doors are open. Sign in, launch a coin, trade 8 chains. Web or Telegram.\n{SITE}",
     "#crypto #Solana"),
]
GENERAL_TAGS = ["#crypto", "#altcoins", "#Web3", "#cryptocurrency", "#DeFi"]


# Older promos that a newer one says better (promo number = its image number). PROMO_SKIP overrides, "none" turns skipping off.
DEFAULT_SKIP = "2,4,6,7,9,13,17,24,25,26,29,31,35,37,38,40,41,42,43"
DATED_AFTER_LAUNCH = {49}  # graphic shows the launch date: skipped once FERZAN is live


def skipped(now: float | None = None) -> set:
    raw = (os.environ.get("PROMO_SKIP") if os.environ.get("PROMO_SKIP") is not None else DEFAULT_SKIP).strip().lower()
    if raw in ("", "none"):
        return set()
    out = {int(x) for x in raw.split(",") if x.strip().isdigit()}
    if (time.time() if now is None else now) >= LAUNCH_AT:
        out |= DATED_AFTER_LAUNCH
    return out


def next_promo(pointer: int) -> int:
    """Index of the next promo to post, passing over skipped numbers. Images stay matched because numbers never shift."""
    skip, n = skipped(), len(PROMOS)
    i = pointer % n
    for _ in range(n):
        if (i + 1) not in skip:
            return i
        i = (i + 1) % n
    return pointer % n  # everything skipped: fall back rather than post nothing


def ramp(now: float | None = None) -> bool:
    """Final 3 days before launch: more posts (X cap at least 10/day, a promo at least every 3h). PROMO_RAMP=0 turns it off."""
    now = time.time() if now is None else now
    return os.environ.get("PROMO_RAMP") != "0" and LAUNCH_AT - 3 * 86400 <= now < LAUNCH_AT + 86400


def x_len(text: str) -> int:
    """X counts every link as 23 characters."""
    return sum(23 if w.startswith("http") else len(w) for w in text.split(" ")) + text.count(" ")


def with_tags(body: str, tags: str, extra: str = "") -> str:
    tag_list = tags.split() + ([extra] if extra and extra not in tags.split() else [])
    out = body + "\n\n" + " ".join(tag_list)
    return out if x_len(out) <= 280 else body + "\n\n" + " ".join(tags.split())


import statefile  # noqa: E402


def load() -> dict:
    return statefile.read_json(STATE, lambda: {"done": [], "x_log": [], "promo_i": 0, "last_promo": 0})


def save(s: dict) -> None:
    s["done"] = s["done"][-500:]; s["x_log"] = [t for t in s["x_log"] if t > time.time() - 86400]
    statefile.write_json(STATE, s)


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


def post(s: dict, key: str, text: str, x_text: str | None = None, groups: bool = False, image: str = "", video: str = "") -> None:
    """Posts once per key, with its graphic when promo_img/<image> exists. Preview mode sends it to the admins only."""
    if key in s["done"]:
        return
    s["done"].append(key)
    if LIVE:
        save(s)  # remembered before sending, so a crash never causes a repeat post
    pic = fm.img(image) if image else None
    vfile = fm.vid(video) if video else None
    send = (lambda chat, t: fm.tg_video(chat, t, vfile, pic)) if vfile else (lambda chat, t: fm.tg_photo(chat, t, pic))
    if not LIVE:
        where = "channel + X" + (" + " + ", ".join(GROUPS) if groups else "")
        for chat in _admin_ids():
            send(chat, f"PREVIEW (would go to {where}):\n\n" + text)
        print("preview:", key); return
    send(os.environ.get("FERZAN_LAUNCHES_CHANNEL") or "", text)
    if groups:
        for g in GROUPS:
            if not send(g, text):
                admins(f"Could not post in {g}: add @Ferzan_Launch_Bot to it (admin in a channel, member in a group).")
    cap = int(os.environ.get("PROMO_X_PER_DAY") or 6)
    if ramp():
        cap = max(cap, 10)
    if x_text is not None and len(s["x_log"]) < cap:
        try:
            ok, info = fm.x_post_video(x_text[:280], vfile, pic) if vfile else fm.x_post(x_text[:280], pic)
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
            et = "Thursday Oct 15, 4:00 PM Eastern"
            fact = COUNTDOWN_FACTS.get(secs, "")
            text = (f"⏳ FERZAN launches in {label} — {et}.\n\n{fact}\n\nThe launch is automatic. The fee starts at 99% and falls to 1% over 30 minutes, "
                    f"so sniping the open costs almost everything. 650M of the supply is locked in a multisig by Meteora.\n\n"
                    f"The contract address is posted here and at {SITE}/ferzan the moment it goes live. Anything posted before that is not FERZAN.")
            post(s, f"countdown:{secs}", text, with_tags(f"⏳ $FERZAN launches in {label}: {et}. {fact} Contract address only from @ferzaneco and {SITE}/ferzan.", "#Solana #Meteora"), groups=True, image=COUNTDOWN_IMG.get(secs, ""))


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
    hours = float(os.environ.get("PROMO_EVERY_HOURS") or 4)
    every = (min(hours, 3.0) if ramp(now) else hours) * 3600
    if now - float(s.get("last_promo") or 0) < every - 300:  # 5 minutes of slack so the 10-minute timer never skips a slot
        return
    if LAUNCH_AT - 3 * 3600 < now < LAUNCH_AT + 3 * 3600:
        return  # keep launch hours clear
    i = next_promo(int(s.get("promo_i") or 0))
    tg_text, x_body, tags = PROMOS[i]
    n = int(s.get("promo_n") or 0)  # rotating extra tag keeps repeat cycles from being identical (X rejects duplicates)
    post(s, f"promo:{int(now // every)}", tg_text, with_tags(x_body, tags, GENERAL_TAGS[n % len(GENERAL_TAGS)]),
         groups=os.environ.get("PROMO_GROUP_PROMOS") != "0", image=f"promo_{i + 1:02d}.jpg",
         video=f"promo_{i + 1:02d}.mp4")
    s["promo_n"] = n + 1
    s["promo_i"] = i + 1; s["last_promo"] = now


if __name__ == "__main__":
    if os.environ.get("PROMO_OFF") == "1":
        print("promos are off (PROMO_OFF=1)"); sys.exit(0)
    try:
        with statefile.lock(STATE):
            s = load(); now = time.time()
            countdown(s, now); recap(s, now); promo(s, now)
            save(s)
    except statefile.Busy:
        print("another promo run is in progress")
    except statefile.StateCorrupt as e:
        admins(f"Promo did NOT run: {e}"); sys.exit(1)
