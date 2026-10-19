"""Concurrent generate() calls on one shared Radio must not see each other's per-run state."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fakes import Resolver, Similar, Tracks, artist

from postradio.engine import Params, Providers, Radio
from postradio.providers import GenreSource


class SlowGenres(GenreSource):
    """Records the genre list each call sees; sleeps so requests overlap."""

    name = "slow"

    def __init__(self):
        self.seen, self.lock = [], threading.Lock()

    def genre_artists(self, genres, op, popularity, limit):
        time.sleep(0.05)
        return [artist("B")]

    def genre_tracks(self, genres, op, popularity, limit):
        with self.lock:
            self.seen.append(list(genres))
        time.sleep(0.05)
        return []


def test_concurrent_requests_keep_their_own_genres_and_seed_state():
    gs = SlowGenres()
    r = Radio(Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()], genres=[gs]),
              log=lambda *_: None)

    def genre_run(_):
        return r.generate(Params(0.5, n=5, seed=1), genres=["shoegaze"])

    def artist_run(_):
        return r.generate(Params(0.5, n=5, seed=1), artist="Seed")

    with ThreadPoolExecutor(8) as pool:
        futs = [pool.submit(genre_run if i % 2 else artist_run, i) for i in range(12)]
        results = [f.result() for f in futs]
    assert gs.seen and all(g == ["shoegaze"] for g in gs.seen)  # never another request's (empty) genres
    for i, res in enumerate(results):
        if i % 2:
            assert res["genres"] == ["shoegaze"] and res["seed"]["artist"] is None
        else:
            assert res["genres"] is None and res["seed"]["artist"] == "Seed" and res["seed"]["popularity"] == 0.95
    r.close()
