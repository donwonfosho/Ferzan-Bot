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


def vid(name: str) -> Path | None:
    p = IMG_DIR / name
    return p if p.is_file() and p.stat().st_size < 50_000_000 else None


def tg_video(chat: str, text: str, video: Path | None, poster: Path | None) -> bool:
    """Video with the text as its caption; falls back to the poster image, then to text, so a post is never lost."""
    token = _token()
    if not token or not chat:
        return False
    if video is None:
        return tg_photo(chat, text, poster)
    long = len(text) > TG_CAPTION_MAX
    try:
        with video.open("rb") as fh:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendVideo", timeout=180,
                              data={"chat_id": chat, "supports_streaming": "true", **({} if long else {"caption": text})},
                              files={"video": fh})
        if r.status_code != 200:
            return tg_photo(chat, text, poster)
    except Exception:
        return tg_photo(chat, text, poster)
    return tg_text(chat, text) if long else True


def _x_upload_video(keys: dict, video: Path) -> str:
    """Chunked video upload to X (INIT, APPEND in 1MB pieces, FINALIZE, wait until processed). Returns the media id or ""."""
    import time
    import x_poster
    url = "https://upload.twitter.com/1.1/media/upload.json"
    data = video.read_bytes()

    def call(fields: dict, media: bytes | None = None) -> tuple[int, dict]:
        # every field goes as multipart, so the OAuth signature only covers the URL (same as the image upload)
        files = {k: (None, str(v)) for k, v in fields.items()}
        if media is not None:
            files["media"] = (video.name, media, "application/octet-stream")
        r = requests.post(url, timeout=120, files=files, headers={"Authorization": x_poster.oauth_header("POST", url, keys)})
        try:
            return r.status_code, (r.json() if r.content else {})
        except Exception:
            return r.status_code, {}

    try:
        code, d = call({"command": "INIT", "total_bytes": len(data), "media_type": "video/mp4", "media_category": "tweet_video"})
        mid = d.get("media_id_string") or ""
        if code not in (200, 201, 202) or not mid:
            print(f"X video INIT: HTTP {code}"); return ""
        for i, off in enumerate(range(0, len(data), 1_000_000)):
            code, _ = call({"command": "APPEND", "media_id": mid, "segment_index": i}, data[off:off + 1_000_000])
            if code not in (200, 201, 202, 204):
                print(f"X video APPEND {i}: HTTP {code}"); return ""
        code, d = call({"command": "FINALIZE", "media_id": mid})
        if code not in (200, 201, 202):
            print(f"X video FINALIZE: HTTP {code}"); return ""
        info = d.get("processing_info")
        for _ in range(20):  # up to ~2 minutes of processing
            if not info or info.get("state") == "succeeded":
                return str(mid)
            if info.get("state") == "failed":
                print("X video processing failed"); return ""
            time.sleep(min(max(int(info.get("check_after_secs") or 5), 2), 10))
            r = requests.get(url, params={"command": "STATUS", "media_id": mid}, timeout=30,
                             headers={"Authorization": x_poster.oauth_header("GET", url, keys, {"command": "STATUS", "media_id": mid})})
            info = (r.json() if r.status_code == 200 else {}).get("processing_info")
        return ""
    except Exception as e:
        print("X video upload failed:", str(e)[:120])
        return ""


def x_post_video(text: str, video: Path | None, poster: Path | None) -> tuple[bool, str]:
    """Posts to X with the video attached; if the upload fails, posts with the poster image instead."""
    import x_poster
    keys = x_poster.keys_from_env()
    if not keys:
        return False, "no X keys"
    mid = _x_upload_video(keys, video) if video is not None else ""
    if not mid:
        return x_post(text, poster)
    code, d = x_poster._request("POST", x_poster.TWEET_URL, keys, {"text": text[:280], "media": {"media_ids": [mid]}})
    if code in (200, 201) and (d.get("data") or {}).get("id"):
        return True, d["data"]["id"]
    print(f"X post with video failed (HTTP {code}); posting with the image")
    return x_post(text, poster)
