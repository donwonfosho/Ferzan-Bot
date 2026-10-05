import asyncio, os, sqlite3, sys, tempfile, time, unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
TMP = tempfile.mkdtemp()
os.environ.update(BUYBOT_DB=f"{TMP}/buy.db", FEE_WALLET_SOL="TreasurySol1111111111111111111111111111111111",
                  FEE_WALLET_EVM="0x" + "ab" * 20, FERZAN_OWNER_IDS="99")
sys.modules["telegram"] = mock.MagicMock(); sys.modules["telegram.ext"] = mock.MagicMock()
sys.path.insert(0, str(ROOT))
import buy_bot as bb  # noqa: E402

EVM_TX = "0x" + "cd" * 32
EVM_TX2 = "0x" + "ef" * 32
SOL_TX = "5" * 87


class Msg:
    def __init__(self): self.sent = []
    async def reply_text(self, t, **k): self.sent.append(t)


def upd(uid, chat=None):
    m = Msg(); u = mock.MagicMock()
    u.effective_user.id = uid; u.effective_chat.id = chat or uid; u.effective_message = m
    return u, m


def ctx(*args):
    c = mock.MagicMock(); c.args = list(args); c.user_data = {}
    async def send(*a, **k): c.sent = getattr(c, "sent", []) + [a]
    c.bot.send_message = send
    return c


def run(co): return asyncio.run(co)


def reset():
    con = sqlite3.connect(os.environ["BUYBOT_DB"])
    for t in ("boosts", "boost_orders"):
        try: con.execute(f"DELETE FROM {t}")
        except sqlite3.OperationalError: pass
    con.commit(); con.close()
    bb._db().close()


class Units(unittest.TestCase):
    def test_amount_is_never_below_the_quote_and_carries_the_tag(self):
        for chain, quote in (("sol", 0.0821), ("sol", 2.7), ("base", 0.004), ("bsc", 0.0197), ("eth", 0.016667)):
            for tag in (1, 77, 4242, 9999):
                u = bb._exact_units(quote, chain, tag)
                dec = bb._pay_units(chain)[0]
                self.assertGreaterEqual(u / 10**dec, quote - 1e-12, (chain, quote, tag))
                self.assertLess(u / 10**dec - quote, 0.0021 if chain == "sol" else 0.00021)   # at most one grid step more
                self.assertEqual(bb._tag_of(u, chain), tag)

    def test_display_is_exact_and_short_enough_for_exchanges(self):
        self.assertEqual(bb._fmt_units(bb._exact_units(0.0821, "sol", 4242), "sol"), "0.0834242")
        self.assertEqual(len(bb._fmt_units(bb._exact_units(0.004, "base", 17), "base").split(".")[1]), 8)
        self.assertEqual(bb._fmt_units(bb._exact_units(0.004, "base", 17), "base"), "0.00400017")

    def test_payment_with_extra_dust_below_the_tag_digits_still_matches(self):
        u = bb._exact_units(0.05, "sol", 321) + 55      # <100 lamports of noise
        self.assertEqual(bb._tag_of(u, "sol"), 321)


class Orders(unittest.TestCase):
    def setUp(self): reset()

    def test_tags_are_unique_and_new_order_cancels_the_old(self):
        o1 = bb._open_order(1, "base", 0.01, {"kind": "trending"})
        o2 = bb._open_order(2, "base", 0.01, {"kind": "trending"})
        self.assertNotEqual(o1[1], o2[1])
        o1b = bb._open_order(1, "base", 0.01, {"kind": "trending"})
        self.assertEqual(bb._order_row(None, 1)["id"], o1b[0])
        self.assertEqual(bb._order_row(o1[0])["status"], "cancelled")

    def test_old_orders_expire(self):
        o = bb._open_order(1, "sol", 1.0, {})
        con = bb._db(); con.execute("UPDATE boost_orders SET created_ts=? WHERE id=?", (int(time.time()) - bb.ORDER_TTL_S - 5, o[0])); con.commit(); con.close()
        self.assertIsNone(bb._order_row(None, 1))

    def test_payload_survives_a_restart(self):
        o = bb._open_order(1, "sol", 1.0, {"kind": "raid", "hours": 24, "ca": "X"})
        self.assertEqual(bb._order_row(None, 1)["payload"]["ca"], "X")      # read from the database, not memory

    def test_match_rules(self):
        o = bb._open_order(1, "base", 0.01, {}); row = bb._order_row(o[0])
        now = int(time.time())
        self.assertIsNone(bb._order_problem(row, o[2], now))
        self.assertIn("before this order", bb._order_problem(row, o[2], now - 3600))
        self.assertIn("doesn't carry", bb._order_problem(row, o[2] + 10**10 * 3 if (o[1] + 3) <= 9999 else o[2] - 10**10 * 3, now))
        self.assertIn("Couldn't read", bb._order_problem(row, None, now))
        self.assertIn("before this order", bb._order_problem(row, o[2], None))
        self.assertIsNone(bb._order_problem(row, 10**15, now, check_tag=False))


