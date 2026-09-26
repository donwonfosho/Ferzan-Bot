"""
curve_indexer.py -- follows Ferzan bonding-curve factories on BNB Chain and Base.

Reads only public chain data (eth_getLogs / eth_call) and writes a small SQLite
index that the API uses for the trade-page chart, the New launches feed,
King of the Hill, top volume and creator track records. It also posts the
graduation alert to the creator's chat and the launches channel.

Holds no keys and sends no transactions. Run as its own service:
    python curve_indexer.py            (loops forever)
    python curve_indexer.py --once     (one pass, then exit - for checks)
"""
from __future__ import annotations

import html
import json
import logging
import os
import sqlite3
import sys
import time

import requests

try:  # same env files as the launch bot (values already set by systemd win)
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv("/opt/ferzan/.env")
    load_dotenv(Path(__file__).resolve().with_name(".env"))
except ImportError:
    pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("curve_indexer")

LAUNCH_DB = os.environ.get("LAUNCH_DB_PATH") or "/opt/ferzan/app/launch/launch_bot.db"
INDEX_DB = os.environ.get("CURVE_INDEX_DB") or os.path.join(os.path.dirname(LAUNCH_DB) or ".", "curve_index.db")

T_LAUNCHED = "0x188ae4cd8aa7c0376e9501e76fb7a19dd1454add5c88bffbf75f391807a14475"
T_TRADE = "0xf13ff38259ab955eb95db2db3b0c9e4e41db64e91a47c1d463a86970563e4b08"
T_GRAD = "0x71aa8c3702dbbc54247a4e6446450e64a46250dd72707a247ddb01d72eaef445"

CHAINS = {
    "bsc": {
        "factory_env": "FACTORY_BSC_CURVE", "rpc_env": "BSC_RPC_URL", "sym": "BNB", "dex": "PancakeSwap",
        "fallback": ["https://bsc-rpc.publicnode.com", "https://bsc-dataseed.binance.org"],
        "block_time": 0.75, "explorer": "https://bscscan.com",
    },
    "base": {
        "factory_env": "FACTORY_BASE_CURVE", "rpc_env": "BASE_RPC_URL", "sym": "ETH", "dex": "Uniswap",
        "fallback": ["https://base-rpc.publicnode.com", "https://mainnet.base.org"],
        "block_time": 2.0, "explorer": "https://basescan.org",
    },
}
CONFIRMATIONS = 2
# Free public nodes only keep recent history ("archive requests require a token"), so we follow
# the chain from near its head and read older curves' current state straight from the contracts.
RECENT_BLOCKS = int(os.environ.get("CURVE_INDEX_RECENT_BLOCKS") or "100")

SCHEMA = """
CREATE TABLE IF NOT EXISTS curves (
    chain TEXT NOT NULL, curve TEXT NOT NULL, token TEXT NOT NULL, creator TEXT NOT NULL,
    name TEXT, symbol TEXT, total_supply TEXT, curve_supply TEXT, grad_target TEXT,
    v_eth TEXT, v_token TEXT, start_time INTEGER, pool TEXT,
    real_eth TEXT DEFAULT '0', tokens_sold TEXT DEFAULT '0', price REAL, mcap REAL,
    volume REAL DEFAULT 0, trades INTEGER DEFAULT 0, buys INTEGER DEFAULT 0, sells INTEGER DEFAULT 0,
    launched_block INTEGER, launched_ts INTEGER, last_trade_ts INTEGER,
    graduated INTEGER DEFAULT 0, grad_ts INTEGER, grad_native REAL, grad_notified INTEGER DEFAULT 0,
    PRIMARY KEY (chain, curve)
);
CREATE TABLE IF NOT EXISTS trades (
    chain TEXT NOT NULL, curve TEXT NOT NULL, block INTEGER NOT NULL, ts INTEGER NOT NULL,
    tx TEXT NOT NULL, log_index INTEGER NOT NULL, trader TEXT, is_buy INTEGER,
    native REAL, tokens REAL, price REAL, real_eth TEXT,
    PRIMARY KEY (chain, tx, log_index)
);
CREATE INDEX IF NOT EXISTS idx_trades_curve ON trades (curve, ts);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades (ts);
CREATE INDEX IF NOT EXISTS idx_curves_creator ON curves (creator);
CREATE TABLE IF NOT EXISTS cursor (chain TEXT PRIMARY KEY, block INTEGER NOT NULL, rpc TEXT);
"""


