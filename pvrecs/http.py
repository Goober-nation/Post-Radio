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
MIN_INTERVAL = {"musicbrainz.org": 1.1, "api.deezer.com": 0.12, "default": 0.05}  # MB hard limit: 1 req/s
MAX_ATTEMPTS = {"musicbrainz.org": 8, "default": 4}  # 2+4+...+60s of backoff before giving up on one MB call
USER_AGENT = "postvinyl-recs/0.1 ( tim.13102006@gmail.com )"


class Http:
    def __init__(self, cache_path: Path = CACHE_PATH, ttl: int = 7 * 86400, offline: bool = False,
                 min_interval: dict[str, float] | None = None, max_attempts: dict[str, int] | None = None,
                 backoff_cap_s: float = 60.0):
        self.min_interval = min_interval or MIN_INTERVAL
        self.max_attempts = max_attempts or MAX_ATTEMPTS
        self.backoff_cap_s = backoff_cap_s
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
        self.failures = 0

    def _key(self, method, url, params, body):
        raw = json.dumps([method, url, params, body], sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()

    def _throttle(self, url):
        host = urlparse(url).hostname or ""
        gap = self.min_interval.get(host, self.min_interval.get("default", 0.05))
        with self._lock:
            wait = self._last.get(host, 0) + gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def request(self, method, url, params=None, body=None, headers=None, shape=None, max_age=None,
                cache_params=None, valid=None):
        key = self._key(method, url, cache_params if cache_params is not None else params, body)
        row = self.db.execute("SELECT t, v FROM c WHERE k=?", (key,)).fetchone()
        if row and (self.offline or time.time() - row[0] < (max_age if max_age is not None else self.ttl)):
            return json.loads(row[1])
        if self.offline:
            return None
        host = urlparse(url).hostname or ""
        attempts = self.max_attempts.get(host, self.max_attempts.get("default", 4))
        for attempt in range(attempts):
            self._throttle(url)
            self.calls += 1
            try:
                r = self.session.request(method, url, params=params, json=body, headers=headers, timeout=20)
            except requests.RequestException:
                self._backoff(host, attempt, None)
                continue
            if r.status_code in (429, 502, 503, 504):
                # MB answers 503 when we exceed 1 req/s or it's overloaded: wait, never error out
                self._backoff(host, attempt, r.headers.get("Retry-After"))
                continue
            if r.status_code != 200:
                return None
            data = r.json()
            if isinstance(data, dict) and "error" in data and url.startswith("https://api.deezer.com"):
                self._backoff(host, attempt, None)  # Deezer quota errors come back as 200 + error body
                continue
            if shape:
                data = shape(data)
            if valid and not valid(data):
                return data  # e.g. a Subsonic auth error: report it, never cache it
            self.db.execute("INSERT OR REPLACE INTO c VALUES (?,?,?)", (key, time.time(), json.dumps(data)))
            self.db.commit()
            return data
        self.failures += 1
        return json.loads(row[1]) if row else None  # stale cache beats nothing

    def _backoff(self, host: str, attempt: int, retry_after: str | None):
        try:
            wait = float(retry_after) if retry_after else 0.0
        except ValueError:
            wait = 0.0
        wait = max(wait, min(self.backoff_cap_s, 2.0 ** (attempt + 1)))
        with self._lock:  # push the host's next slot out so every caller backs off, not just this one
            self._last[host] = max(self._last.get(host, 0), time.monotonic() + wait - self.min_interval.get(host, 0))
        time.sleep(wait)

    def get(self, url, params=None, headers=None, shape=None, max_age=None, cache_params=None, valid=None):
        return self.request("GET", url, params=params, headers=headers, shape=shape, max_age=max_age,
                            cache_params=cache_params, valid=valid)

    def post(self, url, body, headers=None):
        return self.request("POST", url, body=body, headers=headers)
