"""HTTP layer: on-disk SQLite response cache plus a per-host request scheduler.

Each host gets (a) a minimum spacing between request *starts* and (b) a cap on requests in flight,
both from config.Advanced. Threads reserve their start slot under a per-host lock and sleep outside it,
so hosts never block each other. ListenBrainz's X-RateLimit headers are honoured: when the window is
nearly spent, every thread waits for it to reset. MusicBrainz is 1 in flight, 1 req/s, and backs off
exponentially on 503 instead of failing.
"""
import hashlib
import json
import sqlite3
import threading
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlparse

import requests

from .brand import USER_AGENT
from .config import Advanced

CACHE_PATH = Path(__file__).resolve().parent.parent / ".cache" / "http.sqlite"


class Http:
    def __init__(self, cache_path: Path = CACHE_PATH, ttl: int = 7 * 86400, offline: bool = False,
                 min_interval: dict[str, float] | None = None, max_attempts: dict[str, int] | None = None,
                 backoff_cap_s: float = Advanced.backoff_cap_s, max_in_flight: dict[str, int] | None = None,
                 ratelimit_floor: int = Advanced.ratelimit_floor):
        defaults = Advanced()  # rate limits live in config.Advanced only
        self.min_interval = min_interval or defaults.min_interval_s
        self.max_attempts = max_attempts or defaults.max_attempts
        self.max_in_flight = max_in_flight or defaults.max_in_flight
        self.backoff_cap_s, self.ratelimit_floor = backoff_cap_s, ratelimit_floor
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(cache_path, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS c (k TEXT PRIMARY KEY, t REAL, v TEXT)")
        self._db_lock = threading.Lock()
        self.ttl = ttl
        self.offline = offline
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        adapter = requests.adapters.HTTPAdapter(pool_connections=8, pool_maxsize=16)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._next: dict[str, float] = {}  # host -> earliest monotonic time the next request may start
        self._host_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._sems: dict[str, threading.BoundedSemaphore] = {}
        self._meta_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self.calls = 0
        self.failures = 0

    # ---------------------------------------------------------------- scheduling
    def _get(self, table: dict, host: str, fallback):
        return table.get(host, table.get("default", fallback))

    def _sem(self, host: str) -> threading.BoundedSemaphore:
        with self._meta_lock:
            if host not in self._sems:
                self._sems[host] = threading.BoundedSemaphore(max(1, self._get(self.max_in_flight, host, 4)))
            return self._sems[host]

    def _reserve(self, host: str) -> None:
        """Take the host's next start slot, then sleep until it (outside the lock)."""
        gap = self._get(self.min_interval, host, 0.05)
        with self._host_locks[host]:
            now = time.monotonic()
            slot = max(now, self._next.get(host, 0.0))
            self._next[host] = slot + gap
        if slot > now:
            time.sleep(slot - now)

    def _push_back(self, host: str, seconds: float) -> None:
        """Nobody starts a request to this host for `seconds` (backoff / exhausted rate window)."""
        with self._host_locks[host]:
            self._next[host] = max(self._next.get(host, 0.0), time.monotonic() + seconds)

    def _honour_headers(self, host: str, r: requests.Response) -> None:
        try:
            remaining = int(r.headers["X-RateLimit-Remaining"])
            reset_in = float(r.headers.get("X-RateLimit-Reset-In", 1))
        except (KeyError, ValueError):
            return
        if remaining <= self.ratelimit_floor:
            self._push_back(host, reset_in + 0.2)

    # ---------------------------------------------------------------- cache
    def _key(self, method, url, params, body):
        raw = json.dumps([method, url, params, body], sort_keys=True)
        return hashlib.sha1(raw.encode()).hexdigest()

    def _cached(self, key):
        with self._db_lock:
            return self.db.execute("SELECT t, v FROM c WHERE k=?", (key,)).fetchone()

    def _store(self, key, data):
        with self._db_lock:
            self.db.execute("INSERT OR REPLACE INTO c VALUES (?,?,?)", (key, time.time(), json.dumps(data)))
            self.db.commit()

    # ---------------------------------------------------------------- requests
    def request(self, method, url, params=None, body=None, headers=None, shape=None, max_age=None,
                cache_params=None, valid=None):
        key = self._key(method, url, cache_params if cache_params is not None else params, body)
        row = self._cached(key)
        if row and (self.offline or time.time() - row[0] < (max_age if max_age is not None else self.ttl)):
            return json.loads(row[1])
        if self.offline:
            return None
        host = urlparse(url).hostname or ""
        attempts = self._get(self.max_attempts, host, 4)
        with self._sem(host):
            for attempt in range(attempts):
                self._reserve(host)
                with self._stats_lock:
                    self.calls += 1
                try:
                    r = self.session.request(method, url, params=params, json=body, headers=headers, timeout=20)
                except requests.RequestException:
                    self._backoff(host, attempt, None)
                    continue
                self._honour_headers(host, r)
                if r.status_code in (429, 502, 503, 504):
                    # MB answers 503 when we exceed 1 req/s or it's overloaded: wait, never error out
                    self._backoff(host, attempt, r.headers.get("Retry-After"))
                    continue
                if r.status_code != 200:
                    return None
                data = r.json()
                if host == "api.deezer.com" and _deezer_quota_error(data):
                    self._backoff(host, attempt, None)  # quota errors come back as 200 + error body
                    continue  # (other Deezer errors, e.g. 800 'no data', are stable answers: cached below)
                if shape:
                    data = shape(data)
                if valid and not valid(data):
                    return data  # e.g. a Subsonic auth error: report it, never cache it
                self._store(key, data)
                return data
        with self._stats_lock:
            self.failures += 1
        return json.loads(row[1]) if row else None  # stale cache beats nothing

    def _backoff(self, host: str, attempt: int, retry_after: str | None):
        try:
            wait = float(retry_after) if retry_after else 0.0
        except ValueError:
            wait = 0.0
        wait = max(wait, min(self.backoff_cap_s, 2.0 ** (attempt + 1)))
        self._push_back(host, wait)  # every caller backs off, not just this one
        time.sleep(wait)

    def close(self) -> None:
        with self._db_lock:
            self.db.close()
        self.session.close()

    def get(self, url, params=None, headers=None, shape=None, max_age=None, cache_params=None, valid=None):
        return self.request("GET", url, params=params, headers=headers, shape=shape, max_age=max_age,
                            cache_params=cache_params, valid=valid)

    def post(self, url, body, headers=None):
        return self.request("POST", url, body=body, headers=headers)


def _deezer_quota_error(data) -> bool:
    err = data.get("error") if isinstance(data, dict) else None
    return isinstance(err, dict) and err.get("code") == 4  # "Quota limit exceeded"
