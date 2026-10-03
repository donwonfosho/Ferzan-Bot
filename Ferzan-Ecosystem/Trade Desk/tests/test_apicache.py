import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import requests

import apicache


class R:
    def __init__(self, code=200, body=b'{"a":1}', headers=None):
        self.status_code, self.content, self.headers = code, body, headers or {}


class Cache(unittest.TestCase):
    def setUp(self):
        apicache.clear()

    def patch(self, *answers):
        return mock.patch.object(apicache.requests, "get", side_effect=list(answers))

    def test_repeat_is_served_from_memory(self):
        with self.patch(R()) as g:
            a = apicache.get("https://x.test/p", params={"i": 1}, ttl=30)
            b = apicache.get("https://x.test/p", params={"i": 1}, ttl=30)
        self.assertEqual((a.json(), b.json(), g.call_count, b.cached), ({"a": 1}, {"a": 1}, 1, True))

    def test_different_params_not_shared(self):
        with self.patch(R(), R()) as g:
            apicache.get("https://x.test/p", params={"i": 1})
            apicache.get("https://x.test/p", params={"i": 2})
        self.assertEqual(g.call_count, 2)

    def test_429_backs_off_and_serves_stale(self):
        with self.patch(R(), R(429, b"", {"Retry-After": "90"})) as g:
            apicache.get("https://x.test/p", ttl=0)
            r = apicache.get("https://x.test/p", ttl=0)  # 429 -> stale answer comes back
            self.assertEqual(r.json(), {"a": 1})
            self.assertGreater(apicache.cooling("x.test"), 80)
            apicache.get("https://x.test/p", ttl=0)  # in cooldown: no network call
            apicache.get("https://x.test/other", ttl=0)  # nothing cached for this one: plain 429
        self.assertEqual(g.call_count, 2)

    def test_429_without_cache_is_a_429(self):
        with self.patch(R(429)):
            r = apicache.get("https://y.test/p")
        self.assertEqual(r.status_code, 429)
        with self.assertRaises(requests.HTTPError):
            r.raise_for_status()

    def test_errors_are_not_cached(self):
        with self.patch(R(500, b"no"), R()) as g:
            self.assertEqual(apicache.get("https://z.test/p").status_code, 500)
            self.assertEqual(apicache.get("https://z.test/p").status_code, 200)
        self.assertEqual(g.call_count, 2)

    def test_network_error_still_raises(self):
        with mock.patch.object(apicache.requests, "get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(requests.RequestException):
                apicache.get("https://w.test/p")


if __name__ == "__main__":
    unittest.main()
