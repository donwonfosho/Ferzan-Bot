"""Ferzan Liquidity Desk.

Probe a live pool. Build a sliced (randomized interval) route.
Paper by default. Live = quotes from Jupiter / 0x, one side only.
"""

from __future__ import annotations

import logging
import re

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

import config
from dex_data import DexDataError, estimate_price_impact_pct, fetch_token_pools, liquidity_score
from router import live_ok, make_plan, max_usd, plan_text, run_live_quotes, run_paper

EVM_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SOL_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
TON_RE = re.compile(r"^(EQ|UQ|kQ)[A-Za-z0-9_-]{46,}$")

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("liq")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = "LIVE QUOTES" if live_ok() else "PAPER"
    await update.effective_message.reply_text(
        "Ferzan Liquidity Desk\n"
        f"Mode: {mode} · cap ${max_usd():.0f} if live\n\n"
        "Paste a CA or /liq <ca>\n"
        "/plan <ca> [usd] [buy|sell] [slices]\n"
        "/route <ca> [usd] [buy|sell] [slices]\n\n"
        "Sliced routing + jittered waits. One side per job.\n"
        "Does not ping-pong buy and sell on the same book.\n"
        "Live broadcast stays in Ferzan Trade Bot."
    )


async def _report(update: Update, address: str) -> None:
    await update.effective_message.reply_text(f"Looking up `{address}`…", parse_mode="Markdown")
    try:
        pools = fetch_token_pools(address)
    except DexDataError as exc:
        await update.effective_message.reply_text(f"⚠ {exc}")
        return
    top = pools[0]
    score = liquidity_score(top)
    lines = [
        f"*{top.base_symbol}/{top.quote_symbol}* on `{top.chain}` ({top.dex})",
        f"Price: ${top.price_usd:,.8f}",
        f"Liquidity (TVL): ${top.liquidity_usd:,.0f}",
        f"24h volume: ${top.volume_24h_usd:,.0f}",
        f"24h change: {top.price_change_24h_pct:+.2f}%",
        f"Score: *{score}/100*",
        "",
        "*Est. impact:*",
    ]
    for size in config.TEST_TRADE_SIZES_USD:
        impact = estimate_price_impact_pct(top.liquidity_usd, size)
        flag = "thin" if impact >= 8 else "ok" if impact >= 2 else "deep"
        lines.append(f"${size:,} → {impact:.2f}% ({flag})")
    if top.url:
        lines.append(top.url)
    await update.effective_message.reply_text("\n".join(lines), parse_mode="Markdown")


def _parse_route_args(args: list[str]) -> tuple[str, float, str, int]:
    if not args:
        raise ValueError("Usage: /plan <ca> [usd] [buy|sell] [slices]")
    ca = args[0].strip()
    usd = float(args[1]) if len(args) > 1 else 5.0
    side = args[2].lower() if len(args) > 2 else "buy"
    slices = int(args[3]) if len(args) > 3 else 4
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    return ca, usd, side, slices


async def liq_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /liq <contract>")
        return
    await _report(update, context.args[0].strip())


async def plan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ca, usd, side, slices = _parse_route_args(context.args or [])
        plan = make_plan(ca, usd, side=side, slices=slices)
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(plan_text(plan))


async def route_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        ca, usd, side, slices = _parse_route_args(context.args or [])
        plan = make_plan(ca, usd, side=side, slices=slices)
    except Exception as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text("Running slices…")
    try:
        text = run_live_quotes(plan) if plan.live else run_paper(plan)
    except Exception as exc:
        text = str(exc)
    await update.effective_message.reply_text(text[:3500])


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if EVM_RE.match(text) or SOL_RE.match(text) or TON_RE.match(text):
        await _report(update, text)


def main() -> None:
    token = config.TELEGRAM_BOT_TOKEN
    if not token:
        raise SystemExit("Set LIQ_BOT_TOKEN — a NEW token, not the Trade Bot.")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("liq", liq_cmd))
    app.add_handler(CommandHandler("token", liq_cmd))
    app.add_handler(CommandHandler("liquidity", liq_cmd))
    app.add_handler(CommandHandler("plan", plan_cmd))
    app.add_handler(CommandHandler("route", route_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Liquidity desk starting live=%s", live_ok())
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
