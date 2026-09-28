"""Ferzan offsite backup: after the nightly backup, encrypts the newest /opt/ferzan/backups/<date>/ folder and
sends it to the admins in Telegram, so the databases and keys survive even if the droplet is lost.

  * encryption  GPG, AES-256, with the passphrase in /opt/ferzan/dbc-keys/offsite-passphrase (you chose it at
                install time; keep your own copy in a password manager: without it the file cannot be opened)
  * where       a DM from the Launch Bot to each FERZAN_ADMIN_IDS (or OFFSITE_BACKUP_CHAT if set)
  * size        Telegram bots can send up to 50 MB; bigger backups are refused and you get a note instead

Restore on any computer:  gpg -d ferzan-backup-<date>.tar.gpg > backup.tar  &&  tar -xf backup.tar
Usage: ferzan_offsite.py [run|test]      (test = encrypt and check, send nothing)
"""
import io, json, os, subprocess, sys, tarfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
for f in ("/opt/ferzan/.env", str(HERE / ".env")):
    if os.path.isfile(f):
        for line in open(f, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
import requests  # noqa: E402

BACKUPS = Path("/opt/ferzan/backups")
PASS = Path("/opt/ferzan/dbc-keys/offsite-passphrase")
STATE = Path("/opt/ferzan/ops/offsite_state.json")
MAX_BYTES = 49 * 1024 * 1024


def chats() -> list:
    one = (os.environ.get("OFFSITE_BACKUP_CHAT") or "").strip()
    if one:
        return [one]
    return sorted({x.strip() for x in ((os.environ.get("FERZAN_ADMIN_IDS") or "") + "," +
                                      (os.environ.get("ADMIN_TELEGRAM_ID") or "")).split(",") if x.strip()})


def token() -> str:
    return os.environ.get("LAUNCHBOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""


def note(text: str) -> None:
    for c in chats():
        try:
            requests.post(f"https://api.telegram.org/bot{token()}/sendMessage", json={"chat_id": c, "text": text}, timeout=15)
        except Exception:
            pass
    print(text)


def newest() -> Path | None:
    dirs = [d for d in BACKUPS.iterdir() if d.is_dir() and d.name[:8].isdigit()] if BACKUPS.is_dir() else []
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def encrypt(folder: Path) -> bytes:
    if not PASS.is_file() or len(PASS.read_text().strip()) < 12:
        raise RuntimeError("no offsite passphrase set (run the installer again)")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(folder, arcname=folder.name)
    p = subprocess.run(["gpg", "--batch", "--yes", "--quiet", "--symmetric", "--cipher-algo", "AES256",
                        "--passphrase-file", str(PASS), "--output", "-"], input=buf.getvalue(), capture_output=True, timeout=300)
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError("gpg failed: " + p.stderr.decode(errors="replace")[-200:])
    return p.stdout


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    folder = newest()
    if not folder:
        note("⚠️ Offsite backup: no nightly backup folder found in /opt/ferzan/backups.")
        return 1
    try:
        blob = encrypt(folder)
    except Exception as e:
        note(f"⚠️ Offsite backup FAILED to encrypt {folder.name}: {e}")
        return 1
    if len(blob) > MAX_BYTES:
        note(f"⚠️ Offsite backup {folder.name} is {len(blob) / 1e6:.0f} MB, over Telegram's 50 MB limit. Nothing sent.")
        return 1
    name = f"ferzan-backup-{folder.name}.tar.gpg"
    if mode == "test":
        print(f"test ok: {name} encrypts to {len(blob) / 1e6:.1f} MB; would go to {len(chats())} admin chat(s). Nothing sent.")
        return 0
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    if st.get("last_sent") == folder.name:
        print(f"{folder.name} was already sent")
        return 0
    sent = 0
    for c in chats():
        try:
            r = requests.post(f"https://api.telegram.org/bot{token()}/sendDocument",
                              data={"chat_id": c, "caption": f"🔐 Ferzan nightly backup {folder.name} (encrypted, "
                                                            f"{len(blob) / 1e6:.1f} MB). Keep this chat; open with your backup passphrase."},
                              files={"document": (name, blob, "application/octet-stream")}, timeout=180)
            sent += 1 if (r.json() or {}).get("ok") else 0
        except Exception:
            pass
    if not sent:
        note(f"⚠️ Offsite backup {folder.name}: Telegram did not accept the file. Will retry tomorrow.")
        return 1
    st.update(last_sent=folder.name, sent_at=int(time.time()))
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st))
    print(f"sent {name} ({len(blob) / 1e6:.1f} MB) to {sent} chat(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
