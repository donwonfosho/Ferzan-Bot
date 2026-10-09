"""The one place the FERZAN launch time lives.

Set FERZAN_LAUNCH_AT in /opt/ferzan/.env (ISO 8601 such as 2026-11-05T20:00:00Z, or unix seconds) to schedule the launch.
While it is not set, every countdown, launch-day announcement, buyback start and automatic launch stays OFF:
launch_at() returns NOT_SET (1 Jan 2100) so every "has the launch time come?" check says no.
"""
import os
from datetime import datetime, timezone
from pathlib import Path

NOT_SET = 4102444800  # 2100-01-01 00:00 UTC: "no date yet"
HERE = Path(__file__).resolve().parent


def _env(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    if v:
        return v
    for f in ("/opt/ferzan/.env", str(HERE / ".env")):
        try:
            for line in open(f, encoding="utf-8"):
                line = line.strip()
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return ""


def parse(raw: str) -> int:
    raw = (raw or "").strip()
    if not raw:
        return NOT_SET
    try:
        if raw.isdigit():
            v = int(raw)
        else:
            v = int(datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
    except ValueError:
        return NOT_SET  # a typo means "no date", never a wrong date
    return v if 1_700_000_000 < v < NOT_SET else NOT_SET


def launch_at() -> int:
    return parse(_env("FERZAN_LAUNCH_AT"))


def is_set(value: int | None = None) -> bool:
    return (launch_at() if value is None else value) < NOT_SET


def label_et(value: int | None = None) -> str:
    """'Thursday Nov 5, 4:00 PM Eastern', or a plain 'date to be announced'."""
    v = launch_at() if value is None else value
    if not is_set(v):
        return "date to be announced"
    from zoneinfo import ZoneInfo
    d = datetime.fromtimestamp(v, ZoneInfo("America/New_York"))
    return d.strftime("%A %b ") + str(d.day) + d.strftime(", ") + str(d.hour % 12 or 12) + d.strftime(":%M ") + ("AM" if d.hour < 12 else "PM") + " Eastern"
