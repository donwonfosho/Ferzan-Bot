"""
vanity_grinder.py -- keeps a small pool of Solana mint keypairs whose address ends in
VANITY_SOL_SUFFIX (default "fzn"), so Solana launches get a branded mint address instantly.

Keys are written to /opt/ferzan/dbc-keys/vanity-sol (outside git, root-only, 0600).
They are one-time mint keys: they control nothing until a launch uses one, and each is
deleted the moment a launch takes it. Runs at low priority as its own service.
"""
import json
import logging
import os
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv("/opt/ferzan/.env")
    load_dotenv(Path(__file__).resolve().with_name(".env"))
except ImportError:
    pass

from solders.keypair import Keypair

import vanity

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vanity_grinder")
TARGET = int(os.environ.get("VANITY_SOL_POOL") or "25")


def grind_one(suffix: str) -> tuple:
    tries = 0
    while True:
        kp = Keypair()
        addr = str(kp.pubkey())
        tries += 1
        if addr.endswith(suffix):
            return addr, list(bytes(kp)), tries


def main() -> None:
    suffix = vanity.SOL_SUFFIX
    if not suffix:
        log.warning("VANITY_SOL_SUFFIX is empty/invalid - nothing to do")
        time.sleep(3600)
        return
    pool = vanity.SOL_POOL
    pool.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(pool, 0o700)
    once = "--once" in sys.argv
    log.info("keeping %d mint keys ending in '%s' in %s", TARGET, suffix, pool)
    while True:
        have = vanity.sol_pool_size()
        if have >= TARGET:
            if once:
                return
            time.sleep(20)
            continue
        t = time.time()
        addr, secret, tries = grind_one(suffix)
        tmp = pool / f".{addr}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(secret, fh)
        os.rename(tmp, pool / f"{addr}.json")
        log.info("pool %d/%d: %s (%d tries, %.1fs)", have + 1, TARGET, addr, tries, time.time() - t)
        if once:
            return


if __name__ == "__main__":
    main()
