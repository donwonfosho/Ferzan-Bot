"""
/liquidity: add a SunSwap V2 pool for your Tron coin from your Ferzan Trade Bot wallet.

  /liquidity -> pick one of your Tron coins -> how much TRX -> how much of your coins
  -> the bot approves the coin (if needed) and SIMULATES the pool on Tron to get the exact energy cost
  -> you see price, market cap, cost, and whether the LP gets burned (default: burned = locked forever)
  -> Create pool: the Trade Desk helper sends it, then burns the LP; the result is posted here + in the channel.

The wallet key never enters this process (Trade Desk/tron_liquidity_exec.py signs). Slow steps run as background
tasks so the bot keeps answering everyone else.
"""
from __future__ import annotations

import html
import os
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import launch_bot_db as db
import tron_launch as tron

HELPER = "tron_liquidity_exec.py"
TRX_PRESETS = [100, 250, 500, 1000]
PCT_PRESETS = [50, 80, 90, 100]
PLANS: dict[str, dict] = {}   # plan id -> plan (kept in memory; a restart just means /liquidity again)
TRADE = (os.environ.get("FERZAN_BOT_USERNAME") or "Ferzan_Trade_Bot").lstrip("@")
BUY = (os.environ.get("FERZAN_BUY_BOT") or "Ferzan_Buy_Bot").lstrip("@")


def _e(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows + [[InlineKeyboardButton("✖️ Cancel", callback_data="lp:no")]])


def _coins(uid: int) -> list:
    with db._get_conn() as c:
        rows = c.execute("SELECT id, name, symbol, result_token_address, total_supply, decimals FROM launch_requests "
                         "WHERE telegram_user_id = ? AND chain = 'tron' AND status = 'confirmed' "
                         "AND result_token_address LIKE 'T%' ORDER BY created_at DESC LIMIT 10", (uid,)).fetchall()
    return [dict(r) for r in rows]


async def liquidity_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    coins = _coins(uid)
    if not tron.live():
        await update.effective_message.reply_text("Tron is not open in the Launch Bot yet.")
        return
    if not coins:
        await update.effective_message.reply_text("You have no Tron coins yet. /launch one first, then come back here.")
        return
    context.user_data["lp"] = {"coins": coins}
    rows = [[InlineKeyboardButton(f"{c['name']} (${c['symbol']})", callback_data=f"lp:t:{i}")] for i, c in enumerate(coins)]
    await update.effective_message.reply_text(
        "<b>💧 Add liquidity on SunSwap</b>\n\nA pool lets anyone buy and sell your coin (Trade Bot, Buy Bot alerts, "
        "charts). It's made from your Trade Bot wallet: you put in TRX + some of your coins, and that ratio sets the "
        "starting price.\n\nWhich coin?", parse_mode="HTML", reply_markup=_kb(rows))


async def _ask_trx(q, st: dict):
    c = st["coin"]
    i = st["info"]
    dec = int(i.get("decimals") or c["decimals"] or 6)
    held = int(i.get("token_balance") or 0) / 10**dec
    rows = [[InlineKeyboardButton(f"{n:,} TRX", callback_data=f"lp:x:{n}") for n in TRX_PRESETS[:2]],
            [InlineKeyboardButton(f"{n:,} TRX", callback_data=f"lp:x:{n}") for n in TRX_PRESETS[2:]]]
    pool = f"\nA pool already exists: <code>{_e(i['pair'])}</code> (you'd be adding to it)." if i.get("pair") else ""
    await q.edit_message_text(
        f"<b>{_e(c['name'])} (${_e(c['symbol'])})</b>\nTrade Bot wallet <code>{_e(i['address'])}</code>\n"
        f"TRX: <b>{i['trx']:,.2f}</b> · your {_e(c['symbol'])}: <b>{held:,.0f}</b>{pool}\n\n"
        "<b>How much TRX goes into the pool?</b> Tap one or type a number. More TRX = a deeper pool and a steadier price. "
        "Keep extra TRX for the energy cost (shown before anything is sent).",
        parse_mode="HTML", reply_markup=_kb(rows))


