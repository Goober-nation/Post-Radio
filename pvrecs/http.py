"""Tiny HTTP layer with an on-disk SQLite cache and per-host rate limiting."""
import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

CACHE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "http.sqlite"
MIN_INTERVAL = {"musicbrainz.org": 1.05, "api.deezer.com": 0.12}
USER_AGENT = "postvinyl-recs/0.1 ( tim.13102006@gmail.com )"


class Http:
    def __init__(self, cache_path: Path = CACHE_PATH, ttl: int = 7 * 86400, offline: bool = False):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(cache_path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS c (k TEXT PRIMARY KEY, t REAL, v TEXT)")
        self.ttl = ttl
        self.offline = offline
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.calls = 0

    def _key(self, method, url, params, body):
        raw = json.dumps([method, url, params, body], sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()

    def _throttle(self, url):
        host = urlparse(url).hostname or ""
        gap = MIN_INTERVAL.get(host, 0.05)
        with self._lock:
            wait = self._last.get(host, 0) + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def request(self, method, url, params=None, body=None, headers=None, shape=None):
        key = self._key(method, url, params, body)
        row = self.db.execute("SELECT t, v FROM c WHERE k=?", (key,)).fetchone()
        if row and (self.offline or time.time() - row[0] < self.ttl):
            return json.loads(row[1])
        if self.offline:
            return None
        for attempt in range(4):
            self._throttle(url)
            self.calls += 1
            try:
                r = self.session.request(method, url, params=params, json=body, headers=headers, timeout=20)
            except requests.RequestException:
                time.sleep(1 + attempt)
                continue
            if r.status_code in (429, 503):
                time.sleep(float(r.headers.get("Retry-After", 2 + attempt * 2)))
                continue
            if r.status_code != 200:
                return None
            data = r.json()
            if shape:
                data = shape(data)
            if isinstance(data, dict) and "error" in data and url.startswith("https://api.deezer.com"):
                time.sleep(2)  # Deezer quota errors come back as 200 + error body
                continue
            self.db.execute("INSERT OR REPLACE INTO c VALUES (?,?,?)", (key, time.time(), json.dumps(data)))
            self.db.commit()
            return data
        return None

    def get(self, url, params=None, headers=None, shape=None):
        return self.request("GET", url, params=params, headers=headers, shape=shape)

    def post(self, url, body, headers=None):
        return self.request("POST", url, body=body, headers=headers)
