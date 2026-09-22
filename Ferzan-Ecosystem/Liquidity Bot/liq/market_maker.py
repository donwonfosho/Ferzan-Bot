"""
market_maker.py

Execution class for a testnet limit-order market-making loop using CCXT's
async support. Quotes a two-sided market around mid-price, refreshes on a
randomized interval, and enforces self-trade prevention so the bot's own
buy and sell orders can never cross and match each other.

NOTE: I can't execute this against a live testnet from where I'm running --
verify against your actual exchange/testnet with small order sizes before
trusting it unattended. Exchange-specific quirks (tick size, minimum order
size, param names for native STP) vary and are called out below where you
should double check them.
"""

import asyncio
import logging
import random

import ccxt.async_support as ccxt

logger = logging.getLogger(__name__)


class STPViolation(Exception):
    """Raised when a computed quote would cross the bot's own opposite-side quote."""


class TestnetMarketMaker:
    """
    One instance = one symbol on one exchange's testnet/sandbox.

    Each cycle:
      1. Fetch the current order book, compute mid-price.
      2. Compute a buy quote `spread_fraction` below mid and a sell quote
         `spread_fraction` above mid.
      3. Self-trade prevention: clamp quotes so they never cross each
         other OR the current market's opposite side, and use postOnly
         so an order that *would* cross gets rejected by the exchange
         rather than filled as a taker against yourself.
      4. Place both orders (with retry/backoff on transient errors).
      5. Sleep a random interval, then cancel whatever's still open and
         loop -- so quotes stay near the current market instead of
         going stale.
    """

    def __init__(
        self,
        exchange_id: str,
        symbol: str,
        api_key: str,
        api_secret: str,
        order_size: float,
        spread_fraction: float = 0.001,   # 0.1%
        min_refresh_s: float = 5.0,
        max_refresh_s: float = 15.0,
        sandbox: bool = True,
        extra_order_params: dict = None,
    ):
        exchange_class = getattr(ccxt, exchange_id)
        self.exchange = exchange_class({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,  # let ccxt pace requests to the exchange's documented limit
        })
        # Ferzan: testnet/sandbox only. Do not disable.
        self.exchange.set_sandbox_mode(True)

        self.symbol = symbol
        self.order_size = order_size
        self.spread_fraction = spread_fraction
        self.min_refresh_s = min_refresh_s
        self.max_refresh_s = max_refresh_s
        # postOnly is the generic cross-exchange way to say "reject this
        # order instead of letting it take liquidity" -- combine with your
        # exchange's native STP param here if it has one, e.g. Binance's
        # {'selfTradePreventionMode': 'EXPIRE_TAKER'}.
        self.extra_order_params = {"postOnly": True, **(extra_order_params or {})}

        self._running = False
        self.open_orders: dict = {}  # order_id -> order dict
        self._tick_size = None  # resolved from market precision on start()

    async def start(self):
        await self.exchange.load_markets()
        market = self.exchange.market(self.symbol)
        # Use the exchange's actual tick size rather than guessing --
        # falls back to a conservative default if precision isn't reported.
        precision = market.get("precision", {}).get("price")
        # `if precision else 1e-8` used to treat a real precision of 0 (a
        # legitimate whole-number tick size on some markets) as falsy, same
        # as "not reported" -- silently using a 1e-8 tick instead of 1.0 and
        # under-clamping quotes on those markets. `is not None` is the fix.
        self._tick_size = (10 ** -precision) if precision is not None else 1e-8

        self._running = True
        try:
            await self._run_loop()
        finally:
            await self._cancel_all_open_orders()
            await self.exchange.close()

    def stop(self):
        self._running = False

    # ---- core loop -------------------------------------------------

    async def _run_loop(self):
        while self._running:
            try:
                mid, best_bid, best_ask = await self._fetch_mid_price()
                buy_price, sell_price = self._compute_quotes(mid, best_bid, best_ask)
            except STPViolation as e:
                logger.warning(f"Skipping cycle -- {e}")
                await asyncio.sleep(self.min_refresh_s)
                continue
            except (ccxt.NetworkError, ValueError) as e:
                logger.warning(f"Could not read order book ({e}); retrying shortly")
                await asyncio.sleep(2)
                continue

            await self._place_order_with_retry("buy", buy_price)
            await self._place_order_with_retry("sell", sell_price)

            interval = random.uniform(self.min_refresh_s, self.max_refresh_s)
            logger.info(f"Quotes placed (buy {buy_price} / sell {sell_price}); holding {interval:.1f}s")
            await asyncio.sleep(interval)

            await self._cancel_all_open_orders()

    async def _fetch_mid_price(self):
        ob = await self.exchange.fetch_order_book(self.symbol, limit=5)
        if not ob["bids"] or not ob["asks"]:
            raise ValueError("Order book missing a bid or ask side")
        best_bid = ob["bids"][0][0]
        best_ask = ob["asks"][0][0]
        mid = (best_bid + best_ask) / 2
        return mid, best_bid, best_ask

    def _compute_quotes(self, mid: float, best_bid: float, best_ask: float):
        raw_buy = mid * (1 - self.spread_fraction)
        raw_sell = mid * (1 + self.spread_fraction)

        # STP, layer 1: never quote through the current opposite side of
        # the market (would make you a taker against someone else, or --
        # worse -- against your own resting order on a thin book).
        buy_price = min(raw_buy, best_ask - self._tick_size)
        sell_price = max(raw_sell, best_bid + self._tick_size)

        # STP, layer 2: never let our own two quotes cross each other.
        if buy_price >= sell_price:
            raise STPViolation(
                f"buy {buy_price} would cross sell {sell_price} (spread too tight "
                f"for current book width) -- widen spread_fraction or skip this cycle"
            )

        return round(buy_price, 10), round(sell_price, 10)

    # ---- order placement / cancellation -------------------------------------------------

    async def _place_order_with_retry(self, side: str, price: float, max_retries: int = 3):
        for attempt in range(1, max_retries + 1):
            try:
                order = await self.exchange.create_order(
                    self.symbol, "limit", side, self.order_size, price, self.extra_order_params
                )
                self.open_orders[order["id"]] = order
                return order

            except ccxt.RateLimitExceeded:
                wait = (self.exchange.rateLimit / 1000) * attempt
                logger.warning(f"Rate limited placing {side} order (attempt {attempt}); backing off {wait:.1f}s")
                await asyncio.sleep(wait)

            except ccxt.InsufficientFunds as e:
                logger.error(f"Insufficient funds for {side} order: {e}")
                return None  # not retryable without funding the account

            except ccxt.InvalidOrder as e:
                # Often means the postOnly order would have crossed and
                # was rejected -- exactly the STP behavior we want, so
                # log at info rather than error.
                logger.info(f"{side} order rejected (likely postOnly cross-prevention): {e}")
                return None

            except ccxt.NetworkError as e:
                logger.warning(f"Network error placing {side} order (attempt {attempt}): {e}")
                await asyncio.sleep(1 * attempt)

            except ccxt.ExchangeError as e:
                logger.error(f"Exchange error placing {side} order: {e}")
                return None

        logger.error(f"Giving up on {side} order after {max_retries} attempts")
        return None

    async def _cancel_all_open_orders(self):
        for order_id in list(self.open_orders):
            try:
                await self.exchange.cancel_order(order_id, self.symbol)
            except ccxt.OrderNotFound:
                pass  # already filled or already cancelled -- fine
            except ccxt.RateLimitExceeded:
                wait = self.exchange.rateLimit / 1000
                logger.warning(f"Rate limited cancelling {order_id}; backing off {wait:.1f}s")
                await asyncio.sleep(wait)
                try:
                    await self.exchange.cancel_order(order_id, self.symbol)
                except ccxt.OrderNotFound:
                    pass
                except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                    logger.warning(f"Cancel retry failed for {order_id}: {e}")
            except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                logger.warning(f"Error cancelling {order_id}: {e}")
            finally:
                self.open_orders.pop(order_id, None)