async def lp_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    parts = q.data.split(":")
    if parts[1] == "go":  # straight from the "coin is live" message: skip the coin picker
        with db._get_conn() as c:
            row = c.execute("SELECT id, name, symbol, result_token_address, total_supply, decimals FROM launch_requests "
                            "WHERE id = ? AND telegram_user_id = ? AND chain = 'tron' AND status = 'confirmed' "
                            "AND result_token_address LIKE 'T%'", (":".join(parts[2:]), update.effective_user.id)).fetchone()
        if not row:
            await q.answer("Couldn't find that coin. Send /liquidity.", show_alert=True)
            return
        context.user_data["lp"] = {"coins": [dict(row)]}
        parts = ["lp", "t", "0"]
    st = context.user_data.get("lp") or {}
    if parts[1] == "no":
        await q.answer()
        context.user_data.pop("lp", None)
        await q.edit_message_text("Cancelled. Nothing was sent.")
        return
    if parts[1] == "t":
        await q.answer("Reading your wallet…")
        try:
            st["coin"] = st["coins"][int(parts[2])]
        except (KeyError, IndexError, ValueError):
            await q.edit_message_text("That list expired. Send /liquidity again.")
            return
        info = await tron.run("info", {"uid": update.effective_user.id, "token": st["coin"]["result_token_address"]},
                              timeout=40, script=HELPER)
        if not info.get("ok"):
            await q.edit_message_text(f"Couldn't read your Trade Bot wallet ({_e(info.get('error'))}). Nothing was sent.")
            return
        st["info"] = info
        st["await"] = "trx"
        await _ask_trx(q, st)
        return
    if parts[1] == "x":
        await q.answer()
        await _ask_pct(q.message, st, int(parts[2]), edit=q)
        return
    if parts[1] == "p":
        await q.answer("Checking the pool on Tron… (up to a minute)")
        await q.edit_message_text("⏳ Approving your coin for SunSwap (if needed) and simulating the pool on Tron. "
                                  "Nothing is added yet.")
        context.application.create_task(_prepare(context, update.effective_chat.id, update.effective_user.id, st,
                                                 int(parts[2])))
        return


async def lp_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = context.user_data.get("lp") or {}
    if st.get("await") != "trx":
        return
    try:
        n = float(update.message.text.replace(",", "").replace("TRX", "").strip())
    except ValueError:
        await update.message.reply_text("Send just a number of TRX, like 300.")
        return
    if n < 10:
        await update.message.reply_text("Use at least 10 TRX.")
        return
    await _ask_pct(update.message, st, n)


async def _ask_pct(msg, st: dict, trx: float, edit=None):
    st["trx"] = trx
    st["await"] = ""
    rows = [[InlineKeyboardButton(f"{p}%", callback_data=f"lp:p:{p}") for p in PCT_PRESETS]]
    text = (f"<b>{trx:,.0f} TRX</b> goes in.\n\n<b>How much of your {_e(st['coin']['symbol'])} goes in with it?</b>\n"
            "Most creators put 80-100% in the pool. Fewer coins in = a higher starting price.")
    if edit:
        await edit.edit_message_text(text, parse_mode="HTML", reply_markup=_kb(rows))
    else:
        await msg.reply_text(text, parse_mode="HTML", reply_markup=_kb(rows))


async def _prepare(context, chat_id: int, uid: int, st: dict, pct: int):
    bot = context.bot
    c, i = st["coin"], st["info"]
    dec = int(i.get("decimals") or c["decimals"] or 6)
    token_raw = int(i.get("token_balance") or 0) * pct // 100
    trx_sun = int(st["trx"] * 1_000_000)
    res = await tron.run("prepare", {"uid": uid, "token": c["result_token_address"], "trx_sun": trx_sun,
                                     "token_raw": str(token_raw)}, timeout=120, script=HELPER)
    if not res.get("ok"):
        err = res.get("error") or "unknown"
        if err == "low_balance":
            err = f"your Trade Bot wallet needs about {res.get('need_trx', 30):g} TRX for the approval step"
        await bot.send_message(chat_id, f"❌ Can't set up this pool: {_e(err)}. Nothing was added.", parse_mode="HTML")
        return
    pid = secrets.token_hex(4)
    tokens = token_raw / 10**dec
    price = st["trx"] / tokens if tokens else 0
    supply = int(c["total_supply"]) / 10**dec
    PLANS[pid] = {"uid": uid, "chat": chat_id, "coin": c, "trx_sun": trx_sun, "token_raw": token_raw, "burn": True,
                  "at": time.time(), "tokens": tokens, "price": price, "mcap": price * supply, "res": res}
    await bot.send_message(chat_id, _plan_text(PLANS[pid]), parse_mode="HTML", reply_markup=_plan_kb(pid, PLANS[pid]))


