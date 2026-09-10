"""
CONFLUENCE — Telegram signal + paper-trading bot

Built on the price-alert bot Claude started:
  /price  /alert  /list  /remove

Added so this is not another Banana Gun clone:
  /signal   scored card with visible factors
  /buy      paper fill only if confluence clears (or override)
  /positions /sell /journal /watch /settings
  refusal   the bot will say no
  breaker   daily loss cap pauses new risk

This is not financial advice. Paper mode is the product.
Live custody/sniping is intentionally not included.
"""

from __future__ import annotations

import html
import logging
import os

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import db
import fees
import onchain
try:
    import quotes
except Exception:  # noqa: BLE001 — keep the bot alive if quotes.py is missing
    quotes = None
    logging.getLogger(__name__).exception("quotes module failed to load")
import sniper
import trading
from chains import ACTIVE, CHAINS, chain_list, resolve_chain

try:
    from chains import explorer_tx
except ImportError:

    def explorer_tx(chain: str, txid: str) -> str:
        return txid

from confluence import SignalCard, analyze
from onchain import OnchainError
from price_fetcher import PriceFetchError, get_price_usd, get_prices_usd, search_coin

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ALERT_INTERVAL_SECONDS = int(os.getenv("ALERT_INTERVAL_SECONDS", "60"))
SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "90"))
WALLET_POLL_SECONDS = int(os.getenv("WALLET_POLL_SECONDS", "75"))
DRAWDOWN_POLL_SECONDS = int(os.getenv("DRAWDOWN_POLL_SECONDS", "120"))
SNIPE_POLL_SECONDS = int(os.getenv("SNIPE_POLL_SECONDS", "25"))
LAUNCH_FEED_SECONDS = int(os.getenv("LAUNCH_FEED_SECONDS", "60"))


def _operators() -> set[int]:
    raw = os.getenv("OPERATOR_USER_IDS", "").strip()
    if not raw:
        return _allowlist()
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


def _is_operator(user_id: int) -> bool:
    ops = _operators()
    return (not ops) or user_id in ops


def _allowlist() -> set[int]:
    raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set()
    return {int(x.strip()) for x in raw.split(",") if x.strip()}


def _allowed(user_id: int) -> bool:
    allow = _allowlist()
    return (not allow) or user_id in allow


def _esc(v: object) -> str:
    return html.escape(str(v))


def _bar(score: int) -> str:
    filled = max(0, min(10, round(score / 10)))
    return "█" * filled + "░" * (10 - filled)


def render_card(card: SignalCard) -> str:
    s = card.snapshot
    lines = [
        f"<b>{_esc(s.symbol)}</b> · {_esc(s.name)}",
        f"{_esc(s.chain)}/{_esc(s.dex)} · ${_esc(f'{s.price_usd:,.6g}')}",
        f"Score <b>{card.score}</b>/100 {_bar(card.score)}  ·  {_esc(card.bias)}",
        "",
    ]
    for f in card.factors:
        lines.append(f"• <b>{_esc(f.name)}</b> {f.score} — {_esc(f.note)}")
    lines += [
        "",
        _esc(card.thesis),
        f"Suggested stop {card.stop_pct:g}% · target {card.take_pct:g}%",
    ]
    if s.liquidity_usd:
        lines.append(
            f"Liq ${_esc(f'{s.liquidity_usd:,.0f}')} · 24h vol ${_esc(f'{s.volume_24h:,.0f}')}"
        )
    if s.url:
        lines.append(f'<a href="{html.escape(s.url, quote=True)}">Chart</a>')
    lines.append("\n<i>Not financial advice. Paper fills only.</i>")
    return "\n".join(lines)


def card_keyboard(query: str, score: int) -> InlineKeyboardMarkup:
    q = query[:40]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Paper buy", callback_data=f"buy:{q}"),
                InlineKeyboardButton("Override", callback_data=f"force:{q}"),
            ],
            [
                InlineKeyboardButton("Watch", callback_data=f"watch:{q}"),
                InlineKeyboardButton("Refresh", callback_data=f"sig:{q}"),
            ],
        ]
    )