def idx_conn() -> sqlite3.Connection:
    c = sqlite3.connect(INDEX_DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


def init_db() -> None:
    with idx_conn() as c:
        c.executescript(SCHEMA)


# ------------------------------------------------------------------ rpc --
class Rpc:
    def __init__(self, urls: list[str]):
        self.urls = [u for u in urls if u]
        self.i = 0
        self.s = requests.Session()

    @property
    def url(self) -> str:
        return self.urls[self.i % len(self.urls)]

    def call(self, method: str, params: list, rotate: bool = True):
        last = None
        for _ in range(len(self.urls) if rotate else 1):
            try:
                r = self.s.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=25)
                body = r.json()
                if "error" in body:
                    raise RuntimeError(str(body["error"].get("message") if isinstance(body["error"], dict) else body["error"]))
                return body.get("result")
            except (requests.RequestException, ValueError) as e:
                last = e
                self.i += 1  # network trouble: try the next endpoint
        raise RuntimeError(f"rpc {method} failed: {last}")


def _word(hexdata: str, i: int) -> int:
    h = hexdata[2:] if hexdata.startswith("0x") else hexdata
    return int(h[i * 64:(i + 1) * 64] or "0", 16)


def _addr_topic(t: str) -> str:
    return "0x" + t[-40:].lower()


def _call_uint(rpc: Rpc, to: str, sel: str) -> int:
    res = rpc.call("eth_call", [{"to": to, "data": sel}, "latest"])
    return int(res, 16) if res and res != "0x" else 0


def _call_str(rpc: Rpc, to: str, sel: str) -> str:
    try:
        res = rpc.call("eth_call", [{"to": to, "data": sel}, "latest"])
        h = res[2:]
        if len(h) >= 128:
            n = int(h[64:128], 16)
            return bytes.fromhex(h[128:128 + n * 2]).decode("utf-8", "replace")[:64]
        return bytes.fromhex(h).rstrip(b"\x00").decode("utf-8", "replace")[:64]
    except Exception:
        return ""


def pick_rpc(chain: str, factory: str) -> Rpc | None:
    """Endpoints that answer eth_getLogs for our contracts (many public nodes refuse it)."""
    cfg = CHAINS[chain]
    urls = [os.environ.get(cfg["rpc_env"], "").strip()] + cfg["fallback"]
    good = []
    for u in [u for u in urls if u]:
        try:
            r = Rpc([u])
            head = int(r.call("eth_blockNumber", [], rotate=False), 16)
            r.call("eth_getLogs", [{"fromBlock": hex(head - 20), "toBlock": hex(head), "address": factory,
                                    "topics": [T_LAUNCHED]}], rotate=False)
            good.append(u)
        except Exception as e:
            log.info("%s: %s cannot serve logs (%s)", chain, u.split("//")[-1].split("/")[0], str(e)[:80])
    return Rpc(good) if good else None