def _plan_text(p: dict) -> str:
    r, c = p["res"], p["coin"]
    lines = [f"<b>💧 Pool for {_e(c['name'])} (${_e(c['symbol'])})</b>",
             f"Put in: <b>{p['trx_sun'] / 1e6:,.0f} TRX</b> + <b>{p['tokens']:,.0f} {_e(c['symbol'])}</b>",
             f"Starting price: <b>{p['price']:.10f} TRX</b> per coin" if not r.get("exists") else
             "Adding to the existing pool at its current price (2% max drift).",
             f"Starting market cap: <b>{p['mcap']:,.0f} TRX</b>" if not r.get("exists") else "",
             f"Network energy (measured on Tron): <b>about {r['energy_cost_trx']:,.2f} TRX</b>",
             f"Wallet: {r['trx']:,.2f} TRX · needs about {r['need_trx']:,.2f} TRX",
             "🔥 LP tokens get <b>burned</b>: the liquidity is locked forever, which is what buyers look for."
             if p["burn"] else "🔓 LP tokens stay in your Trade Bot wallet (you could pull the liquidity later).",
             "✅ Approved for SunSwap: " + r["approved"] if r.get("approved") else ""]
    if not r.get("enough"):
        lines.append(f"\n⚠️ Not enough TRX: send {r['need_trx'] - r['trx']:,.2f} more to <code>{_e(r['address'])}</code> "
                     "(Tron network), then /liquidity again.")
    return "\n".join(x for x in lines if x)


def _plan_kb(pid: str, p: dict) -> InlineKeyboardMarkup:
    rows = []
    if p["res"].get("enough"):
        rows.append([InlineKeyboardButton("✅ Create pool", callback_data=f"lpg:go:{pid}")])
    rows.append([InlineKeyboardButton("🔓 Don't burn LP" if p["burn"] else "🔥 Burn LP (recommended)",
                                      callback_data=f"lpg:burn:{pid}")])
    rows.append([InlineKeyboardButton("✖️ Cancel", callback_data=f"lpg:no:{pid}")])
    return InlineKeyboardMarkup(rows)


async def lpg_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, act, pid = q.data.split(":")
    p = PLANS.get(pid)
    if not p or p["uid"] != update.effective_user.id or time.time() - p["at"] > 1800:
        await q.answer("That plan expired. Send /liquidity again.", show_alert=True)
        return
    if act == "no":
        PLANS.pop(pid, None)
        await q.answer()
        await q.edit_message_text("Cancelled. No pool was created.")
        return
    if act == "burn":
        p["burn"] = not p["burn"]
        await q.answer()
        await q.edit_message_text(_plan_text(p), parse_mode="HTML", reply_markup=_plan_kb(pid, p))
        return
    PLANS.pop(pid, None)  # one tap = one send
    await q.answer("Creating the pool…")
    await q.edit_message_text(_plan_text(p) + "\n\n⏳ Creating the pool on SunSwap. This takes about a minute; "
                              "please don't start another one.", parse_mode="HTML")
    context.application.create_task(_add(context.bot, pid, p))


async def _add(bot, pid: str, p: dict):
    c = p["coin"]
    res = await tron.run("add", {"uid": p["uid"], "token": c["result_token_address"], "trx_sun": p["trx_sun"],
                                 "token_raw": str(p["token_raw"]), "burn": p["burn"], "rid": f"{c['id']}:{pid}"},
                         timeout=240, script=HELPER)
    if not res.get("ok"):
        link = f"\n{_e(res.get('link'))}" if res.get("link") else ""
        await bot.send_message(p["chat"], f"❌ Pool not created: {_e(res.get('error'))}.{link}", parse_mode="HTML",
                               disable_web_page_preview=True)
        return
    ca = c["result_token_address"]
    burned = res.get("lp_burned")
    text = (f"💧 <b>Liquidity added: {_e(c['name'])} (${_e(c['symbol'])})</b> on Tron\n"
            f"{p['trx_sun'] / 1e6:,.0f} TRX + {p['tokens']:,.0f} {_e(c['symbol'])} on SunSwap V2\n"
            f"CA: <code>{_e(ca)}</code>\nPool: <code>{_e(res.get('pair'))}</code>\n"
            + ("🔥 LP burned: liquidity locked forever\n" if burned else
               ("⚠️ LP burn didn't confirm; the LP tokens are still in the creator's wallet\n" if p["burn"] else ""))
            + f"Tx: {_e(res.get('link'))}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⚡ Buy in Ferzan Trade Bot", url=f"https://t.me/{TRADE}?start=buy_{ca}")],
        [InlineKeyboardButton("🟢 Add Buy Bot to your group", url=f"https://t.me/{BUY}?startgroup=trk_tron_{ca}")],
    ])
    await bot.send_message(p["chat"], text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    ch = (os.environ.get("FERZAN_LAUNCHES_CHANNEL") or "").strip()
    if ch:
        try:
            await bot.send_message(ch, text, parse_mode="HTML", disable_web_page_preview=True,
                                   reply_markup=InlineKeyboardMarkup([kb.inline_keyboard[0]]))
        except Exception:  # noqa: BLE001
            pass


def register(app) -> None:
    app.add_handler(CommandHandler("liquidity", liquidity_cmd))
    app.add_handler(CallbackQueryHandler(lp_cb, pattern="^lp:"))
    app.add_handler(CallbackQueryHandler(lpg_cb, pattern="^lpg:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lp_text), group=2)
