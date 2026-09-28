"""Launch dress rehearsal: follows one coin through every stage of a real launch and ticks each one off.

  python3 rehearse.py                 newest coin launched through Ferzan
  python3 rehearse.py <token>         a specific coin (mint, token address or curve)
  python3 rehearse.py <token> --watch keep checking every 20 s for up to 20 minutes, until every stage passes

Read-only: it never signs, sends or changes anything. Checks, in launch order:
  launch recorded -> indexed -> on the board -> share card -> first trade -> live feed -> X post -> competition board
"""
import json, os, socket, ssl, sqlite3, sys, time
from pathlib import Path

for f in ("/opt/ferzan/.env",):
    if os.path.isfile(f):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
import requests  # noqa: E402

LAUNCH_DB = os.environ.get("LAUNCH_DB_PATH") or "/opt/ferzan/app/launch/launch_bot.db"
INDEX_DB = os.environ.get("CURVE_INDEX_DB") or str(Path(LAUNCH_DB).parent / "curve_index.db")
LOCAL = "http://127.0.0.1:8000"
PUBLIC = "https://launch.ferzaneco.com"


def ro(path: str):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def find_coin(arg: str):
    c = ro(INDEX_DB)
    try:
        if arg:
            return c.execute("SELECT * FROM curves WHERE token = ? OR curve = ? OR lower(token) = lower(?) OR lower(curve) = lower(?)",
                             (arg, arg, arg, arg)).fetchone()
        return c.execute("SELECT * FROM curves ORDER BY COALESCE(launched_ts, 0) DESC LIMIT 1").fetchone()
    finally:
        c.close()


def stream_sees(chain: str, token: str, curve: str, seconds: int) -> str:
    """Listens to the public live feed for a trade or launch event about this coin."""
    ctx = ssl.create_default_context()
    try:
        raw = socket.create_connection(("launch.ferzaneco.com", 443), timeout=10)
        s = ctx.wrap_socket(raw, server_hostname="launch.ferzaneco.com")
        s.sendall(b"GET /api/stream HTTP/1.1\r\nHost: launch.ferzaneco.com\r\nAccept: text/event-stream\r\n\r\n")
        s.settimeout(3)
        buf, end = b"", time.time() + seconds
        keys = {k.lower() for k in (token, curve) if k}
        while time.time() < end:
            try:
                chunk = s.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            buf += chunk
            for line in buf.split(b"\n"):
                if line.startswith(b"data:"):
                    try:
                        ev = json.loads(line[5:])
                    except Exception:
                        continue
                    if ev.get("chain") == chain and {str(ev.get("token", "")).lower(), str(ev.get("curve", "")).lower()} & keys:
                        s.close()
                        return f"{ev.get('type')} event arrived"
            buf = buf.rsplit(b"\n", 1)[-1]
        s.close()
        return ""
    except Exception as e:
        return f"error: {type(e).__name__}"


def check(arg: str, listen: int) -> list:
    r = find_coin(arg)
    if not r:
        return [("indexed", False, "not in the index yet (the indexer picks new coins up within a minute)")]
    chain, token, curve = r["chain"], r["token"], r["curve"]
    out = [("indexed", True, f"{chain} ${r['symbol']} {token[:6]}…{token[-4:]}")]
    with ro(LAUNCH_DB) as c:
        row = c.execute("SELECT status, created_at FROM launch_requests WHERE (result_token_address = ? OR lower(result_token_address) = lower(?)) "
                        "ORDER BY created_at DESC LIMIT 1", (token, token)).fetchone()
    out.insert(0, ("launch recorded", bool(row and row["status"] == "confirmed"), row["status"] if row else "no launch request found (launched outside Ferzan?)"))
    try:
        items = requests.get(f"{LOCAL}/api/launches", params={"limit": 60}, timeout=20).json()
        items = items.get("items", items) if isinstance(items, dict) else items
        on = any(str(i.get("token", "")).lower() == token.lower() or str(i.get("curve", "")).lower() == curve.lower() for i in items)
        out.append(("on the board", on, "in /api/launches" if on else "not in the newest 60 on /api/launches"))
    except Exception as e:
        out.append(("on the board", False, f"API error {type(e).__name__}"))
    try:
        a = requests.get(f"{PUBLIC}/api/share/{chain}/{token}", timeout=20)
        b = requests.get(f"{PUBLIC}/api/og/{chain}/{token}.png", timeout=30)
        ok = a.status_code == 200 and "og:image" in a.text and b.status_code == 200 and b.headers.get("content-type", "").startswith("image/")
        out.append(("share card", ok, f"share page {a.status_code}, card image {b.status_code}"))
    except Exception as e:
        out.append(("share card", False, f"error {type(e).__name__}"))
    with ro(INDEX_DB) as c:
        t = c.execute("SELECT COUNT(*) n, MAX(ts) last, SUM(is_buy) buys FROM trades WHERE chain = ? AND curve = ?", (chain, curve)).fetchone()
        try:
            xp = c.execute("SELECT status, tweet_id FROM x_posts WHERE key = ?", (f"launch:{curve}",)).fetchone()
        except sqlite3.Error:
            xp = None
    n = int(t["n"] or 0)
    out.append(("first trade", n > 0, f"{n} trades, {int(t['buys'] or 0)} buys, last {int(time.time() - t['last'])}s ago" if n else "no trades yet: buy a little from a second wallet"))
    if listen:
        seen = stream_sees(chain, token, curve, listen)
        out.append(("live feed", seen.endswith("arrived"), seen or f"no event in {listen}s (make a trade while this listens)"))
    x_on = bool(os.environ.get("X_API_KEY") or os.environ.get("X_CONSUMER_KEY"))
    if xp:
        out.append(("X post", xp["status"] in ("posted", "ok") or bool(xp["tweet_id"]), f"status {xp['status']}"))
    else:
        out.append(("X post", not x_on, "not posted yet" if x_on else "X posting is not set up (skipped)"))
    try:
        d = requests.get(f"{LOCAL}/api/compete", timeout=60).json()
        out.append(("competition board", d.get("traders", 0) > 0, f"{d.get('traders', 0)} traders this round, {len(d.get('volume', []))} on the volume board"))
    except Exception as e:
        out.append(("competition board", False, f"error {type(e).__name__}"))
    try:
        st = requests.get(f"{LOCAL}/api/stream-status", timeout=10).json()
        out.append(("live feed capacity", bool(st.get("running")) or st.get("clients", 0) == 0, f"{st.get('clients')} of {st.get('max')} viewers connected"))
    except Exception as e:
        out.append(("live feed capacity", False, f"error {type(e).__name__}"))
    return out


def show(rows: list) -> bool:
    print(time.strftime("%H:%M:%S"), "-" * 60)
    for name, ok, info in rows:
        print(f"  {'✅' if ok else '⏳'} {name:20} {info}")
    good = all(ok for _, ok, _ in rows)
    print("  ALL STAGES PASS" if good else f"  {sum(1 for _, ok, _ in rows if not ok)} stage(s) still waiting")
    return good


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    watch = "--watch" in sys.argv
    arg = args[0] if args else ""
    if not watch:
        show(check(arg, 25))
        sys.exit(0)
    end = time.time() + 20 * 60
    while time.time() < end:
        if show(check(arg, 20)):
            break
        time.sleep(20)