def positions_keyboard(user_id: int) -> InlineKeyboardMarkup | None:
    rows = []
    for p in db.open_positions(user_id)[:8]:
        rows.append(
            [
                InlineKeyboardButton(
                    f"Close #{p['id']} {p['symbol']}",
                    callback_data=f"close:{p['id']}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else None


async def guard(update: Update) -> bool:
    user = update.effective_user
    if not user or not _allowed(user.id):
        if update.message:
            await update.effective_message.reply_text("This bot is locked to an allowlist.")
        elif update.callback_query:
            await update.callback_query.answer("Locked.", show_alert=True)
        return False
    db.ensure_user(user.id, user.username)
    return True


async def resolve_symbol_or_reply(update: Update, symbol: str):
    try:
        candidates = search_coin(symbol)
    except PriceFetchError as exc:
        await update.effective_message.reply_text(f"Couldn't reach price data: {exc}")
        return None
    if not candidates:
        await update.effective_message.reply_text(f"No token matching '{symbol}'.")
        return None
    if len(candidates) > 1 and candidates[0]["symbol"].lower() != symbol.lower():
        options = "\n".join(
            f"  • {c['symbol'].upper()} — {c['name']} (id: {c['id']})"
            for c in candidates[:5]
        )
        await update.effective_message.reply_text(
            f"Multiple matches for '{symbol}'. Re-run with the id:\n\n{options}"
        )
        return None
    return candidates[0]


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Chains", callback_data="go:chains"),
                InlineKeyboardButton("Wallets", callback_data="go:wallets"),
            ],
            [
                InlineKeyboardButton("Signals", callback_data="go:signal:sol"),
                InlineKeyboardButton("Copytrade", callback_data="go:copy"),
            ],
            [
                InlineKeyboardButton("Settings", callback_data="go:settings"),
                InlineKeyboardButton("Active orders", callback_data="go:snipes"),
            ],
            [
                InlineKeyboardButton("Positions", callback_data="go:pos"),
                InlineKeyboardButton("Auto snipe", callback_data="go:snipehelp"),
            ],
            [
                InlineKeyboardButton("Launches", callback_data="go:launches"),
                InlineKeyboardButton("Live quote", callback_data="go:quotehelp"),
            ],
            [
                InlineKeyboardButton("Fees / cut", callback_data="go:fees"),
                InlineKeyboardButton("Drawdown", callback_data="go:pnl"),
            ],
            [
                InlineKeyboardButton("BUY & SELL — paste a CA", callback_data="go:buyhelp"),
            ],
        ]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        user = db.ensure_user(update.effective_user.id, update.effective_user.username)
        ready, _fee_note = fees.live_ready()
        cash = float(user.get("paper_cash") or 10000)
        floor = int(user.get("min_confluence") or 62)
        size = float(user.get("size_pct") or 5)
        cap = float(user.get("max_daily_loss_pct") or 8)
        text = (
            "FERZAN TRADE BOT\n"
            "One desk. Score first. Then trade.\n\n"
            f"Paper book   ${cash:,.2f}\n"
            f"Floor {floor} · size {size}% · day cap -{cap}%\n"
            f"Platform cut {fees.current_bps() / 100:.2f}%\n"
            f"{'Fee wallets live' if ready else 'Set FEE_WALLET_* to collect the cut'}\n\n"
            "Chains · ETH  BNB  Base  Solana  Hood\n"
            "Paste a token CA to open the trade card.\n"
            "Paper fills if the score clears. Live = /quote then sign in Trust.\n\n"
            "We refuse bad tape. They don't."
        )
        target = update.effective_message
        if target:
            await target.reply_text(text, reply_markup=home_keyboard())
    except Exception:
        logger.exception("start failed")
        try:
            if update.effective_message:
                await update.effective_message.reply_text(
                    "FERZAN is up. Desk hit a snag loading your paper book. "
                    "Try /signal sol — do not Redeploy yet."
                )
        except Exception:
            logger.exception("start fallback failed")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start(update, context)


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /price sol")
        return
    symbol = context.args[0]
    coin = await resolve_symbol_or_reply(update, symbol)
    if coin is None:
        return
    try:
        usd = get_price_usd(coin["id"])
    except PriceFetchError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(
        f"{coin['symbol'].upper()} ({coin['name']}): ${usd:,.6g}"
    )


async def alert_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if len(context.args) != 3:
        await update.effective_message.reply_text(
            "Usage: /alert <symbol> <above|below> <price>\nExample: /alert sol above 200"
        )
        return
    symbol, direction, price_str = context.args
    direction = direction.lower()
    if direction not in ("above", "below"):
        await update.effective_message.reply_text("Direction must be above or below.")
        return
    try:
        target_price = float(price_str)
        if target_price <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Price must be a positive number.")
        return
    coin = await resolve_symbol_or_reply(update, symbol)
    if coin is None:
        return
    alert_id = db.add_alert(
        chat_id=update.effective_chat.id,
        symbol=coin["symbol"],
        coin_id=coin["id"],
        direction=direction,
        target_price=target_price,
    )
    await update.effective_message.reply_text(
        f"Alert #{alert_id}: {coin['symbol'].upper()} {direction} ${target_price:,.6g}\n"
        f"Checked every {ALERT_INTERVAL_SECONDS}s."
    )


async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    alerts = db.list_alerts(update.effective_chat.id)
    if not alerts:
        await update.effective_message.reply_text("No active price alerts. Create one with /alert.")
        return
    lines = [
        f"#{a['id']} — {a['symbol']} {a['direction']} ${a['target_price']:,.6g}"
        for a in alerts
    ]
    await update.effective_message.reply_text("Active alerts:\n" + "\n".join(lines))


async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /remove <alert_id>")
        return
    try:
        alert_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Alert id must be a number.")
        return
    if db.delete_alert(alert_id, update.effective_chat.id):
        await update.effective_message.reply_text(f"Removed alert #{alert_id}.")
    else:
        await update.effective_message.reply_text("No such alert.")


async def _send_signal(update: Update, query: str, edit: bool = False) -> None:
    try:
        card = analyze(query)
    except PriceFetchError as exc:
        text = f"Could not score {query}: {exc}"
        if edit and update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.effective_message.reply_text(text)
        return
    text = render_card(card)
    markup = card_keyboard(card.snapshot.query or query, card.score)
    if edit and update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup
        )
    else:
        await update.effective_message.reply_text(
            text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=markup
        )


