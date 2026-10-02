#!/usr/bin/env python3
"""Ferzan droplet smoke test: read-only, sends nothing, spends nothing, prints no secrets.

    /opt/ferzan/.venv/bin/python /opt/ferzan/app/Ferzan-Ecosystem/smoke_test.py

Checks services, the public Mini App pages, the local APIs, and that the Trade Bot's new screens build.
Exit code 0 = everything passed, 1 = something failed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESK = HERE / "Trade Desk"
SERVICES = ("ferzan-trade", "ferzan-webapp", "ferzan-launch", "ferzan-launch-api")
PAGES = ("app", "evm", "solana", "ton", "claim", "curve", "launches", "leaderboard")
BASE = os.environ.get("MINI_APP_BASE_URL", "https://launch.ferzaneco.com/miniapp").rstrip("/")
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    print(("PASS " if ok else "FAIL ") + label + (f"  ({detail})" if detail else ""))


def get(url: str, timeout: int = 10) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ferzan-smoke"}), timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def load_env() -> None:
    """Read KEY=VALUE lines into this process only so modules import the way the bots do. Values are never printed."""
    for p in (os.environ.get("ENV_FILE", ""), "/opt/ferzan/app/.env", "/opt/ferzan/.env", str(DESK / ".env")):
        if p and Path(p).is_file():
            for line in Path(p).read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            print(f"(settings read from {p})")
            return
    print("(no .env found: module checks use the current environment)")


def main() -> int:
    print("== Services")
    for s in SERVICES:
        try:
            st = subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            st = "unknown"
        check(st == "active", f"service {s}", st)

    print("\n== Mini App pages (public)")
    for p in PAGES:
        code, body = get(f"{BASE}/{p}.html")
        check(code == 200 and b"<" in body, f"{p}.html", f"HTTP {code}")
    code, body = get(f"{BASE}/app.html")
    check(code == 200 and b"ferzan" in body.lower(), "app.html has the Ferzan theme and chooser", f"HTTP {code}")

    print("\n== Local APIs")
    for label, url in (("Launch API /api/launches", "http://127.0.0.1:8000/api/launches"),
                       ("Launch API /api/leaderboard", "http://127.0.0.1:8000/api/leaderboard"),
                       ("Trade web app /api/public-stats", "http://127.0.0.1:8021/api/public-stats")):
        code, body = get(url)
        ok = code == 200
        if ok:
            try:
                json.loads(body)
            except ValueError:
                ok = False
        check(ok, label, f"HTTP {code}")

    print("\n== Trade Bot screens build")
    load_env()
    sys.path.insert(0, str(DESK))
    os.chdir(DESK)
    try:
        import trust

        txt = trust.security_text()
        check("custodial" in txt.lower() and "Base" in txt, "/security text (honest custody and Base note)")
        check(bool(trust.countdown()), "launch countdown", trust.countdown())
        check(trust.rank_for(0)["title"] == "Rookie" and trust.rank_for(50_000)["title"] == "Sniper", "/rank ladder")
    except Exception as e:
        check(False, "trust module", type(e).__name__ + ": " + str(e)[:80])
    try:
        import wrapped_card

        png = wrapped_card.render(name="Smoke", week={"trades": 3, "volume_usd": 1234.0, "pnl_usd": 56.0, "best_usd": 40.0, "win_rate": 66.0},
                                  rank=trust.rank_for(1234.0), footer="smoke test")
        check(png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000, "/wrapped card renders", f"{len(png) // 1024} KB")
    except Exception as e:
        check(False, "wrapped card", type(e).__name__ + ": " + str(e)[:80])
    try:
        import launchday

        rep = launchday.report()
        check("Launch-day check" in rep, "/launchday report", f"{rep.count(chr(10))} lines")
        print("\n" + rep.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    except Exception as e:
        check(False, "launchday", type(e).__name__ + ": " + str(e)[:80])

    bad = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed" + ("" if not bad else "  |  FAILED: " + "; ".join(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
