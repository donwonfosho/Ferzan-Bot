"""Crash-safe state files for the timer scripts (flywheel, refill, promo, flagship).
read_json: a missing file gives the default; a damaged file raises StateCorrupt, so a script never
starts from a blank state and repeats money moves or posts. write_json: temp file, fsync, atomic rename.
lock: one run at a time per state file (a second start exits instead of running twice)."""
import contextlib, fcntl, json, os
from pathlib import Path


class StateCorrupt(Exception):
    pass


class Busy(Exception):
    pass


def read_json(path, default):
    p = Path(path)
    if not p.exists():
        return default() if callable(default) else default
    try:
        d = json.loads(p.read_text())
    except Exception as e:
        raise StateCorrupt(f"{p} is unreadable ({e}); fix or restore it by hand") from e
    if not isinstance(d, dict):
        raise StateCorrupt(f"{p} does not hold an object")
    return d


def write_json(path, data, indent=None) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(json.dumps(data, indent=indent))
        fh.flush(); os.fsync(fh.fileno())
    os.replace(tmp, p)


@contextlib.contextmanager
def lock(path):
    p = Path(str(path) + ".lock")
    p.parent.mkdir(parents=True, exist_ok=True)
    fh = open(p, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close(); raise Busy(str(p))
    try:
        yield
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN); fh.close()