async def signal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /signal sol   or paste a contract.")
        return
    await _send_signal(update, " ".join(context.args))


async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /buy sol")
        return
    query = " ".join(context.args)
    try:
        card = analyze(query)
    except PriceFetchError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    ok, msg = trading.paper_buy(update.effective_user.id, card, force=False)
    await update.effective_message.reply_text(msg)
    if not ok:
        await update.effective_message.reply_text(
            render_card(card),
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=card_keyboard(query, card.score),
        )


async def positions_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    user = db.get_user(uid)
    opens = db.open_positions(uid)
    closed = db.recent_closed(uid, 5)
    day = db.realized_today(uid)
    lines = [
        f"Cash ${user['paper_cash']:,.2f}",
        f"Realized today {day:+,.2f} USD",
        "",
    ]
    if not opens:
        lines.append("No open paper positions.")
    for p in opens:
        lines.append(
            f"#{p['id']} LONG {p['symbol']} qty {p['qty']:.6g} @ ${p['entry']:,.6g}"
            f"  sl ${p['stop']:,.6g}  tp ${p['take']:,.6g}"
        )
    if closed:
        lines.append("\nRecent closes:")
        for p in closed:
            pnl = p["pnl"] if p["pnl"] is not None else 0
            lines.append(f"#{p['id']} {p['symbol']} {pnl:+,.2f} USD")
    await update.effective_message.reply_text("\n".join(lines), reply_markup=positions_keyboard(uid))


async def sell_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /sell <position_id>")
        return
    try:
        pos_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Position id must be a number. See /positions.")
        return
    ok, msg = trading.paper_close(update.effective_user.id, pos_id, reason="manual")
    await update.effective_message.reply_text(msg)


async def watch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    if not context.args:
        names = db.watchlist_of(uid)
        await update.effective_message.reply_text(
            "Watchlist: " + (", ".join(names) if names else "(empty)")
            + "\nAdd with /watch jup   remove with /unwatch jup"
        )
        return
    q = context.args[0]
    db.add_watch(uid, q)
    await update.effective_message.reply_text(f"Watching {q.upper()}. Scanner will score it.")


async def unwatch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /unwatch sol")
        return
    db.remove_watch(update.effective_user.id, context.args[0])
    await update.effective_message.reply_text(f"Removed {context.args[0].upper()}.")


