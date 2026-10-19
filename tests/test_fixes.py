"""Taste reload via version(), store tail reads, typed feedback API, seeded troi, unheard override, genre logs."""
import random

from fakes import Resolver, Similar, Tracks, artist
from fastapi.testclient import TestClient
from test_api import rec  # noqa: F401  (fixture)

from postradio.engine import Params, Providers, Radio
from postradio.models import Artist
from postradio.providers import GenreSource
from postradio.providers.feedback_store import JsonFeedbackStore
from postradio.providers.listenbrainz import _troi_pick
from postradio.server import create_app


def test_engine_reloads_taste_when_store_changes(tmp_path):
    store = JsonFeedbackStore(tmp_path / "o.jsonl")
    r = Radio(Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()], feedback=[store]),
              log=lambda *_: None)
    assert r.taste().artist(Artist("Alpha")) == 0
    store.import_batch({"hates": {"artists": ["Alpha"]}})
    assert r.taste().artist(Artist("Alpha")) < 0  # picked up without anyone touching engine internals
    first = r.taste()
    assert r.taste() is first  # unchanged version -> cached


def test_store_reads_only_the_tail(tmp_path):
    store = JsonFeedbackStore(tmp_path / "o.jsonl", depth=3)
    for i in range(50):
        store.import_batch({"loves": {"artists": [f"A{i}"]}})
    assert sorted(f.artist for f in store.load()) == ["A47", "A48", "A49"]
    assert store._tail(3, block=16) == store.path.read_text().splitlines()[-3:]  # tiny blocks, same answer
    assert len(store.batches()) == 50 and store.index_path.exists()


def test_feedback_endpoint_is_typed(rec):  # noqa: F811
    client = TestClient(create_app(rec))
    ok = client.post("/feedback", json={"loves": {"artists": ["X", {"name": "Y"}],
                                                  "tracks": [{"artist": "Z", "title": "T"}]}})
    assert ok.status_code == 200 and ok.json()["items"] == 3
    assert client.post("/feedback", json={"likes": {}}).status_code == 422
    assert client.post("/feedback", json={"loves": {"tracks": [{"artist": "Z"}]}}).status_code == 422
    schema = client.get("/openapi.json").json()["components"]["schemas"]
    assert {"FeedbackImport", "FeedbackSection", "TrackRef"} <= set(schema)


def test_troi_pick_is_reproducible_and_stays_in_window():
    mbids = [f"m{i}" for i in range(100)]
    a = _troi_pick(mbids, 20, 40, 5, random.Random(7))
    assert len(a) == 5 and a == _troi_pick(mbids, 20, 40, 5, random.Random(7))
    assert len(_troi_pick([f"m{i}" for i in range(8)], 20, 54, 8, random.Random(1))) == 3  # LB's usual 8-per-artist
    assert all(20 <= int(m[1:]) < 40 for m in a)


def test_unheard_none_overrides_config_default(rec):  # noqa: F811
    import dataclasses

    rec.cfg = dataclasses.replace(rec.cfg, defaults=dataclasses.replace(rec.cfg.defaults, unheard="artists"))
    assert rec._unheard(None) == "artists" and rec._unheard("none") is None and rec._unheard("tracks") == "tracks"


class FixedGenres(GenreSource):
    name = "fixed"

    def supported_genres(self):
        return ["Rock", "Jazz"]

    def unsupported(self, genres):
        return [g for g in genres if g not in ("rock", "jazz")]

    def genre_artists(self, genres, op, popularity, limit):
        return [] if op == "and" and self.unsupported(genres) else [artist("B")]


def test_unsupported_genre_is_logged_with_supported_list():
    logs = []
    r = Radio(Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()], genres=[FixedGenres()]),
              log=logs.append)
    r.generate(Params(0.3, n=5, seed=1), genres=["rock", "shoegaze"], genre_op="and")
    line = next(m for m in logs if m.startswith("genre: fixed"))
    assert "shoegaze" in line and "skipped (and)" in line and "Rock, Jazz" in line
