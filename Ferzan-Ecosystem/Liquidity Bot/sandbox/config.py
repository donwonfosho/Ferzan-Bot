import os

TELEGRAM_BOT_TOKEN = (os.getenv("LIQ_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
# Separate token from Ferzan Trade Bot. Same token = both crash.
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID") or "0")
PAYWALL = (os.getenv("LIQ_PAYWALL") or "0").strip() in {"1", "true", "yes"}
TEST_TRADE_SIZES_USD = [100, 1_000, 10_000]
