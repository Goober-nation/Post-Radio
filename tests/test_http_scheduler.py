"""Per-host scheduling under concurrency."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from postradio.http import Http


class Resp:
    def __init__(self, headers=None):
        self.status_code, self.headers = 200, headers or {}

    def json(self):
        return {"ok": 1}


def make(tmp_path, monkeypatch, headers=None, latency=0.0, **kw):
    http = Http(cache_path=tmp_path / "c.sqlite", **kw)
    starts, in_flight, peak = [], [0], [0]
    lock = threading.Lock()

    def request(method, url, **_):
        with lock:
            starts.append((url, time.monotonic()))
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
        time.sleep(latency)
        with lock:
            in_flight[0] -= 1
        return Resp(headers)

    monkeypatch.setattr(http.session, "request", request)
    return http, starts, peak


def test_min_interval_holds_across_threads_and_hosts_dont_block(tmp_path, monkeypatch):
    http, starts, _ = make(tmp_path, monkeypatch, min_interval={"a.test": 0.05, "b.test": 0.0, "default": 0},
                           max_in_flight={"default": 8})
    urls = [f"https://a.test/{i}" for i in range(6)] + [f"https://b.test/{i}" for i in range(6)]
    with ThreadPoolExecutor(12) as pool:
        list(pool.map(http.get, urls))
    a = sorted(t for u, t in starts if "a.test" in u)
    b = sorted(t for u, t in starts if "b.test" in u)
    assert all(y - x >= 0.045 for x, y in zip(a, a[1:], strict=False))
    assert b[-1] - b[0] < 0.05  # b isn't queued behind a's spacing


def test_max_in_flight_caps_concurrency(tmp_path, monkeypatch):
    http, _, peak = make(tmp_path, monkeypatch, latency=0.05, min_interval={"default": 0},
                         max_in_flight={"musicbrainz.org": 1, "default": 8})
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(http.get, [f"https://musicbrainz.org/{i}" for i in range(6)]))
    assert peak[0] == 1


def test_lb_ratelimit_headers_pause_the_host(tmp_path, monkeypatch):
    http, starts, _ = make(tmp_path, monkeypatch, headers={"X-RateLimit-Remaining": "1", "X-RateLimit-Reset-In": "0.3"},
                           min_interval={"default": 0})
    http.get("https://api.listenbrainz.org/1")
    http.get("https://api.listenbrainz.org/2")
    assert starts[1][1] - starts[0][1] >= 0.45  # reset_in + 0.2 margin


def test_deezer_no_data_error_is_cached_not_retried(tmp_path, monkeypatch):
    http = Http(cache_path=tmp_path / "c.sqlite")
    calls = []

    class R(Resp):
        def json(self):
            return {"error": {"type": "DataException", "message": "no data", "code": 800}}

    monkeypatch.setattr(http.session, "request", lambda *a, **k: calls.append(1) or R())
    slept = []
    monkeypatch.setattr("postradio.http.time.sleep", slept.append)
    assert http.get("https://api.deezer.com/artist/1/radio")["error"]["code"] == 800
    assert http.get("https://api.deezer.com/artist/1/radio")["error"]["code"] == 800
    assert len(calls) == 1 and sum(slept) < 1


def test_transient_deezer_errors_are_not_cached(tmp_path, monkeypatch):
    http = Http(cache_path=tmp_path / "c.sqlite")
    calls = []

    class Busy(Resp):
        def json(self):
            return {"error": {"type": "Exception", "message": "service busy", "code": 700}}

    monkeypatch.setattr(http.session, "request", lambda *a, **k: calls.append(1) or Busy())
    http.get("https://api.deezer.com/artist/1/related")
    http.get("https://api.deezer.com/artist/1/related")
    assert len(calls) == 2  # asked again: a busy spell must not blank an artist for a week


def test_two_instances_share_one_schedule_per_host(tmp_path, monkeypatch):
    lim = {"rate.test": 0.05, "default": 0}
    a, starts_a, _ = make(tmp_path, monkeypatch, min_interval=lim)
    b = Http(cache_path=tmp_path / "c2.sqlite", min_interval=lim)
    monkeypatch.setattr(b.session, "request", a.session.request)  # same recorder
    with ThreadPoolExecutor(6) as pool:
        list(pool.map(lambda i: (a if i % 2 else b).get(f"https://rate.test/{i}"), range(6)))
    ts = sorted(t for _, t in starts_a)
    assert all(y - x >= 0.045 for x, y in zip(ts, ts[1:], strict=False))  # spacing held across both instances


def test_final_attempt_does_not_sleep(tmp_path, monkeypatch):
    http = Http(cache_path=tmp_path / "c.sqlite", max_attempts={"default": 2})

    class R503(Resp):
        def __init__(self):
            super().__init__()
            self.status_code = 503

    monkeypatch.setattr(http.session, "request", lambda *a, **k: R503())
    slept = []
    monkeypatch.setattr("postradio.http.time.sleep", slept.append)
    assert http.get("https://x.test/a") is None
    # attempt 1 fails -> 2 s backoff; attempt 2 waits ~2 s for its pushed-back slot, fails, and returns at once
    # (the old code slept another 4 s of backoff after the final attempt)
    assert slept[0] == 2.0 and len(slept) == 2 and sum(slept) < 4.1


def test_old_cache_rows_are_evicted(tmp_path, monkeypatch):
    http = Http(cache_path=tmp_path / "c.sqlite", keep_s=100)
    http._store("old", {"v": 1})
    http._store("new", {"v": 2})
    with http._db_lock:
        http.db.execute("UPDATE c SET t = t - 1000 WHERE k = 'old'")
        http.db.commit()
    assert http.evict() == 1
    assert http._cached("old") is None and http._cached("new") is not None
