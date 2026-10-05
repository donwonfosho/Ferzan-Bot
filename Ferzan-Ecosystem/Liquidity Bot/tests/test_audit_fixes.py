import asyncio, os, sys, types, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# a tiny stand-in for ccxt so the market maker can be exercised without the real package
_cc = types.ModuleType("ccxt"); _as = types.ModuleType("ccxt.async_support")
class _E(Exception): pass
for n, base in (("BaseError", _E),):
    setattr(_as, n, base)
class NetworkError(_E): pass
class ExchangeError(_E): pass
class RateLimitExceeded(NetworkError): pass
class InsufficientFunds(ExchangeError): pass
class InvalidOrder(ExchangeError): pass
class OrderNotFound(ExchangeError): pass
for c in (NetworkError, ExchangeError, RateLimitExceeded, InsufficientFunds, InvalidOrder, OrderNotFound):
    setattr(_as, c.__name__, c)
_as.TICK_SIZE, _as.DECIMAL_PLACES = 4, 2
class _Ex:
    rateLimit = 1
    precisionMode = 4
    def __init__(self, *_a, **_k): pass
    def set_sandbox_mode(self, *_): pass
_as.fake = _Ex
sys.modules["ccxt"], sys.modules["ccxt.async_support"] = _cc, _as
import liq.market_maker as mm  # noqa: E402


class Fake(_Ex):
    def __init__(self):
        self.created, self.cancel_results, self.open = [], [], []
        self.raise_on_create = None
    async def create_order(self, symbol, kind, side, size, price, params=None):
        if self.raise_on_create:
            e, self.raise_on_create = self.raise_on_create, None
            raise e
        o = {"id": f"o{len(self.created)+1}", "side": side, "price": price, "amount": size}
        self.created.append(o); self.open.append(o)
        return o
    async def fetch_open_orders(self, symbol): return list(self.open)
    async def cancel_order(self, oid, symbol):
        r = self.cancel_results.pop(0) if self.cancel_results else None
        if r: raise r
    async def close(self): pass


def maker():
    m = mm.TestnetMarketMaker.__new__(mm.TestnetMarketMaker)
    m.exchange, m.symbol, m.order_size, m.open_orders, m._tick_size = Fake(), "X/USDT", 1.0, {}, 0.01
    m.extra_order_params = {}
    return m


class Orders(unittest.TestCase):
    def test_timeout_after_the_exchange_accepted_does_not_double_place(self):
        m = maker()
        async def run():
            # first call: order reaches the exchange, then the connection drops
            real = m.exchange.create_order
            async def flaky(*a, **k):
                await real(*a, **k)
                raise mm.ccxt.NetworkError("timeout")
            m.exchange.create_order = flaky
            return await m._place_order_with_retry("buy", 10.0)
        o = asyncio.run(run())
        self.assertEqual(len(m.exchange.created), 1)          # placed once, not twice
        self.assertEqual(o["id"], "o1")
        self.assertIn("o1", m.open_orders)                    # and we track it

    def test_timeout_before_the_exchange_saw_it_retries(self):
        m = maker(); m.exchange.raise_on_create = mm.ccxt.NetworkError("down")
        o = asyncio.run(m._place_order_with_retry("sell", 11.0))
        self.assertEqual(len(m.exchange.created), 1); self.assertEqual(o["side"], "sell")

    def test_cannot_check_means_no_blind_retry(self):
        m = maker(); m.exchange.raise_on_create = mm.ccxt.NetworkError("down")
        async def boom(_): raise mm.ccxt.NetworkError("still down")
        m.exchange.fetch_open_orders = boom
        self.assertIsNone(asyncio.run(m._place_order_with_retry("buy", 10.0)))
        self.assertEqual(m.exchange.created, [])

    def test_failed_cancel_keeps_the_order_id(self):
        m = maker(); m.open_orders = {"a": {}, "b": {}}
        m.exchange.cancel_results = [mm.ccxt.NetworkError("x"), None]
        asyncio.run(m._cancel_all_open_orders())
        self.assertEqual(list(m.open_orders), ["a"])           # b cancelled, a still tracked

    def test_already_gone_order_is_forgotten(self):
        m = maker(); m.open_orders = {"a": {}}
        m.exchange.cancel_results = [mm.ccxt.OrderNotFound("x")]
        asyncio.run(m._cancel_all_open_orders())
        self.assertEqual(m.open_orders, {})


class Source(unittest.TestCase):
    def test_tick_size_mode_is_respected(self):
        self.assertIn("precisionMode", (ROOT / "liq/market_maker.py").read_text())

    def test_solana_mm_insert_is_valid_sql(self):
        import sqlite3
        src = (ROOT / "liq/solana_mm.py").read_text()
        line = [l for l in src.splitlines() if "INSERT INTO mm_trades" in l][0]
        sql = line.split('"')[1]
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE mm_trades (session_id, side, ts, usd, tx_hash, ok, note)")
        c.execute(sql, (1, "buy", 1.0, 2.0, "sig", 1, "n"))

    def test_mmw_and_upgrade_buttons_are_routed(self):
        bot = (ROOT / "liq_bot.py").read_text()
        self.assertIn('pattern=r"^(liq|mmw):"', bot)
        self.assertIn('CB_UPGRADE = "liq:upgrade"', (ROOT / "liq/subscription.py").read_text())
        self.assertIn('data == "liq:upgrade"', bot)


if __name__ == "__main__":
    unittest.main()
