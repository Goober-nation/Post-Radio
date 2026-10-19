"""Unheard levels, genre seeds and the loves/hates JSON store."""
import json
from datetime import UTC, datetime

import pytest
from fakes import NAMES, Resolver, Similar, Tracks, artist
from test_api import rec  # noqa: F401  (fixture)
from test_engine import StaticHistory

from postradio.engine import Params, Providers, Radio
from postradio.models import History, Play
from postradio.providers import GenreSource
from postradio.providers.feedback_store import JsonFeedbackStore


class Genres(GenreSource):
    name = "fake-genres"

    def genre_artists(self, genres, op, popularity, limit):
        return [artist("B"), artist("C")] if op == "or" or genres == ["shoegaze"] else [artist("C")]


def radio(history=None, genres=None):
    prov = Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()],
                     history=StaticHistory(history or History()), genres=genres or [])
    return Radio(prov, log=lambda *_: None)


def test_unheard_levels():
    h = History.from_plays([Play("Alpha", "Alpha song 0"), Play("Beta", "Beta song 9")])
    p = Params(0.0, n=40, seed=1, unheard="tracks")  # explore 0 would otherwise only penalise played tracks
    res = radio(h).generate(p, artist="Seed")["tracks"]
    assert "Alpha song 0" not in {t["title"] for t in res} and any(t["artist"] == "Alpha" for t in res)
    p.unheard = "artists"
    assert not {"Alpha", "Beta"} & {t["artist"] for t in radio(h).generate(p, artist="Seed")["tracks"]}


def test_genre_seed_builds_graph_from_genre_artists():
    res = radio(genres=[Genres()]).generate(Params(0.5, n=10, seed=1), genres=["shoegaze", "dream pop"])
    artists = {t["artist"] for t in res["tracks"]}
    assert artists <= {"Beta", "Gamma"} and artists
    assert res["seed"]["artist"] is None and res["genres"] == ["shoegaze", "dream pop"]
    assert all(t["path"][0] == "shoegaze or dream pop" for t in res["tracks"])
    and_res = radio(genres=[Genres()]).generate(Params(0.5, n=10, seed=1), genres=["a", "b"], genre_op="and")
    assert {t["artist"] for t in and_res["tracks"]} <= {"Gamma"}


def test_feedback_store_appends_dated_batches_newest_wins(tmp_path):
    store = JsonFeedbackStore(tmp_path / "fb" / "opinions.jsonl")
    b1 = store.import_batch({"loves": {"artists": ["Beta"], "tracks": [{"artist": "Alpha", "title": "Alpha song 1"}]},
                             "hates": {"albums": [{"artist": "Gamma", "title": "G"}]}},
                            now=datetime(2026, 10, 1, tzinfo=UTC))
    assert b1["items"] == 3 and b1["imported_at"].startswith("2026-10-01")
    store.import_batch({"hates": {"artists": ["Beta"]}}, now=datetime(2026, 10, 20, tzinfo=UTC))
    got = {(f.artist, f.title): f.score for f in store.load()}
    assert got[("Beta", None)] == -1.0  # newer batch wins
    assert got[("Alpha", "Alpha song 1")] == 1.0 and got[("Gamma", None)] == -0.5  # album = half, artist-level
    assert [b["imported_at"][:10] for b in store.batches()] == ["2026-10-01", "2026-10-20"]
    assert len((tmp_path / "fb" / "opinions.jsonl").read_text().splitlines()) == 4  # nothing rewritten


def test_feedback_store_depth_reads_only_newest(tmp_path):
    store = JsonFeedbackStore(tmp_path / "o.jsonl", depth=1)
    store.import_batch({"loves": {"artists": ["Old"]}})
    store.import_batch({"loves": {"artists": ["New"]}})
    assert [f.artist for f in store.load()] == ["New"]


@pytest.mark.parametrize("bad", [[], {"likes": {}}, {"loves": {"songs": []}}, {"loves": {"tracks": ["x"]}},
                                 {"loves": {"albums": [{"artist": "a"}]}}, {"loves": {}}])
def test_feedback_store_rejects_bad_input(tmp_path, bad):
    with pytest.raises(ValueError):
        JsonFeedbackStore(tmp_path / "o.jsonl").import_batch(bad)


def test_api_import_feedback_lands_in_save_dir_and_feeds_taste(rec):  # noqa: F811
    rec.radio.p.feedback = [rec.feedback_store]
    out = rec.import_feedback({"hates": {"artists": ["Alpha"]}})
    assert rec.feedback_store.path.parent.parent == rec.save_dir and out["items"] == 1
    res = rec.generate(artist="Seed", n=10)
    assert all(t["taste"]["artist"] < 0 for t in res["tracks"] if t["artist"] == "Alpha")
    assert json.loads(rec.feedback_store.path.read_text().splitlines()[0])["kind"] == "artist"
    assert NAMES  # fakes import used
