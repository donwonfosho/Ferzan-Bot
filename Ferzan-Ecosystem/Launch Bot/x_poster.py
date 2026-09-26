"""
x_poster.py -- posts Ferzan launch news to X (Twitter): new curve launches, 90% to graduation,
new King of the Hill and graduations. Called from the launch indexer's loop.

Needs X API keys with write access (OAuth 1.0a user context) in Launch Bot/.env:
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET   (set with /opt/ferzan/ops/set_x.sh)
Optional: X_POSTS=launch,p90,koth,grad   X_MAX_POSTS_PER_DAY=10
Without keys it does nothing. It never posts old news: on its first run it only records what exists.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request

log = logging.getLogger("x_poster")
TWEET_URL = "https://api.x.com/2/tweets"
ME_URL = "https://api.x.com/2/users/me"
CHAIN_NAME = {"base": "Base", "bsc": "BNB Chain", "eth": "Ethereum", "ethereum": "Ethereum", "robinhood": "Robinhood Chain"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS x_posts (key TEXT PRIMARY KEY, kind TEXT, ts INTEGER, tweet_id TEXT, status TEXT);
"""


def _q(s: str) -> str:
    return urllib.parse.quote(str(s), safe="~-._")


def oauth_header(method: str, url: str, keys: dict, params: dict | None = None,
                 nonce: str | None = None, ts: str | None = None) -> str:
    """OAuth 1.0a HMAC-SHA1 Authorization header. `params` = query/form params (JSON bodies are not signed)."""
    oauth = {
        "oauth_consumer_key": keys["api_key"], "oauth_nonce": nonce or secrets.token_hex(16),
        "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": ts or str(int(time.time())),
        "oauth_token": keys["access_token"], "oauth_version": "1.0",
    }
    allp = {**(params or {}), **oauth}
    pstr = "&".join(f"{k}={v}" for k, v in sorted((_q(k), _q(v)) for k, v in allp.items()))
    base = "&".join([method.upper(), _q(url), _q(pstr)])
    skey = f"{_q(keys['api_secret'])}&{_q(keys['access_secret'])}"
    oauth["oauth_signature"] = base64.b64encode(hmac.new(skey.encode(), base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_q(k)}="{_q(v)}"' for k, v in sorted(oauth.items()))


def keys_from_env() -> dict | None:
    k = {"api_key": os.environ.get("X_API_KEY", "").strip(), "api_secret": os.environ.get("X_API_SECRET", "").strip(),
         "access_token": os.environ.get("X_ACCESS_TOKEN", "").strip(), "access_secret": os.environ.get("X_ACCESS_SECRET", "").strip()}
    return k if all(k.values()) else None


def _request(method: str, url: str, keys: dict, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": oauth_header(method, url, keys), "Content-Type": "application/json", "User-Agent": "ferzan-x-poster"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except Exception as e:  # network
        return 0, {"error": str(e)[:120]}


def whoami(keys: dict) -> tuple[bool, str]:
    code, d = _request("GET", ME_URL, keys)
    if code == 200 and d.get("data"):
        return True, "@" + d["data"].get("username", "?")
    return False, f"HTTP {code}: {(d.get('detail') or d.get('title') or d.get('error') or d)}"[:200]


def tweet(keys: dict, text: str) -> tuple[bool, str]:
    code, d = _request("POST", TWEET_URL, keys, {"text": text[:280]})
    if code in (200, 201) and d.get("data", {}).get("id"):
        return True, d["data"]["id"]
    return False, f"HTTP {code}: {(d.get('detail') or d.get('title') or d.get('error') or d)}"[:200]


def _fmt(kind: str, r, base: str, extra: str = "") -> str:
    name, sym = (r["name"] or "Token")[:40], (r["symbol"] or "")[:12]
    chain = CHAIN_NAME.get(r["chain"], r["chain"])
    url = f"{base}/curve.html?chain={r['chain']}&curve={r['curve']}"
    head = {
        "launch": f"🚀 New on Ferzan: {name} (${sym}) on {chain}",
        "p90": f"🚀 {name} (${sym}) is 90% of the way to graduation on {chain}{extra}",
        "koth": f"👑 New King of the Hill: {name} (${sym}) on {chain}{extra}",
        "grad": f"🎓 {name} (${sym}) just graduated on {chain}! Liquidity is live and the LP is burned 🔥",
    }[kind]
    return f"{head}\n\nCA: {r['token']}\nTrade: {url}"


def run(conn, now: int | None = None, post=None) -> list:
    """Scan the index DB for news and post what's new. Returns [(kind, key, ok, info)]."""
    now = int(now or time.time())
    conn.executescript(SCHEMA)
    keys = keys_from_env()
    post = post or (lambda text: tweet(keys, text))
    kinds = {k.strip() for k in (os.environ.get("X_POSTS") or "launch,p90,koth,grad").split(",") if k.strip()}
    cap = int(os.environ.get("X_MAX_POSTS_PER_DAY") or 10)
    base = (os.environ.get("MINI_APP_BASE_URL") or "https://launch.ferzaneco.com/miniapp").rstrip("/")
    done = {row[0] for row in conn.execute("SELECT key FROM x_posts")}
    first_run = not done and conn.execute("SELECT COUNT(*) FROM x_posts").fetchone()[0] == 0
    news = []  # (kind, key, row, extra)
    for r in conn.execute("SELECT * FROM curves WHERE launched_ts > ?", (now - 3600,)):
        news.append(("launch", f"launch:{r['curve']}", r, ""))
    try:
        for a in conn.execute("SELECT chain, curve, ts FROM alerts_sent WHERE kind = 'p90' AND ts > ?", (now - 3600,)):
            r = conn.execute("SELECT * FROM curves WHERE chain = ? AND curve = ?", (a[0], a[1])).fetchone()
            if r and not r["graduated"]:
                news.append(("p90", f"p90:{r['curve']}", r, ""))
        st = {k: v for k, v in conn.execute("SELECT k, v FROM alert_state")}
        if st.get("koth") and int(st.get("koth_ts") or 0) > now - 3600:
            r = conn.execute("SELECT * FROM curves WHERE curve = ?", (st["koth"],)).fetchone()
            if r:
                news.append(("koth", f"koth:{r['curve']}:{st['koth_ts']}", r, ""))
    except Exception:
        pass  # growth-alert tables not there yet
    for r in conn.execute("SELECT * FROM curves WHERE graduated = 1 AND grad_ts > ?", (now - 6 * 3600,)):
        news.append(("grad", f"grad:{r['curve']}", r, ""))
    if first_run:  # remember what already happened; start posting from the next event
        for kind, key, _, _ in news:
            conn.execute("INSERT OR IGNORE INTO x_posts (key, kind, ts, status) VALUES (?,?,?,'skipped-initial')", (key, kind, now))
        conn.execute("INSERT OR IGNORE INTO x_posts (key, kind, ts, status) VALUES ('init', 'init', ?, 'init')", (now,))
        return []
    out = []
    sent_today = conn.execute("SELECT COUNT(*) FROM x_posts WHERE status = 'posted' AND ts > ?", (now - 86400,)).fetchone()[0]
    for kind, key, r, extra in news:
        if key in done:
            continue
        if kind not in kinds or not keys:
            conn.execute("INSERT OR IGNORE INTO x_posts (key, kind, ts, status) VALUES (?,?,?,'off')", (key, kind, now))
            continue
        if sent_today >= cap:
            break  # try again next pass (tomorrow's allowance)
        ok, info = post(_fmt(kind, r, base, extra))
        if ok:
            sent_today += 1
            conn.execute("INSERT OR REPLACE INTO x_posts (key, kind, ts, tweet_id, status) VALUES (?,?,?,?,'posted')",
                         (key, kind, now, info))
        else:
            log.warning("x post failed (%s): %s", kind, info)
            if info.startswith(("HTTP 401", "HTTP 403")):
                conn.execute("INSERT OR REPLACE INTO x_posts (key, kind, ts, status) VALUES (?,?,?,?)", (key, kind, now, "failed"))
            break  # rate limit / outage: stop this pass
        out.append((kind, key, ok, info))
    return out
