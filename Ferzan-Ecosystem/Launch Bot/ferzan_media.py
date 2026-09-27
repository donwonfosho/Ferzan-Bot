"""Image posting for the Ferzan auto-posts: Telegram photos and X posts with an attached image.
Every function falls back to a text-only post when the image is missing or the upload fails, so a post is never lost."""
from __future__ import annotations

import os
from pathlib import Path

import requests

IMG_DIR = Path(__file__).resolve().parent / "promo_img"
TG_CAPTION_MAX = 1024  # Telegram's limit for photo captions


def img(name: str) -> Path | None:
    p = IMG_DIR / name
    return p if p.is_file() else None


def _token() -> str:
    return os.environ.get("LAUNCHBOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""


def tg_text(chat: str, text: str, preview: bool = False) -> bool:
    token = _token()
    if not token or not chat:
        return False
    try:
        return requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=15, json={
            "chat_id": chat, "text": text[:4000], "disable_web_page_preview": not preview}).status_code == 200
    except Exception:
        return False


def tg_photo(chat: str, text: str, image: Path | None) -> bool:
    """Photo with the text as its caption; a long text goes as a message right after the photo."""
    token = _token()
    if not token or not chat:
        return False
    if image is None:
        return tg_text(chat, text, preview=True)
    long = len(text) > TG_CAPTION_MAX
    try:
        with image.open("rb") as fh:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto", timeout=30,
                              data={"chat_id": chat, **({} if long else {"caption": text})}, files={"photo": fh})
        if r.status_code != 200:
            return tg_text(chat, text, preview=True)
    except Exception:
        return tg_text(chat, text, preview=True)
    return tg_text(chat, text) if long else True


def _x_upload(keys: dict, image: Path) -> str:
    """Uploads an image to X and returns its media id ("" on failure). Tries the v2 endpoint, then v1.1."""
    import x_poster
    data = image.read_bytes()
    for url, fields, id_of in (
        ("https://api.x.com/2/media/upload", {"media_category": "tweet_image", "media_type": "image/jpeg"},
         lambda d: (d.get("data") or {}).get("id") or ""),
        ("https://upload.twitter.com/1.1/media/upload.json", {"media_category": "tweet_image"},
         lambda d: d.get("media_id_string") or ""),
    ):
        try:
            r = requests.post(url, timeout=60, data=fields, files={"media": (image.name, data, "image/jpeg")},
                              headers={"Authorization": x_poster.oauth_header("POST", url, keys)})
            if r.status_code in (200, 201):
                mid = id_of(r.json())
                if mid:
                    return str(mid)
            print(f"X media upload {url.split('/')[2]}: HTTP {r.status_code}")
        except Exception as e:
            print("X media upload failed:", str(e)[:120])
    return ""


def x_post(text: str, image: Path | None = None) -> tuple[bool, str]:
    """Posts to X with the image attached when it uploads; otherwise text only."""
    import x_poster
    keys = x_poster.keys_from_env()
    if not keys:
        return False, "no X keys"
    mid = _x_upload(keys, image) if image is not None else ""
    if not mid:
        return x_poster.tweet(keys, text)
    code, d = x_poster._request("POST", x_poster.TWEET_URL, keys, {"text": text[:280], "media": {"media_ids": [mid]}})
    if code in (200, 201) and (d.get("data") or {}).get("id"):
        return True, d["data"]["id"]
    print(f"X post with image failed (HTTP {code}); posting text only")
    return x_poster.tweet(keys, text)
