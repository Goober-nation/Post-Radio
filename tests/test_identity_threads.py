"""Deezer identity checks, de-duplicated resolution, read-only title lookups, bounded worker pool."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fakes import Resolver, Similar, Tracks
from test_providers import CannedHttp, dz_track

from pvrecs.engine import Params, Providers, Radio
from pvrecs.models import Artist
from pvrecs.providers.deezer import Deezer
from pvrecs.providers.listenbrainz import LBTopTracks


def one_namesake(titles):
    return CannedHttp({"/search/artist": {"data": [{"id": 9, "name": "Bilal", "nb_fan": 5}]},
                       "/artist/9/top": {"data": [dz_track(i, 9, t) for i, t in enumerate(titles)]}})


def test_single_namesake_rejected_when_free_titles_disagree():
    dz = Deezer(one_namesake(["rai 1", "rai 2"]), cheap_titles=lambda a: {"soulsista"})
    assert dz.resolve(Artist("Bilal", mbid="m")) is None


def test_single_match_accepted_with_one_shared_title_or_no_free_titles():
    assert Deezer(one_namesake(["Soul Sista", "x"]), cheap_titles=lambda a: {"soulsista"}).resolve(
        Artist("Bilal", mbid="m")) == 9
    slow = []
    dz = Deezer(one_namesake(["x"]), known_titles=lambda a: slow.append(1) or set())
    assert dz.resolve(Artist("Bilal", mbid="m")) == 9 and not slow  # MB never consulted for a single hit


def test_parallel_resolution_looks_up_each_artist_once():
    searches = []

    class SlowHttp(CannedHttp):
        def get(self, url, *a, **k):
            if url.endswith("/search/artist"):
                searches.append(1)
                time.sleep(0.05)
            return super().get(url, *a, **k)

    dz = Deezer(SlowHttp({"/search/artist": {"data": [{"id": 3, "name": "X", "nb_fan": 1}]}}))
    artists = [Artist("X", mbid="x") for _ in range(8)]  # distinct objects, same artist
    with ThreadPoolExecutor(8) as pool:
        assert set(pool.map(dz.resolve, artists)) == {3}
    assert len(searches) == 1


def test_lb_titles_do_not_touch_the_artist():
    http = CannedHttp({"/top-recordings-for-artist/m": [{"recording_mbid": "r", "recording_name": "Song",
                                                         "total_user_count": 1000}]})
    a = Artist("A", mbid="m")
    assert LBTopTracks(http, "t").titles(a) == {"song"} and a.popularity is None


def test_one_shared_pool_bounds_concurrency():
    live, peak, lock = [0], [0], threading.Lock()

    class Slow(Similar):
        def neighbours(self, artist, limit):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.02)
            with lock:
                live[0] -= 1
            return super().neighbours(artist, limit)

    r = Radio(Providers(resolver=Resolver(), similar=[Slow()] * 6, tracks=[Tracks()]), log=lambda *_: None)
    r.a.fetch_workers = 2
    with ThreadPoolExecutor(2) as outer:  # two callers sharing one Radio
        list(outer.map(lambda _: r.build_graph(Artist("Seed", mbid="S"), Params(1.0)), range(2)))
    assert peak[0] <= 2 and r._pool() is r._pool()
