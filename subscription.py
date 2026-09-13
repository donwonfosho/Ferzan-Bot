"""
subscription.py

Lightweight SQLite-backed premium subscription tracking for a Telegram bot,
plus a decorator that gates any command/callback handler behind premium
status -- non-premium (or expired) users get routed to a payment prompt
instead of the handler running.

Usage:
    import subscription
    subscription.init_db()

    @subscription.premium_required
    async def some_command(update, context):
        ...  # only runs for active premium users
"""

import sqlite3
import functools
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

DB_PATH = os.environ.get("SUBSCRIPTION_DB_PATH", "subscriptions.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER PRIMARY KEY,
    is_premium INTEGER NOT NULL DEFAULT 0,
    expires_at TEXT
);
"""

# Callback data for the "Upgrade" button shown in the payment prompt.
# Wire a CallbackQueryHandler to this in your bot's setup to actually
# start a checkout/payment flow.
CB_UPGRADE = "subscription:upgrade"

DEFAULT_PAYMENT_PROMPT = (
    "🔒 This is a premium feature.\n\n"
    "Upgrade to unlock it — tap below to get started."
)


@contextmanager
def _get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _get_conn() as conn:
        conn.executescript(SCHEMA)


@dataclass
class SubscriptionStatus:
    user_id: int
    is_premium: bool
    expires_at: Optional[datetime]


def get_status(user_id: int) -> SubscriptionStatus:
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT is_premium, expires_at FROM subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    if not row:
        return SubscriptionStatus(user_id=user_id, is_premium=False, expires_at=None)

    expires_at = datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None
    is_premium = bool(row["is_premium"])

    # Lazily expire: if the stored flag says premium but the timestamp has
    # passed, treat as expired and correct the stored flag so future reads
    # don't need to re-derive this.
    if is_premium and expires_at and expires_at < datetime.now(timezone.utc):
        _set_premium_flag(user_id, False)
        is_premium = False

    return SubscriptionStatus(user_id=user_id, is_premium=is_premium, expires_at=expires_at)


def is_premium(user_id: int) -> bool:
    return get_status(user_id).is_premium


def grant_premium(user_id: int, days: int) -> datetime:
    """
    Grants (or extends) premium access. If the user already has time
    remaining, extends from their current expiry rather than from now --
    so topping up doesn't waste already-paid-for time.
    """
    current = get_status(user_id)
    now = datetime.now(timezone.utc)
    start = current.expires_at if (current.expires_at and current.expires_at > now) else now
    new_expiry = start + timedelta(days=days)

    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO subscriptions (user_id, is_premium, expires_at)
               VALUES (?, 1, ?)
               ON CONFLICT(user_id) DO UPDATE SET is_premium = 1, expires_at = excluded.expires_at""",
            (user_id, new_expiry.isoformat()),
        )
    return new_expiry


def revoke_premium(user_id: int):
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO subscriptions (user_id, is_premium, expires_at)
               VALUES (?, 0, NULL)
               ON CONFLICT(user_id) DO UPDATE SET is_premium = 0, expires_at = NULL""",
            (user_id,),
        )


def _set_premium_flag(user_id: int, value: bool):
    with _get_conn() as conn:
        conn.execute(
            """INSERT INTO subscriptions (user_id, is_premium, expires_at)
               VALUES (?, ?, NULL)
               ON CONFLICT(user_id) DO UPDATE SET is_premium = ?""",
            (user_id, int(value), int(value)),
        )


def _payment_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⭐ Upgrade to Premium", callback_data=CB_UPGRADE)]]
    )


async def _send_payment_prompt(update: Update, text: str):
    keyboard = _payment_prompt_keyboard()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(text, reply_markup=keyboard)
    else:
        await update.message.reply_text(text, reply_markup=keyboard)


def premium_required(handler=None, *, prompt: str = DEFAULT_PAYMENT_PROMPT):
    """
    Decorator for command/callback handlers. Checks premium status before
    running the wrapped handler; if the user isn't premium (or expired),
    sends a payment prompt instead and does not call the handler.

    Works as either @premium_required or @premium_required(prompt="...")
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user_id = update.effective_user.id
            if is_premium(user_id):
                return await func(update, context, *args, **kwargs)
            await _send_payment_prompt(update, prompt)
            return None

        return wrapper

    if handler is not None:
        return decorator(handler)  # used as @premium_required with no args
    return decorator  # used as @premium_required(prompt="...")
