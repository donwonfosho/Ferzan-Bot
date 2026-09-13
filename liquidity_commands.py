"""
liquidity_commands.py

/start_liquidity and /stop_liquidity commands connecting the Telegram bot
to TestnetMarketMaker. Each user's loop runs as its own asyncio task;
starting checks the user isn't already running one, stopping cancels the
task and lets TestnetMarketMaker's own cleanup (cancel open orders, close
exchange connection) run before confirming back to the user.
"""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

import credentials_db
from market_maker import TestnetMarketMaker

logger = logging.getLogger(__name__)

# user_id -> {"task": asyncio.Task, "maker": TestnetMarketMaker}
# In-memory by design -- if the bot process restarts, loops need to be
# restarted manually via /start_liquidity anyway (the exchange doesn't
# know about this dict, only about whatever orders are actually open).
_active_loops: dict[int, dict] = {}

# Tune per your risk tolerance / testnet inventory before going further
DEFAULT_ORDER_SIZE = 0.001
DEFAULT_SPREAD_FRACTION = 0.001
DEFAULT_MIN_REFRESH_S = 5
DEFAULT_MAX_REFRESH_S = 15

STOP_TIMEOUT_S = 30  # max time to wait for graceful shutdown before giving up


async def start_liquidity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if user_id in _active_loops:
        await update.message.reply_text(
            "You already have a liquidity loop running. Use /stop_liquidity first."
        )
        return

    creds = credentials_db.get_credentials(user_id)
    if not creds:
        await update.message.reply_text(
            "No API keys on file yet. Set them up first, then try again.\n"
            "(Wire this to whatever credential-collection flow you're using --"
            " e.g. a DM-only /set_api_keys command.)"
        )
        return

    maker = TestnetMarketMaker(
        exchange_id=creds.exchange_id,
        symbol=creds.symbol,
        api_key=creds.api_key,
        api_secret=creds.api_secret,
        order_size=DEFAULT_ORDER_SIZE,
        spread_fraction=DEFAULT_SPREAD_FRACTION,
        min_refresh_s=DEFAULT_MIN_REFRESH_S,
        max_refresh_s=DEFAULT_MAX_REFRESH_S,
        sandbox=True,
    )

    task = asyncio.create_task(maker.start())
    _active_loops[user_id] = {"task": task, "maker": maker}

    # If the loop dies on its own (unhandled exception inside start()),
    # clean up the registry entry and tell the user -- otherwise they'd
    # believe it's still quoting when it silently isn't.
    task.add_done_callback(lambda t: _handle_loop_finished(t, user_id, context))

    await update.message.reply_text(
        f"✅ Liquidity loop started for {creds.symbol} on {creds.exchange_id} (testnet).\n"
        "Use /stop_liquidity to stop it and cancel any open orders."
    )


def _handle_loop_finished(task: asyncio.Task, user_id: int, context: ContextTypes.DEFAULT_TYPE):
    """
    Done-callback for the background task. Runs synchronously when the
    task finishes for ANY reason -- normal cancellation via /stop_liquidity,
    or an unhandled crash. We only need to notify the user for the crash
    case; a clean /stop_liquidity already sends its own confirmation.
    """
    _active_loops.pop(user_id, None)

    if task.cancelled():
        return  # expected path -- /stop_liquidity already handles messaging

    exc = task.exception()
    if exc is not None:
        logger.error(f"Liquidity loop for user {user_id} crashed: {exc}", exc_info=exc)
        asyncio.create_task(
            context.bot.send_message(
                chat_id=user_id,
                text=f"⚠️ Your liquidity loop stopped unexpectedly: {exc}\n"
                     "Any open orders may still be resting on the exchange -- "
                     "check manually, then /start_liquidity to restart if needed.",
            )
        )


async def stop_liquidity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    entry = _active_loops.get(user_id)
    if not entry:
        await update.message.reply_text("No liquidity loop is currently running.")
        return

    task = entry["task"]
    await update.message.reply_text("Stopping loop and cancelling open orders...")

    task.cancel()
    try:
        # Awaiting the cancelled task lets TestnetMarketMaker.start()'s
        # `finally` block run to completion (cancel_all_open_orders +
        # exchange.close()) before we report back to the user.
        await asyncio.wait_for(task, timeout=STOP_TIMEOUT_S)
    except asyncio.CancelledError:
        pass  # expected -- this is exactly what we asked for
    except asyncio.TimeoutError:
        logger.error(f"Loop for user {user_id} did not shut down within {STOP_TIMEOUT_S}s")
        await update.message.reply_text(
            "⚠️ The loop didn't confirm shutdown in time. Open orders may still be "
            "resting on the exchange -- please check manually."
        )
        _active_loops.pop(user_id, None)
        return
    except Exception as e:
        # Shouldn't normally happen (start()'s own error handling should
        # catch exchange errors internally), but don't let it hide a
        # failed order-cancellation from the user.
        logger.error(f"Unexpected error stopping loop for user {user_id}: {e}", exc_info=e)
        await update.message.reply_text(
            f"⚠️ Error while stopping: {e}\nOpen orders may still be resting -- please check manually."
        )
        _active_loops.pop(user_id, None)
        return

    _active_loops.pop(user_id, None)
    await update.message.reply_text("✅ Loop stopped and open orders cancelled.")