class Paid(unittest.TestCase):
    def setUp(self):
        reset()
        self.facts = {}
        self.pt = [
            mock.patch.object(bb, "_verify_evm_tx", lambda tx, chain: (True, 0.0201, "")),
            mock.patch.object(bb, "_verify_sol_tx", lambda tx: (True, 2.7, "")),
            mock.patch.object(bb, "_payment_facts", lambda chain, tx: self.facts.get(tx, (None, None))),
            mock.patch.object(bb, "_get_usd_prices", lambda: (150.0, 3000.0)),
            mock.patch.object(bb, "_post_trending_board", mock.AsyncMock()),
            mock.patch.object(bb, "_post_board", mock.AsyncMock()),
        ]
        for p in self.pt: p.start()
        self.payload = {"kind": "trending", "chain": "base", "hours": 24, "usd": 12.0, "ca": "0xtoken", "cashtag": "$T", "url": ""}

    def tearDown(self):
        for p in self.pt: p.stop()

    def order(self, uid, chain="base", payload=None, quote=0.004):
        return bb._open_order(uid, chain, quote, payload or dict(self.payload, chain=chain))

    def pay(self, uid, tx, c=None):
        u, m = upd(uid); run(bb.paid_cmd(u, c or ctx(tx))); return m.sent[-1]

    def boosts(self): 
        con = bb._db(); n = con.execute("SELECT COUNT(*) FROM boosts").fetchone()[0]; con.close(); return n

    def test_matching_payment_activates_even_with_fresh_memory(self):
        oid, tag, units = self.order(1)
        self.facts[EVM_TX] = (units, int(time.time()) + 5)
        self.assertIn("Boost active", self.pay(1, EVM_TX))      # new ctx: nothing in user_data, the order is in the database
        self.assertEqual(self.boosts(), 1)
        self.assertIsNone(bb._order_row(None, 1))               # order is used up

    def test_someone_elses_payment_cannot_be_claimed(self):
        o1 = self.order(1)
        self.facts[EVM_TX] = (o1[2], int(time.time()) + 5)       # buyer 1 paid
        self.order(2, payload=dict(self.payload, ca="0xattacker"))   # attacker opened their own order first
        self.assertIn("doesn't carry", self.pay(2, EVM_TX))
        self.assertEqual(self.boosts(), 0)
        self.assertIn("Boost active", self.pay(1, EVM_TX))      # the real buyer still gets it
        self.assertEqual(self.boosts(), 1)

    def test_old_treasury_payments_cannot_be_claimed(self):
        o = self.order(1)
        self.facts[EVM_TX] = (o[2], int(time.time()) - 86400)     # same digits, but made yesterday
        self.assertIn("before this order", self.pay(1, EVM_TX))
        self.assertEqual(self.boosts(), 0)

    def test_one_payment_one_boost(self):
        o = self.order(1); self.facts[EVM_TX] = (o[2], int(time.time()) + 5)
        self.assertIn("Boost active", self.pay(1, EVM_TX))
        o2 = self.order(1); 
        self.assertIn("already been used", self.pay(1, EVM_TX))
        self.assertEqual(self.boosts(), 1)

    def test_no_order_no_boost(self):
        self.facts[EVM_TX] = (10**16, int(time.time()))
        self.assertIn("no pending boost", self.pay(5, EVM_TX))

    def test_junk_hash_rejected_before_any_lookup(self):
        with mock.patch.object(bb, "_verify_evm_tx", side_effect=AssertionError("called")):
            self.assertIn("doesn't look like", self.pay(1, "12345"))

    def test_case_variants_are_one_payment(self):
        o = self.order(1); up = "0x" + "CD" * 32
        self.facts[EVM_TX] = (o[2], int(time.time()) + 5)
        self.assertIn("Boost active", self.pay(1, up))
        self.order(2)
        self.assertIn("already been used", self.pay(2, up))

    def test_solana_ads_order(self):
        pl = {"kind": "ads", "chain": "sol", "hours": 24, "native_amt": 2.7, "ca": "", "cashtag": "x", "url": "https://x.io"}
        oid, tag, units = bb._open_order(1, "sol", 2.7, pl)
        self.facts[SOL_TX] = (units, int(time.time()) + 3)
        self.assertGreaterEqual(units, 2_700_000_000)
        self.assertIn("Boost active", self.pay(1, SOL_TX))

    def test_owner_claim_overrides_only_the_reference_digits(self):
        o = self.order(1)
        self.facts[EVM_TX] = (10**16 + 123, int(time.time()) + 5)     # exchange cut the decimals: no tag
        self.assertIn("doesn't carry", self.pay(1, EVM_TX))
        u, m = upd(7); run(bb.claimorder_cmd(u, ctx(str(o[0]), EVM_TX)))        # not an owner: ignored
        self.assertEqual(m.sent, []); self.assertEqual(self.boosts(), 0)
        u, m = upd(99); c = ctx(str(o[0]), EVM_TX); run(bb.claimorder_cmd(u, c))
        self.assertIn("Boost active", m.sent[-1]); self.assertEqual(self.boosts(), 1)
        self.facts[EVM_TX2] = (10**16, int(time.time()) - 86400)
        o2 = self.order(2); u, m = upd(99); run(bb.claimorder_cmd(u, ctx(str(o2[0]), EVM_TX2)))
        self.assertIn("before this order", m.sent[-1])                 # even the owner cannot claim old payments

    def test_instructions_show_the_exact_amount(self):
        _o, _t, units = self.order(1, quote=0.004)
        txt = bb._pay_instruction(units, "base", "ETH", "0xWALLET")
        self.assertIn(bb._fmt_units(units, "base"), txt); self.assertIn("don't round", txt)


class Wiring(unittest.TestCase):
    def test_every_payment_prompt_opens_an_order(self):
        src = (ROOT / "buy_bot.py").read_text()
        self.assertEqual(src.count("await _quote_order("), 4)
        self.assertNotIn("Pay to:\\n<code>{html.escape(wallet", src)
        self.assertIn('CommandHandler("claimorder"', src)


if __name__ == "__main__":
    unittest.main()