async def journal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = db.recent_journal(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("Journal is empty.")
        return
    lines = [f"{r['kind']}: {r['body']}" for r in rows]
    await update.effective_message.reply_text("\n".join(lines)[:3500])


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    user = db.get_user(uid)
    if context.args and len(context.args) == 2:
        key, raw = context.args[0].lower(), context.args[1]
        try:
            if key in {"size", "size_pct"}:
                val = float(raw)
                if not 1 <= val <= 20:
                    raise ValueError
                db.update_user(uid, size_pct=val)
            elif key in {"floor", "min", "score"}:
                val = int(raw)
                if not 40 <= val <= 90:
                    raise ValueError
                db.update_user(uid, min_confluence=val)
            elif key in {"cap", "dd", "daily"}:
                val = float(raw)
                if not 1 <= val <= 25:
                    raise ValueError
                db.update_user(uid, max_daily_loss_pct=val)
            elif key == "alerts":
                db.update_user(uid, alerts_on=1 if raw in {"on", "1", "true"} else 0)
            elif key in {"ddalert", "drawdown"}:
                val = float(raw)
                if not 3 <= val <= 40:
                    raise ValueError
                db.update_user(uid, drawdown_alert_pct=val)
            else:
                await update.effective_message.reply_text("Keys: size, floor, cap, alerts, ddalert")
                return
        except ValueError:
            await update.effective_message.reply_text("Out of range.")
            return
        user = db.get_user(uid)
        await update.effective_message.reply_text("Updated.")
    await update.effective_message.reply_text(
        "Risk vault\n"
        f"size {user['size_pct']}% of cash per ticket (1-20)\n"
        f"floor {user['min_confluence']} confluence to auto-fill (40-90)\n"
        f"cap -{user['max_daily_loss_pct']}% daily realized (1-25)\n"
        f"scanner alerts {'on' if user['alerts_on'] else 'off'}\n"
        f"drawdown ping at -{user.get('drawdown_alert_pct') or 12}% from peak\n\n"
        "Examples:\n/settings size 3\n/settings floor 70\n/settings cap 5\n"
        "/settings alerts off\n/settings ddalert 10"
    )


async def resetpaper_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    start_bal = float(os.getenv("PAPER_STARTING_BALANCE", "10000"))
    for p in db.open_positions(uid):
        db.close_position(p["id"], p["entry"], 0)
    db.update_user(uid, paper_cash=start_bal, starting_equity=start_bal, peak_equity=start_bal)
    db.add_journal(uid, "RESET", f"Paper book reset to ${start_bal:,.0f}")
    await update.effective_message.reply_text(f"Paper account reset to ${start_bal:,.0f}.")


async def watchwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if len(context.args) < 2:
        await update.effective_message.reply_text(
            "Usage: /watchwallet <eth|base|bsc|arb|op|polygon|sol> <address> [label]\n"
            "Example: /watchwallet sol 7xKX... whale1\n"
            f"Providers: {onchain.status_line()}"
        )
        return
    chain_raw, address = context.args[0], context.args[1]
    label = " ".join(context.args[2:]) if len(context.args) > 2 else None
    try:
        chain = onchain.normalize_chain(chain_raw)
        events = onchain.recent_activity(chain, address, limit=3)
    except OnchainError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    except Exception as exc:
        await update.effective_message.reply_text(f"Provider error: {exc}")
        return
    wid = db.add_watched_wallet(update.effective_user.id, chain, address, label)
    if events:
        db.set_wallet_cursor(wid, events[0].txid)
    preview = "\n".join(f"• {e.summary}" for e in events[:3]) or "No recent prints."
    await update.effective_message.reply_text(
        f"Watching wallet #{wid} on {chain}\n{address}\n{preview}"
    )


async def wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = db.list_watched_wallets(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("No watched wallets. /watchwallet sol <addr>")
        return
    lines = [
        f"#{r['id']} {r['chain']} {r['address'][:10]}… {r['label'] or ''}".strip()
        for r in rows
    ]
    await update.effective_message.reply_text("Watched wallets:\n" + "\n".join(lines))


async def unwatchwallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /unwatchwallet <id>")
        return
    try:
        wid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Id must be a number. See /wallets.")
        return
    if db.delete_watched_wallet(wid, update.effective_user.id):
        await update.effective_message.reply_text(f"Dropped wallet #{wid}.")
    else:
        await update.effective_message.reply_text("No such wallet id.")


async def drawdown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    user = db.get_user(uid)
    equity, peak, dd = trading.update_peak_and_drawdown(uid)
    threshold = float(user.get("drawdown_alert_pct") or 12)
    await update.effective_message.reply_text(
        f"Paper equity ${equity:,.2f}\n"
        f"Peak ${peak:,.2f}\n"
        f"Drawdown {dd:.2f}%  (alert at -{threshold:g}%)\n"
        "On-chain token books need Etherscan/Helius keys plus a portfolio endpoint. "
        "Native EVM balance is included when you /watchwallet an address and a key is set."
    )


async def fees_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    uid = update.effective_user.id
    mine = db.fee_totals(uid)
    ready, live_note = fees.live_ready()
    wallets = fees.fee_wallets()
    lines = [
        f"Cut {fees.current_bps() / 100:.2f}% per paper fill (hard-capped at 1%).",
        f"You have paid ${mine['fee_usd']:,.4f} across {mine['count']} tickets.",
        live_note,
    ]
    if wallets["sol"]:
        lines.append(f"SOL fee wallet {wallets['sol']}")
    if wallets["evm"]:
        lines.append(f"EVM fee wallet {wallets['evm']}")
    if wallets["jupiter_fee_account"]:
        lines.append(f"Jupiter fee account {wallets['jupiter_fee_account']}")
    lines.append(
        "Live path: Jupiter platformFeeBps + feeAccount, or 0x swapFeeRecipient. "
        "No seed phrases. The router pays the cut on-chain."
    )
    if _is_operator(uid):
        all_fees = db.fee_totals()
        lines.append(
            f"\nOperator treasury (paper): ${all_fees['fee_usd']:,.4f} "
            f"on ${all_fees['notional_usd']:,.2f} notional."
        )
        for row in db.recent_fees(8):
            lines.append(
                f"  {row['kind']} ${row['fee_usd']:.4f} ({row['fee_bps']}bps) u{row['user_id']}"
            )
    await update.effective_message.reply_text("\n".join(lines))


def _parse_snipe_args(args: list[str]) -> tuple[str | None, str, float]:
    if not args:
        raise ValueError("Usage: /snipe [chain] <token-or-CA> [usd]")
    chain = resolve_chain(args[0])
    if chain:
        rest = args[1:]
    else:
        rest = args
    if not rest:
        raise ValueError("Need a token or contract after the chain.")
    usd = 40.0
    query_parts = []
    for part in rest:
        try:
            usd = float(part)
        except ValueError:
            query_parts.append(part)
    if not query_parts:
        raise ValueError("Need a token or contract.")
    return chain, " ".join(query_parts), usd


async def snipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    try:
        chain, query, usd = _parse_snipe_args(context.args)
    except ValueError as exc:
        await update.effective_message.reply_text(
            f"{exc}\nChains: {chain_list()}\n"
            "Example: /snipe base 0xabc... 25\n"
            "Optional after that in /settings: floor, size. Snipe uses your score floor "
            "and refuses vetoed pools unless you lower gates."
        )
        return
    user = db.get_user(update.effective_user.id)
    sid = sniper.arm(
        user_id=update.effective_user.id,
        query=query,
        chain=chain,
        usd=max(10.0, min(usd, 500.0)),
        min_liq=40_000,
        min_score=int(user["min_confluence"]),
        max_age_h=6.0,
        require_long=True,
    )
    await update.effective_message.reply_text(
        f"Armed snipe #{sid}\n"
        f"{chain or 'any-chain'} {query} ${usd:.0f}\n"
        f"Gates: liq ≥ $40k, score ≥ {user['min_confluence']}, age ≤ 6h, bias LONG, no veto.\n"
        "When DexScreener/Gecko sees a tradable pool that clears those, paper-fill fires.\n"
        "This is not a private-mempool first-block snipe."
    )
    armed = next((r for r in db.active_snipes(update.effective_user.id) if r["id"] == sid), None)
    status, msg = sniper.try_fill(armed) if armed else ("armed", "")
    if status == "filled":
        await update.effective_message.reply_text("Immediate fill\n" + msg)
    elif status == "miss":
        await update.effective_message.reply_text("Armed but not filled yet:\n" + msg)


async def snipes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    rows = db.active_snipes(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("No snipes. Arm one with /snipe sol <CA> 40")
        return
    lines = [
        f"#{r['id']} {r['status']} {r['chain'] or '*'} {r['query']} ${r['usd']:.0f}"
        for r in rows[:15]
    ]
    await update.effective_message.reply_text("\n".join(lines))


async def cancelsnipe_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /cancelsnipe <id>")
        return
    try:
        sid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Id must be a number.")
        return
    if db.cancel_snipe(sid, update.effective_user.id):
        await update.effective_message.reply_text(f"Cancelled snipe #{sid}.")
    else:
        await update.effective_message.reply_text("No armed snipe with that id.")


async def quote_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text(
            "Live quote (unsigned)\n"
            "/quote sol <token-mint> 50\n"
            "/quote base 0xabc… 25\n"
            "50 = dollars of native in. Your 0.50% cut is on the quote.\n"
            "You sign in Phantom/Rabby. The bot never holds keys."
        )
        return
    chain = resolve_chain(context.args[0])
    rest = context.args[1:] if chain else context.args
    if not chain:
        chain = "sol"
    if not rest:
        await update.effective_message.reply_text("Need a token mint or contract.")
        return
    usd = 50.0
    token_parts = []
    for part in rest:
        try:
            usd = float(part)
        except ValueError:
            token_parts.append(part)
    token = " ".join(token_parts).strip()
    if not token:
        await update.effective_message.reply_text("Need a token mint or contract.")
        return
    try:
        if quotes is None:
            await update.effective_message.reply_text("Quote module not loaded. Re-upload quotes.py.")
            return
        if chain == "sol":
            q = quotes.sol_quote(token, max(5.0, usd))
        else:
            q = quotes.evm_quote(chain, token, max(5.0, usd))
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(quotes.format_quote(q))


async def chains_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    lines = ["Trading venues (paper now, live router later)", ""]
    for cid in ACTIVE:
        m = CHAINS[cid]
        extra = f" · chain {m['chain_id']}" if m.get("chain_id") else ""
        lines.append(f"{m['label']} /{cid}{extra}")
        lines.append(f"  data {m['dexscreener']} · router {m['router']}")
        lines.append(f"  gas {m['native']} · {m['rpc']}")
        if m.get("notes"):
            lines.append(f"  {m['notes']}")
        lines.append("")
    lines.append("Examples:")
    lines.append("/signal hood CASHCAT")
    lines.append("/snipe bsc 0xabc… 25")
    lines.append("/watchwallet hood 0x… whale")
    lines.append("/launches sol")
    await update.effective_message.reply_text("\n".join(lines))


async def launches_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    chain = resolve_chain(context.args[0]) if context.args else None
    launches = sniper.fetch_new_pools(chain, limit=8)
    if not launches:
        await update.effective_message.reply_text(
            "No fresh pools from GeckoTerminal right now. Try /launches sol"
        )
        return
    lines = ["New pools"]
    for ln in launches:
        lines.append(
            f"{ln.chain} {ln.symbol} liq ${ln.liquidity_usd:,.0f} {ln.token[:12]}…"
        )
    await update.effective_message.reply_text("\n".join(lines)[:3500])


async def treasury_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    if not _is_operator(update.effective_user.id):
        await update.effective_message.reply_text("Operator only.")
        return
    await fees_cmd(update, context)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await guard(update):
        return
    text = (update.message.text or "").strip()
    if not text or text.startswith("/"):
        return
    if len(text) > 80:
        return
    await _send_signal(update, text)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not await guard(update):
        return
    uid = update.effective_user.id
    data = query.data or ""
    if data.startswith("go:"):
        kind = data[3:]
        if kind.startswith("signal:"):
            await _send_signal(update, kind.split(":", 1)[1], edit=False)
        elif kind == "help":
            await start(update, context)
        elif kind == "pos":
            fake = update
            if update.message is None and update.effective_message:
                pass
            await positions_cmd(update, context)
        elif kind == "launches":
            context.args = ["sol"]
            await launches_cmd(update, context)
        elif kind == "chains":
            await chains_cmd(update, context)
        elif kind == "fees":
            await fees_cmd(update, context)
        elif kind == "wallets":
            await wallets_cmd(update, context)
        elif kind == "settings":
            context.args = []
            await settings_cmd(update, context)
        elif kind == "snipes":
            await snipes_cmd(update, context)
        elif kind == "copy":
            await context.bot.send_message(
                uid,
                "Copytrade\n"
                "/watchwallet sol <address>\n"
                "/watchwallet eth 0x...\n"
                "/wallets\n"
                "Ferzan pings you when they move. It will not spend your Trust wallet.",
            )
        elif kind == "snipehelp":
            await context.bot.send_message(
                uid,
                "Auto snipe (gated)\n"
                "/snipe sol <CA> 40\n"
                "/snipes   /cancelsnipe 3\n"
                "Fills paper when liq/score/age gates pass. Not first-block.",
            )
        elif kind == "quotehelp":
            await context.bot.send_message(
                uid,
                "Live quote — you sign in Trust\n"
                "/quote sol <CA> 50\n"
                "/quote base 0x... 25\n"
                "0.50% cut is on the quote. No seed in this bot.",
            )
        elif kind == "pnl":
            await context.bot.send_message(
                uid,
                f"Paper cash is on /positions and /drawdown.\nSend /positions",
            )
        elif kind == "buyhelp":
            await context.bot.send_message(
                uid,
                "BUY & SELL\n"
                "Paste a CA in this chat.\n"
                "/buy sol     paper if score clears\n"
                "/sell 3      close position #3\n"
                "/quote sol <CA> 50     sign in Trust",
            )
        return
    if data.startswith("sig:"):
        await _send_signal(update, data[4:], edit=True)
        return
    if data.startswith("watch:"):
        name = data[6:]
        db.add_watch(uid, name)
        await query.edit_message_reply_markup(reply_markup=card_keyboard(name, 0))
        await context.bot.send_message(uid, f"Watching {name.upper()}.")
        return
    if data.startswith("buy:") or data.startswith("force:"):
        force = data.startswith("force:")
        name = data.split(":", 1)[1]
        try:
            card = analyze(name)
        except PriceFetchError as exc:
            await context.bot.send_message(uid, str(exc))
            return
        ok, msg = trading.paper_buy(uid, card, force=force)
        await context.bot.send_message(uid, msg)
        return
    if data.startswith("close:"):
        try:
            pos_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        ok, msg = trading.paper_close(uid, pos_id, reason="manual")
        await context.bot.send_message(uid, msg)


async def check_alerts_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    alerts = db.get_all_active_alerts()
    if not alerts:
        return
    coin_ids = [a["coin_id"] for a in alerts]
    try:
        prices = get_prices_usd(coin_ids)
    except PriceFetchError as exc:
        logger.warning("Price poll failed: %s", exc)
        return
    for alert in alerts:
        current = prices.get(alert["coin_id"])
        if current is None:
            continue
        hit = (
            alert["direction"] == "above" and current >= alert["target_price"]
        ) or (
            alert["direction"] == "below" and current <= alert["target_price"]
        )
        if not hit:
            continue
        db.deactivate_alert(alert["id"])
        try:
            await context.bot.send_message(
                chat_id=alert["chat_id"],
                text=(
                    f"Alert #{alert['id']} triggered.\n"
                    f"{alert['symbol']} is ${current:,.6g} "
                    f"({alert['direction']} ${alert['target_price']:,.6g})"
                ),
            )
        except Exception:
            logger.exception("Failed to notify chat %s", alert["chat_id"])


async def scan_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for notice in trading.mark_open_positions():
        user_id, _pos_id, msg = notice
        try:
            await context.bot.send_message(user_id, "Auto-exit\n" + msg)
        except Exception:
            logger.exception("Exit notice failed for %s", user_id)

    for user_id in db.list_alert_users():
        user = db.get_user(user_id)
        if not user:
            continue
        floor = int(user["min_confluence"])
        for name in db.watchlist_of(user_id):
            try:
                card = analyze(name)
            except PriceFetchError:
                continue
            if card.bias != "LONG" or card.score < floor:
                continue
            if not db.should_resend_signal(user_id, name, card.score):
                continue
            try:
                await context.bot.send_message(
                    user_id,
                    "Scanner\n" + render_card(card),
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=card_keyboard(name, card.score),
                )
            except Exception:
                logger.exception("Scanner send failed for %s", user_id)


async def wallet_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for row in db.list_watched_wallets():
        try:
            events = onchain.recent_activity(row["chain"], row["address"], limit=6)
        except Exception as exc:
            logger.info("wallet poll skip #%s: %s", row["id"], exc)
            continue
        if not events:
            continue
        cursor = row.get("cursor")
        fresh = []
        for ev in events:
            if cursor and ev.txid == cursor:
                break
            fresh.append(ev)
        if not fresh:
            continue
        db.set_wallet_cursor(row["id"], events[0].txid)
        lines = [
            f"Wallet #{row['id']} {row['chain']} {row['label'] or row['address'][:8]}",
        ]
        for ev in reversed(fresh[:4]):
            lines.append(f"• {ev.summary}")
            if ev.txid:
                try:
                    lines.append("  " + explorer_tx(row["chain"], ev.txid))
                except Exception:
                    pass
        try:
            await context.bot.send_message(row["user_id"], "\n".join(lines)[:3500])
        except Exception:
            logger.exception("wallet notify failed for %s", row["user_id"])


async def drawdown_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for user in db.list_users():
        uid = int(user["user_id"])
        try:
            equity, peak, dd = trading.update_peak_and_drawdown(uid)
        except Exception:
            logger.exception("drawdown mark failed for %s", uid)
            continue
        threshold = float(user.get("drawdown_alert_pct") or 12)
        if dd < threshold:
            continue
        if not db.should_resend_signal(uid, "__drawdown__", int(dd), cooldown_s=6 * 3600):
            continue
        try:
            await context.bot.send_message(
                uid,
                f"Drawdown alert: paper book is down {dd:.1f}% from peak "
                f"(${peak:,.2f} → ${equity:,.2f}). "
                f"Daily breaker is separate and still at -{user['max_daily_loss_pct']}%.",
            )
        except Exception:
            logger.exception("drawdown notify failed for %s", uid)


async def snipe_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    for user_id, sid, status, msg in sniper.scan_armed():
        if status != "filled":
            continue
        try:
            await context.bot.send_message(user_id, f"Snipe #{sid} filled\n{msg}")
        except Exception:
            logger.exception("snipe notify failed for %s", user_id)


async def launch_feed_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    launches = sniper.fetch_new_pools(None, limit=12)
    if not launches:
        return
    interesting = [ln for ln in launches if ln.liquidity_usd >= 25_000]
    if not interesting:
        return
    for user in db.list_users():
        if not user.get("alerts_on"):
            continue
        uid = int(user["user_id"])
        lines = ["Launch feed"]
        sent_any = False
        for ln in interesting[:6]:
            key = f"launch:{ln.chain}:{ln.token[:24]}"
            if not db.should_resend_signal(uid, key, 1, cooldown_s=6 * 3600):
                continue
            lines.append(
                f"{ln.chain} {ln.symbol} liq ${ln.liquidity_usd:,.0f}\n{ln.token}"
            )
            sent_any = True
        if not sent_any:
            continue
        lines.append("Arm with /snipe <chain> <CA> 40 — gates still apply.")
        try:
            await context.bot.send_message(uid, "\n".join(lines)[:3500])
        except Exception:
            logger.exception("launch feed failed for %s", uid)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN in .env before running.")

    db.init_db()

    async def _post_init(application: Application) -> None:
        try:
            await application.bot.set_my_commands(
                [
                    BotCommand("start", "Home"),
                    BotCommand("signal", "Score a market"),
                    BotCommand("buy", "Paper buy if it clears"),
                    BotCommand("quote", "Live unsigned quote + cut"),
                    BotCommand("positions", "Paper book"),
                    BotCommand("snipe", "Arm a gated snipe"),
                    BotCommand("launches", "New pools"),
                    BotCommand("chains", "Venues"),
                    BotCommand("fees", "Your cut"),
                    BotCommand("settings", "Risk vault"),
                ]
            )
        except Exception:
            logger.exception("set_my_commands failed")

    app = Application.builder().token(token).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("alert", alert_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("signal", signal_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler("positions", positions_cmd))
    app.add_handler(CommandHandler("sell", sell_cmd))
    app.add_handler(CommandHandler("watch", watch_cmd))
    app.add_handler(CommandHandler("unwatch", unwatch_cmd))
    app.add_handler(CommandHandler("journal", journal_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("resetpaper", resetpaper_cmd))
    app.add_handler(CommandHandler("watchwallet", watchwallet_cmd))
    app.add_handler(CommandHandler("wallets", wallets_cmd))
    app.add_handler(CommandHandler("unwatchwallet", unwatchwallet_cmd))
    app.add_handler(CommandHandler("drawdown", drawdown_cmd))
    app.add_handler(CommandHandler("fees", fees_cmd))
    app.add_handler(CommandHandler("treasury", treasury_cmd))
    app.add_handler(CommandHandler("snipe", snipe_cmd))
    app.add_handler(CommandHandler("snipes", snipes_cmd))
    app.add_handler(CommandHandler("cancelsnipe", cancelsnipe_cmd))
    app.add_handler(CommandHandler("launches", launches_cmd))
    app.add_handler(CommandHandler("chains", chains_cmd))
    app.add_handler(CommandHandler("quote", quote_cmd))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("live", quote_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    jq = app.job_queue
    if jq is not None:
        jq.run_repeating(check_alerts_job, interval=ALERT_INTERVAL_SECONDS, first=12)
        jq.run_repeating(scan_job, interval=SCAN_INTERVAL_SECONDS, first=25)
        jq.run_repeating(wallet_job, interval=WALLET_POLL_SECONDS, first=40)
        jq.run_repeating(drawdown_job, interval=DRAWDOWN_POLL_SECONDS, first=55)
        jq.run_repeating(snipe_job, interval=SNIPE_POLL_SECONDS, first=18)
        jq.run_repeating(launch_feed_job, interval=LAUNCH_FEED_SECONDS, first=35)
    else:
        logger.warning("job-queue extra missing; commands still work, scanners off")

    logger.info("FERZAN starting")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
