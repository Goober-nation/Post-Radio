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