# --------------------------------------------------------------- indexing --
class ChainIndexer:
    def __init__(self, chain: str):
        self.chain = chain
        self.cfg = CHAINS[chain]
        self.factory = (os.environ.get(self.cfg["factory_env"]) or "").strip().lower()
        self.factories = [self.factory] if len(self.factory) == 42 else []
        try:  # every curve factory ever deployed on this chain (v2 + v3), from the deploy record
            rec = json.loads(open("/opt/ferzan/dbc-keys/evm-factories.json").read())
            for k, v in rec.items():
                if k.startswith(f"{chain}_curve") and str(v).lower() not in self.factories:
                    self.factories.append(str(v).lower())
        except (OSError, ValueError):
            pass
        self.rpc: Rpc | None = None
        self.step = 1000
        self.max_step = 2000  # lowered for good when a node says its range limit is smaller
        self.known: dict[str, dict] = {}
        self.ts_cache: dict[int, int] = {}

    def ready(self) -> bool:
        if not self.factory.startswith("0x") or len(self.factory) != 42:
            return False
        if self.rpc is None:
            self.rpc = pick_rpc(self.chain, self.factory)
            if self.rpc is None:
                log.warning("%s: no RPC endpoint serves logs - set %s", self.chain, self.cfg["rpc_env"])
                return False
            with idx_conn() as c:
                for r in c.execute("SELECT * FROM curves WHERE chain = ?", (self.chain,)):
                    self.known[r["curve"]] = dict(r)
            self.seed_from_launch_db()
        return True

    def seed_from_launch_db(self) -> None:
        """Curves the bot launched before we were following the chain: read their state now."""
        try:
            lc = sqlite3.connect(LAUNCH_DB, timeout=30)
            rows = lc.execute(
                "SELECT extra_params, created_at FROM launch_requests WHERE chain = ? AND mode = 'bonding_curve' "
                "AND status = 'confirmed'", (self.chain,)).fetchall()
            lc.close()
        except sqlite3.Error as e:
            log.info("%s: launch-db read skipped (%s)", self.chain, e)
            return
        from datetime import datetime as _dt
        for extra, created in rows:
            try:
                curve = (json.loads(extra or "{}").get("curve_address") or "").lower()
            except ValueError:
                continue
            if not curve.startswith("0x") or len(curve) != 42 or curve in self.known:
                continue
            try:
                r = self.rpc
                factory = "0x" + format(_call_uint(r, curve, "0xc45a0155"), "040x")  # factory()
                if factory not in self.factories:
                    continue  # not one of ours on this chain
                token = "0x" + format(_call_uint(r, curve, "0xfc0c546a"), "040x")
                creator = "0x" + format(_call_uint(r, curve, "0x02d05d3f"), "040x")
                try:
                    ts = int(_dt.fromisoformat(str(created).replace("Z", "+00:00")).timestamp())
                except ValueError:
                    ts = int(time.time())
                with idx_conn() as c:
                    self.register_curve(curve, token, creator, 0, ts, c)
                    real = _call_uint(r, curve, "0x7a2a2a2b")
                    sold = _call_uint(r, curve, "0x518ab2a8")
                    grad = _call_uint(r, curve, "0xe7c2b772") == 1
                    cv = self.known[curve]
                    v_eth, v_tok = int(cv["v_eth"]), int(cv["v_token"])
                    price = (v_eth + real) / (v_tok - sold) if v_tok > sold else 0.0
                    if grad:  # after graduation realEth is 0: use the final curve price
                        price = (v_eth + int(cv["grad_target"])) / (v_tok - sold) if v_tok > sold else 0.0
                        real = int(cv["grad_target"])
                    c.execute(
                        "UPDATE curves SET real_eth = ?, tokens_sold = ?, price = ?, mcap = ?, graduated = ?, "
                        "grad_notified = 1 WHERE chain = ? AND curve = ?",
                        (str(real), str(sold), price, price * int(cv["total_supply"]) / 1e18, 1 if grad else 0,
                         self.chain, curve))
            except Exception as e:
                log.info("%s: could not read curve %s (%s)", self.chain, curve, str(e)[:80])

    def block_ts(self, n: int) -> int:
        if n not in self.ts_cache:
            b = self.rpc.call("eth_getBlockByNumber", [hex(n), False])
            self.ts_cache[n] = int(b["timestamp"], 16)
            if len(self.ts_cache) > 5000:
                self.ts_cache.clear()
        return self.ts_cache[n]

    def start_block(self, head: int) -> int:
        with idx_conn() as c:
            row = c.execute("SELECT block FROM cursor WHERE chain = ?", (self.chain,)).fetchone()
        if row:
            return int(row["block"]) + 1
        env = os.environ.get(f"CURVE_INDEX_FROM_{self.chain.upper()}")
        if env:
            return int(env)
        return max(head - RECENT_BLOCKS, 1)

    def get_logs(self, frm: int, to: int, **flt) -> list:
        return self.rpc.call("eth_getLogs", [{"fromBlock": hex(frm), "toBlock": hex(to), **flt}])

    def register(self, lg: dict, c: sqlite3.Connection) -> None:
        blk = int(lg["blockNumber"], 16)
        self.register_curve(_addr_topic(lg["topics"][1]), _addr_topic(lg["topics"][2]),
                            _addr_topic(lg["topics"][3]), blk, self.block_ts(blk), c)

    def register_curve(self, curve: str, token: str, creator: str, blk: int, ts: int, c: sqlite3.Connection) -> None:
        if curve in self.known:
            return
        r = self.rpc
        info = {
            "chain": self.chain, "curve": curve, "token": token, "creator": creator,
            "name": _call_str(r, token, "0x06fdde03"), "symbol": _call_str(r, token, "0x95d89b41"),
            "total_supply": str(_call_uint(r, token, "0x18160ddd")),
            "curve_supply": str(_call_uint(r, curve, "0x2138a4c0")),
            "grad_target": str(_call_uint(r, curve, "0x9a3a8ee1")),
            "v_eth": str(_call_uint(r, curve, "0x4bd387e1")),
            "v_token": str(_call_uint(r, curve, "0x1f514c52")),
            "start_time": _call_uint(r, curve, "0x78e97925"),
            "pool": "0x" + format(_call_uint(r, curve, "0x16f0115b"), "040x"),
            "launched_block": blk,
            "launched_ts": ts,
        }
        v_eth, v_tok = int(info["v_eth"]), int(info["v_token"])
        info["price"] = v_eth / v_tok if v_tok else 0.0
        info["mcap"] = info["price"] * int(info["total_supply"]) / 1e18
        cols = ",".join(info)
        c.execute(f"INSERT OR IGNORE INTO curves ({cols}) VALUES ({','.join('?' * len(info))})", tuple(info.values()))
        self.known[curve] = info
        log.info("%s: new curve %s (%s)", self.chain, curve, info["symbol"])

    def on_trade(self, lg: dict, c: sqlite3.Connection) -> None:
        curve = lg["address"].lower()
        cv = self.known.get(curve)
        if not cv:
            return  # only curves our factory created
        d = lg["data"]
        is_buy = _word(d, 0) == 1
        native, tokens, real_after, sold_after = _word(d, 1), _word(d, 2), _word(d, 5), _word(d, 6)
        v_eth, v_tok = int(cv["v_eth"]), int(cv["v_token"])
        price = (v_eth + real_after) / (v_tok - sold_after) if v_tok > sold_after else 0.0
        blk = int(lg["blockNumber"], 16)
        ts = self.block_ts(blk)
        cur = c.execute(
            "INSERT OR IGNORE INTO trades (chain, curve, block, ts, tx, log_index, trader, is_buy, native, tokens, price, real_eth) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.chain, curve, blk, ts, lg["transactionHash"], int(lg["logIndex"], 16), _addr_topic(lg["topics"][1]),
             1 if is_buy else 0, native / 1e18, tokens / 1e18, price, str(real_after)),
        )
        if cur.rowcount == 0:
            return  # already indexed
        mcap = price * int(cv["total_supply"]) / 1e18
        c.execute(
            "UPDATE curves SET real_eth = ?, tokens_sold = ?, price = ?, mcap = ?, volume = volume + ?, trades = trades + 1, "
            "buys = buys + ?, sells = sells + ?, last_trade_ts = ? WHERE chain = ? AND curve = ?",
            (str(real_after), str(sold_after), price, mcap, native / 1e18, 1 if is_buy else 0, 0 if is_buy else 1, ts,
             self.chain, curve),
        )

    def on_grad(self, lg: dict, c: sqlite3.Connection) -> None:
        curve = lg["address"].lower()
        if curve not in self.known:
            return
        ts = self.block_ts(int(lg["blockNumber"], 16))
        c.execute(
            "UPDATE curves SET graduated = 1, grad_ts = ?, grad_native = ?, pool = ? WHERE chain = ? AND curve = ? AND graduated = 0",
            (ts, _word(lg["data"], 0) / 1e18, _addr_topic(lg["topics"][1]), self.chain, curve),
        )
        log.info("%s: %s graduated", self.chain, curve)

    def run_once(self, max_ranges: int = 40) -> int:
        if not self.ready():
            return 0
        head = int(self.rpc.call("eth_blockNumber", []), 16) - CONFIRMATIONS
        frm = self.start_block(head)
        done = 0
        while frm <= head and done < max_ranges:
            to = min(head, frm + self.step - 1)
            try:
                launched = self.get_logs(frm, to, address=self.factories, topics=[T_LAUNCHED])
                if launched:
                    with idx_conn() as c:
                        for lg in launched:
                            self.register(lg, c)
                events = []
                addrs = list(self.known)
                for i in range(0, len(addrs), 50):  # public nodes want explicit addresses
                    events += self.get_logs(frm, to, address=addrs[i:i + 50], topics=[[T_TRADE, T_GRAD]])
            except RuntimeError as e:
                msg = str(e).lower()
                if "archive" in msg or "missing trie" in msg or "pruned" in msg:
                    new_from = max(frm, head - RECENT_BLOCKS)
                    if new_from > frm:
                        log.warning("%s: node has no history for blocks %d-%d, skipping ahead", self.chain, frm, new_from - 1)
                        frm = new_from
                        continue
                if "rate" in msg or "429" in msg or "too many requests" in msg:
                    log.info("%s: node is rate limiting, pausing", self.chain)
                    time.sleep(8)
                    return done
                if self.step > 50:
                    self.step = max(50, self.step // 2)
                    self.max_step = self.step
                    log.info("%s: smaller log range %d (%s)", self.chain, self.step, str(e)[:80])
                    continue
                raise
            events.sort(key=lambda x: (int(x["blockNumber"], 16), int(x["logIndex"], 16)))
            with idx_conn() as c:
                for lg in events:
                    if lg.get("removed"):
                        continue
                    if lg["topics"][0] == T_TRADE:
                        self.on_trade(lg, c)
                    elif lg["topics"][0] == T_GRAD:
                        self.on_grad(lg, c)
                c.execute(
                    "INSERT INTO cursor (chain, block, rpc) VALUES (?, ?, ?) ON CONFLICT(chain) DO UPDATE SET block = excluded.block, rpc = excluded.rpc",
                    (self.chain, to, self.rpc.url.split("//")[-1].split("/")[0]),
                )
            done += 1
            frm = to + 1
            if self.step < self.max_step:
                self.step = min(self.max_step, self.step * 2)
            time.sleep(0.25)  # stay polite to free public nodes
        return done


# ----------------------------------------------------------------- alerts --
def _tg(chat_id, text: str) -> None:
    token = os.environ.get("LAUNCHBOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    if not token or not chat_id:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=15)
    except requests.RequestException as e:
        log.warning("telegram send failed: %s", e)


def _creator_chats() -> dict:
    out = {}
    try:
        lc = sqlite3.connect(LAUNCH_DB, timeout=30)
        for extra, chat in lc.execute(
                "SELECT extra_params, chat_id FROM launch_requests WHERE mode = 'bonding_curve' AND status = 'confirmed'"):
            try:
                cv = (json.loads(extra or "{}").get("curve_address") or "").lower()
            except ValueError:
                cv = ""
            if cv:
                out[cv] = chat
        lc.close()
    except sqlite3.Error:
        pass
    return out


def send_graduation_alerts() -> None:
    with idx_conn() as c:
        rows = c.execute("SELECT * FROM curves WHERE graduated = 1 AND grad_notified = 0").fetchall()
        if not rows:
            return
        chats = _creator_chats()
        channel = (os.environ.get("FERZAN_LAUNCHES_CHANNEL") or "").strip()
        base = (os.environ.get("MINI_APP_BASE_URL") or "https://launch.ferzaneco.com/miniapp").rstrip("/")
        for r in rows:
            c.execute("UPDATE curves SET grad_notified = 1 WHERE chain = ? AND curve = ?", (r["chain"], r["curve"]))
            if (r["grad_ts"] or 0) < time.time() - 6 * 3600:
                continue  # found while catching up on old blocks - don't announce stale news
            cfg = CHAINS[r["chain"]]
            name, sym = html.escape(r["name"] or "Token"), html.escape(r["symbol"] or "")
            raised = f"{(r['grad_native'] or 0):.4g}"
            chart = f"https://dexscreener.com/{'bsc' if r['chain'] == 'bsc' else 'base'}/{r['token']}"
            text = (
                f"🎓 <b>{name} (${sym}) just graduated!</b>\n\n"
                f"The curve filled at {raised} {cfg['sym']}. Liquidity is now on {cfg['dex']} "
                f"and the LP tokens are burned 🔥\n\n"
                f"<code>{r['token']}</code>\n"
                f'<a href="{chart}">📊 Chart</a> · <a href="{base}/curve.html?chain={r["chain"]}&curve={r["curve"]}">Trade page</a>'
            )
            _tg(chats.get(r["curve"]), text)
            if channel:
                _tg(channel, text)


# ------------------------------------------------------------------- main --
def main() -> None:
    init_db()
    once = "--once" in sys.argv
    workers = [ChainIndexer(ch) for ch in CHAINS]
    while True:
        busy = False
        for w in workers:
            try:
                n = w.run_once(max_ranges=5 if once else 40)
                busy = busy or n >= 40
            except Exception as e:
                log.warning("%s: pass failed: %s", w.chain, str(e)[:200])
                w.rpc = None if "rpc" in str(e) else w.rpc
        try:
            send_graduation_alerts()
        except Exception as e:
            log.warning("alerts failed: %s", e)
        if once:
            with idx_conn() as c:
                for r in c.execute("SELECT chain, block, rpc FROM cursor"):
                    log.info("cursor %s at block %s via %s", r["chain"], r["block"], r["rpc"])
                log.info("curves indexed: %s, trades: %s",
                         c.execute("SELECT COUNT(*) FROM curves").fetchone()[0],
                         c.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
            return
        time.sleep(1 if busy else 6)


if __name__ == "__main__":
    main()
