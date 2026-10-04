"""Read-only check that the TON backup reads (toncenter, tonapi) agree with the liteserver on a real curve.

  /opt/ferzan/.venv/bin/python scripts/ton_backup_check.py <curve address>

Prints PASS/FAIL per source and never signs or sends anything. Keys (TONCENTER_API_KEY / TONAPI_KEY) are used if set, never printed.
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ton_launch as tl  # noqa: E402


def _ints(stack):
    return [int(x) for x in stack[:7]]


def main(curve: str) -> int:
    print("keys:", "toncenter" if os.environ.get("TONCENTER_API_KEY") else "no-toncenter-key",
          "tonapi" if os.environ.get("TONAPI_KEY") else "no-tonapi-key")
    ref = None
    try:
        from pytoniq import LiteBalancer

        async def lite():
            p = LiteBalancer.from_mainnet_config(trust_level=2)
            await p.start_up()
            try:
                return await p.run_get_method(address=tl._addr(curve), method="get_curve", stack=[])
            finally:
                await p.close_all()

        ref = _ints(asyncio.run(asyncio.wait_for(lite(), 20)))
        print("liteserver: OK", ref)
    except Exception as e:  # noqa: BLE001
        print("liteserver: FAIL (that is the problem the backup exists for):", str(e)[:100])
    bad = 0
    for name in ("toncenter", "tonapi"):
        # force one source at a time by hiding the other with an unreachable URL
        orig = tl._http_urls
        tl._http_urls = (lambda n=name: ("https://toncenter.com", "http://127.0.0.1:9") if n == "toncenter"
                         else ("http://127.0.0.1:9", "https://tonapi.io"))
        try:
            got = _ints(tl._http_get_method(curve, "get_curve"))
            same = "same as liteserver" if ref == got else ("no liteserver to compare" if ref is None else "DIFFERENT from liteserver")
            print(f"{name}: PASS {got} ({same})")
            bad += 0 if ref in (None, got) else 1
        except Exception as e:  # noqa: BLE001
            print(f"{name}: FAIL", str(e)[:160])
            bad += 1
        finally:
            tl._http_urls = orig
    print("RESULT:", "ALL GOOD" if bad == 0 else "SOMETHING FAILED - send me this output")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: ton_backup_check.py <curve address>")
    sys.exit(main(sys.argv[1]))
