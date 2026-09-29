"""
Graduation keeper for one Ferzan TON curve:  python scripts/ton_graduate.py <curve>

Thin wrapper around scripts/ton-keeper/ton_keeper.mjs (the only place the keeper key is loaded). It runs the graduation
(graduate -> open the STON.fi pool -> burn the LP) only when TON_KEEPER_LIVE=1; otherwise it prints what it WOULD do and
sends nothing. Prints one JSON line.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
KEEPER_DIR = Path(os.environ.get("TON_KEEPER_DIR") or "/opt/ferzan/ton-keeper/app")


def out(**kw):
    print(json.dumps(kw))
    sys.exit(0)


def main():
    if len(sys.argv) != 2:
        out(ok=False, error="usage: ton_graduate.py <curve>")
    curve = sys.argv[1].strip()
    script = KEEPER_DIR / "ton_keeper.mjs"
    if not script.exists() or not (KEEPER_DIR / "node_modules").exists():
        out(ok=False, error="keeper tools not installed (run the TON keeper setup step)")
    args = ["node", str(script), "run", curve] + (["--send"] if (os.environ.get("TON_KEEPER_LIVE") or "").strip() == "1" else [])
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=540, cwd=str(KEEPER_DIR))
    except subprocess.TimeoutExpired:
        out(ok=False, error="timed out waiting for TON")
    for line in reversed((p.stdout or "").splitlines()):
        if line.startswith("{"):
            print(line)
            return
    out(ok=False, error=(p.stderr or "no answer")[-200:])


if __name__ == "__main__":
    main()
