"""Short cache + rate-limit backoff for the free price/market APIs (CoinGecko, GeckoTerminal).

Why: those APIs allow only a few dozen calls a minute per IP. Many users opening cards, the sniper scan and the
/health probe all share one server IP, so on a busy day they trip HTTP 429 and everything slows down.

What it does:
  * repeat requests inside `ttl` seconds are answered from memory (only successful 200 answers are kept);
  * after a 429 the host is left alone for the Retry-After time (default 60s, doubling to 5 min if it keeps
    happening), and callers get the last good answer (up to `stale_ok` seconds old) or a plain 429;
  * the same request made by several threads at once is sent once.

It never invents data: no cached answer and no live answer means the caller sees an error, as before.
"""

from __future__ import annotations

import json as _json
import threading
import time

import requests

_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, int, bytes]] = {}  # key -> (stored_at, status, body)
_COOL: dict[str, tuple[float, int]] = {}  # host -> (until, consecutive 429s)
_KEY_LOCKS: dict[str, threading.Lock] = {}
MAX_ENTRIES = 600
DEFAULT_COOLDOWN = 60.0
MAX_COOLDOWN = 300.0


class Resp:
    """Just enough of requests.Response for the callers in this project."""

    def __init__(self, status_code: int, content: bytes, url: str = "", cached: bool = False):
        self.status_code = status_code
        self.content = content
        self.url = url
        self.cached = cached

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        return _json.loads(self.content or b"null")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} for {self.url}")


def _host(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0].lower()


def _key(url: str, params) -> str:
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params or {}))


def clear() -> None:
    with _LOCK:
        _CACHE.clear()
        _COOL.clear()
        _KEY_LOCKS.clear()


def cooling(host_or_url: str) -> float:
    """Seconds left before we call this host again (0 when it is fine)."""
    with _LOCK:
        until = _COOL.get(_host(host_or_url), (0.0, 0))[0]
    return max(0.0, until - time.time())


def _cool(host: str, retry_after: str | None) -> None:
    try:
        wait = float(retry_after) if retry_after else 0.0
    except ValueError:
        wait = 0.0
    with _LOCK:
        n = _COOL.get(host, (0.0, 0))[1] + 1
        wait = min(MAX_COOLDOWN, max(wait, DEFAULT_COOLDOWN * (2 ** (min(n, 4) - 1))))
        _COOL[host] = (time.time() + wait, n)


def _stored(key: str, max_age: float) -> Resp | None:
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and time.time() - hit[0] <= max_age:
        return Resp(hit[1], hit[2], key.split("?", 1)[0], cached=True)
    return None


def get(url: str, params: dict | None = None, headers: dict | None = None, timeout: float = 10,
        ttl: float = 20.0, stale_ok: float = 900.0) -> Resp:
    """GET with the cache and backoff above. Network errors still raise requests.RequestException."""
    key, host, now = _key(url, params), _host(url), time.time()
    fresh = _stored(key, ttl)
    if fresh:
        return fresh
    if cooling(host) > 0:
        return _stored(key, stale_ok) or Resp(429, b'{"error":"backing off after a rate limit"}', url)
    with _LOCK:
        gate = _KEY_LOCKS.setdefault(key, threading.Lock())
    with gate:
        fresh = _stored(key, ttl)  # another thread may have just fetched it
        if fresh:
            return fresh
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        status = int(r.status_code)
        if status == 429:
            _cool(host, (getattr(r, "headers", None) or {}).get("Retry-After"))
            return _stored(key, stale_ok) or Resp(429, getattr(r, "content", b"") or b"", url)
        body = getattr(r, "content", None)
        if body is None:
            body = _json.dumps(r.json()).encode()
        if status == 200:
            with _LOCK:
                _COOL.pop(host, None)
                _CACHE[key] = (now, status, body)
                if len(_CACHE) > MAX_ENTRIES:
                    for old in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: MAX_ENTRIES // 5]:
                        _CACHE.pop(old, None)
        return Resp(status, body, url)
