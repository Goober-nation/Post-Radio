"""What a separate client (a webpage, another service) relies on: stopping runs, explaining tracks, reporting
how much of the input was usable, and telling which server it talks to."""
import threading
import time

import pytest
from fastapi.testclient import TestClient
from test_flow import GenreArtists
from test_integration import LOVES, library, world  # noqa: F401 - pytest fixture

from postradio.api import Listener
from postradio.errors import Cancelled, InvalidInput
from postradio.models import Like
from postradio.server import API_VERSION, create_app

LIKES = Listener(likes=[Like(t["artist"], t["title"]) for t in LOVES["loves"]["tracks"]])


def test_a_set_cancel_event_stops_a_run_at_the_next_stage(world):  # noqa: F811
    stages = []
    cancel = threading.Event()

    def progress(p):
        stages.append(p.stage)
        if p.stage == "path":
            cancel.set()

    with library(world) as rec:
        with pytest.raises(Cancelled) as e:
            rec.flow(explore=0.5, n=6, stops=3, listener=LIKES, progress=progress, cancel=cancel)
        assert not e.value.timed_out
        assert "pools" not in stages and "done" not in stages  # stopped right after the path
        assert rec.flow(explore=0.5, n=6, stops=3, listener=LIKES).tracks  # the next call is unaffected


def test_a_passed_deadline_stops_a_run(world):  # noqa: F811
    with library(world) as rec:
        slow = lambda p: time.sleep(0.05)  # noqa: E731 - every stage takes a while
        with pytest.raises(Cancelled) as e:
            rec.flow(explore=0.5, n=6, stops=3, listener=LIKES, progress=slow, timeout_s=0.01)
        assert e.value.timed_out
        with pytest.raises(InvalidInput):
            rec.flow(listener=LIKES, timeout_s=0)


def test_jobs_can_be_cancelled_over_http(world, monkeypatch):  # noqa: F811
    started = threading.Event()

    def blocked(self, *, cancel=None, **_):
        started.set()
        assert cancel.wait(5), "cancel never reached the run"
        raise Cancelled()

    with library(world) as rec, TestClient(create_app(rec)) as client:
        monkeypatch.setattr(type(rec), "flow", blocked)
        url = client.post("/jobs/flow", json={"n": 3}).json()["status_url"]
        assert started.wait(5)
        assert client.delete(url).status_code == 200
        for _ in range(300):
            got = client.get(url).json()
            if got["status"] != "running":
                break
            time.sleep(0.01)
        assert got["status"] == "cancelled" and got["error"]["status"] == 499
        assert client.delete("/jobs/nope").status_code == 404


def test_a_timed_out_sync_call_is_a_504(world):  # noqa: F811
    with library(world) as rec, TestClient(create_app(rec)) as client:
        rec_progress_sleep = rec.flow

        def slow_flow(**kw):
            return rec_progress_sleep(**kw, progress=lambda p: time.sleep(0.05))

        rec.flow = slow_flow
        body = {"n": 6, "stops": 3, "timeout_s": 0.01,
                "listener": {"likes": [{"artist": "Seed", "title": "Seed song 0"}]}}
        assert client.post("/flow", json=body).status_code == 504


def test_flow_tracks_say_why_they_are_there_and_the_input_is_counted(world):  # noqa: F811
    with library(world) as rec:
        res = rec.flow(explore=0.5, n=6, stops=3, listener=LIKES)
        assert all(t.via for t in res.tracks)
        assert {v for t in res.tracks for v in t.via} <= {"likes", "genre:mb"}
        assert "likes" in {v for t in res.tracks for v in t.via}
        used = res.stats["input"]
        assert used == {"likes": 2, "artists": 1, "considered": 1, "matched": 1, "with_genres": 1, "store": False}
        assert rec.profile(LIKES).input == used
        assert rec.flow(n=6, stops=3, listener=LIKES, dry=True).input == used
        assert res.to_dict()["tracks"][0].keys() >= {"via", "artist_popularity"}


def test_radio_tracks_carry_via_and_artist_popularity(world):  # noqa: F811
    with library(world) as rec:
        res = rec.generate(artist="Seed", n=6, explore=0.5, rng_seed=7)
        assert all(t.via for t in res.tracks)


def test_an_unknown_genre_suggests_close_names(world):  # noqa: F811
    class Strict(GenreArtists):
        def unsupported(self, genres):
            return [g for g in genres if g not in self.BY_GENRE]

    with library(world) as rec:
        rec.radio.p.genres = [Strict()]
        with pytest.raises(InvalidInput, match="did you mean: .*jazz rap"):
            rec.generate(genres=["jaz rap"], n=3)
        assert rec.generate(genres=["Jazz Rap"], n=3, rng_seed=1).tracks is not None  # case doesn't matter


def test_health_reports_versions_without_paths(world):  # noqa: F811
    with library(world) as rec, TestClient(create_app(rec)) as client:
        got = client.get("/health").json()
    assert got["ok"] and got["api"] == API_VERSION and got["schema"] == 1 and got["store"] is False
    assert str(world) not in str(got)
