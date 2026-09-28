"""Launch-night load test: opens thousands of live-feed viewers and hammers the busiest API pages at the same time,
then reports how fast the API answered and whether anything failed.

  python3 loadtest.py                      2500 viewers, 30 page requests a second, for 90 seconds
  python3 loadtest.py 1000 20 60           viewers, requests per second, seconds

It goes through nginx on this droplet (127.0.0.1:443 with the real certificate), exactly like visitors do. It reads
only: no trades, no posts, nothing written. Real visitors keep working, but leave room: run it when things are quiet,
and keep viewers under the feed's limit (3000 by default) so real people can still connect.
"""
import asyncio, json, os, resource, ssl, statistics, sys, time

HOST = "launch.ferzaneco.com"
PAGES = ["/api/launches?limit=30", "/api/launches?sort=koth&limit=30", "/api/pulse", "/api/compete", "/api/stream-status",
         "/api/transparency"]
viewers = int(sys.argv[1]) if len(sys.argv) > 1 else 2500
rps = int(sys.argv[2]) if len(sys.argv) > 2 else 30
seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 90

soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, max(soft, viewers + 2000)), hard))
ctx = ssl.create_default_context()
ADDR, PORT = os.environ.get("LT_ADDR", "127.0.0.1"), int(os.environ.get("LT_PORT", "443"))
if os.environ.get("LT_TEST_INSECURE") == "1":  # only for testing this script against a local stand-in
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
stats = {"open": 0, "peak": 0, "full": 0, "limited": 0, "fail": 0, "dropped": 0, "pings": 0}
lat: dict = {p: [] for p in PAGES}
errs: dict = {}


async def viewer(i: int, until: float):
    await asyncio.sleep(i * 0.004)  # ramp up over ~10 s for 2500 viewers
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(ADDR, PORT, ssl=ctx, server_hostname=HOST), 20)
        w.write(f"GET /api/stream HTTP/1.1\r\nHost: {HOST}\r\nAccept: text/event-stream\r\n\r\n".encode())
        await w.drain()
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 20)
        code = int(head.split(b" ", 2)[1])
        if code != 200:
            stats["full" if code == 503 else "limited" if code == 429 else "fail"] += 1
            w.close()
            return
        stats["open"] += 1
        stats["peak"] = max(stats["peak"], stats["open"])
        try:
            while time.time() < until:
                chunk = await asyncio.wait_for(r.read(4096), max(1.0, until - time.time()))
                if not chunk:
                    stats["dropped"] += 1
                    break
                if b"ping" in chunk:
                    stats["pings"] += 1
        except asyncio.TimeoutError:
            pass
        finally:
            stats["open"] -= 1
            w.close()
    except Exception as e:
        stats["fail"] += 1
        errs[type(e).__name__] = errs.get(type(e).__name__, 0) + 1


async def page(path: str):
    t0 = time.perf_counter()
    try:
        r, w = await asyncio.wait_for(asyncio.open_connection(ADDR, PORT, ssl=ctx, server_hostname=HOST), 20)
        w.write(f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\n\r\n".encode())
        await w.drain()
        data = await asyncio.wait_for(r.read(), 30)
        w.close()
        code = int(data.split(b" ", 2)[1])
        if code != 200:
            errs[f"HTTP {code} {path}"] = errs.get(f"HTTP {code} {path}", 0) + 1
            return
        lat[path].append((time.perf_counter() - t0) * 1000)
    except Exception as e:
        errs[f"{type(e).__name__} {path}"] = errs.get(f"{type(e).__name__} {path}", 0) + 1


def api_cpu() -> str:
    try:
        pid = os.popen("systemctl show -p MainPID --value ferzan-launch-api 2>/dev/null").read().strip()
        rss = int(open(f"/proc/{pid}/status").read().split("VmRSS:")[1].split()[0]) // 1024
        return f"API memory {rss} MB, load {open('/proc/loadavg').read().split()[0]}"
    except Exception:
        return ""


async def main():
    until = time.time() + seconds
    print(f"load test: {viewers} live viewers + {rps} page requests/s for {seconds}s via nginx ({HOST})")
    tasks = [asyncio.create_task(viewer(i, until)) for i in range(viewers)]
    reqs = []
    k = 0
    start = time.time()
    next_report = start + 15
    while time.time() < until:
        for _ in range(rps):
            reqs.append(asyncio.create_task(page(PAGES[k % len(PAGES)])))
            k += 1
        await asyncio.sleep(1)
        if time.time() >= next_report:
            done = sum(len(v) for v in lat.values())
            print(f"  {int(time.time() - start):3}s  viewers connected {stats['open']:5}  pages answered {done:6}  errors {sum(errs.values()):4}  {api_cpu()}")
            next_report += 15
    await asyncio.gather(*reqs, return_exceptions=True)
    await asyncio.gather(*tasks, return_exceptions=True)
    print("\n== results")
    print(f"live viewers: peak {stats['peak']} of {viewers} | refused because full {stats['full']} | per-address limit {stats['limited']} | "
          f"failed {stats['fail']} | dropped early {stats['dropped']}")
    ok = True
    for p, v in lat.items():
        if not v:
            print(f"  {p:38} no answers")
            ok = False
            continue
        v.sort()
        p95 = v[int(len(v) * 0.95) - 1] if len(v) > 1 else v[0]
        print(f"  {p:38} {len(v):5} answers   median {statistics.median(v):6.0f} ms   95% under {p95:6.0f} ms   slowest {v[-1]:6.0f} ms")
        ok = ok and p95 < 2000
    if errs:
        print("errors:", json.dumps(errs, indent=None))
    total = sum(len(v) for v in lat.values()) + sum(errs.values())
    bad = sum(errs.values()) / total * 100 if total else 0
    print(f"error rate {bad:.1f}%   {api_cpu()}")
    print("VERDICT:", "OK for launch night" if ok and bad < 1 and stats["peak"] >= viewers * 0.98 else
          "NEEDS ATTENTION (send this output to Claude)")


if __name__ == "__main__":
    asyncio.run(main())
