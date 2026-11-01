"""MusicBrainz local store: dump import, name/alias resolution, genres, and the adapter's store-first path."""
import io
import json
import sqlite3
import tarfile

from postradio.models import Artist
from postradio.providers.musicbrainz import MusicBrainz
from postradio.providers.musicbrainz_store import (
    INDEX_FILE,
    SCHEMA,
    STORE_FILE,
    ArtistStore,
    GenreIndex,
    import_dump,
    insert,
    name_key,
)

GRAPH = {"hip hop", "pop rap", "trap", "chanson"}
ARTISTS = [
    {"id": "a", "name": "Sainté", "sort-name": "Sainté", "aliases": [{"name": "Sainte"}],
     "genres": [{"name": "hip hop", "count": 3}, {"name": "not-a-graph-genre", "count": 9}], "tags": []},
    {"id": "b", "name": "Sainte", "sort-name": "Sainte", "disambiguation": "French band", "genres": [],
     "tags": [{"name": "chanson", "count": 1}, {"name": "rock", "count": 5}]},
    {"id": "c", "name": "Nobody", "sort-name": "Nobody", "genres": [], "tags": []},
    {"id": "d", "name": "Twista", "sort-name": "Twista", "aliases": [{"name": "Tung Twista"}],
     "genres": [{"name": "hip hop", "count": 7}, {"name": "trap", "count": 1}, {"name": "pop rap", "count": 1}]},
    {"id": "ye", "name": "Ye", "sort-name": "Ye", "aliases": [{"name": "Kanye West"}],
     "genres": [{"name": "hip hop", "count": 20}, {"name": "pop rap", "count": 4}]},
    {"name": "no id: skipped"},
]


def make_store(artists=ARTISTS) -> ArtistStore:
    """A tiny in-memory store built through the importer's own insert(), so tests cover the real transform."""
    s = ArtistStore(":memory:")
    s.con.executescript(SCHEMA)
    insert(s.con, artists, GRAPH)
    return s


class NoHttp:
    """Fails the test if the adapter goes to the API when the store should answer."""

    def __init__(self, routes=None):
        self.routes, self.calls = routes or {}, []

    def get(self, url, params=None, **_):
        self.calls.append(url)
        for suffix, payload in self.routes.items():
            if url.endswith(suffix):
                return payload(params or {}) if callable(payload) else payload
        return None


def test_name_key_folds_case_diacritics_punctuation_but_keeps_scripts():
    assert name_key("SAINTÉ!") == name_key("sainte") == "sainte"
    assert name_key("ЛСП") == "лсп"
    assert name_key("!!!") == "!!!"


def test_genres_restricted_to_graph_with_tag_fallback():
    s = make_store()
    assert s.genres("a") == ["hip hop"]
    assert s.genres("b") == ["chanson"]  # from tags, since genres is empty
    assert s.genres("c") == [] and s.genres("zzz") == []
    assert s.genres("d") == ["hip hop", "pop rap", "trap"]  # ties broken by name


def test_resolve_matches_aliases_and_ranks_by_votes():
    s = make_store()
    got = s.resolve("sainte")
    assert [c["mbid"] for c in got] == ["a", "b"] and got[1]["disambiguation"] == "French band"
    assert [c["mbid"] for c in s.resolve("tung twista")] == ["d"]
    assert s.resolve("missing") == []


def test_genre_index_ranks_by_genre_votes_and_caps():
    s = make_store()
    assert s.genre_index(["hip hop", "chanson", "jazz"], cap=1) == {"hip hop": [["ye", "Ye"]],
                                                                    "chanson": [["b", "Sainte"]]}


def test_import_dump_streams_the_archive(tmp_path):
    body = "\n".join(json.dumps(a) for a in ARTISTS).encode() + b"\nnot json\n"
    archive = tmp_path / "artist.tar.xz"
    with tarfile.open(archive, "w:xz") as tar:
        info = tarfile.TarInfo("mbdump/artist")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    out = tmp_path / "data" / STORE_FILE
    stats = import_dump(archive, out, GRAPH, log=lambda _m: None)
    assert stats["artists"] == 5 and stats["with_genres"] == 4
    s = ArtistStore.open(tmp_path / "data")
    assert s.resolve("Kanye West")[0]["mbid"] == "ye"
    with sqlite3.connect(out) as con:  # the store is opened read-only by the app
        assert con.execute("SELECT count(*) FROM artists").fetchone() == (5,)


def test_adapter_resolves_aliases_and_genres_locally():
    http = NoHttp()
    mb = MusicBrainz(http, store=make_store())
    assert mb.find_artist("Kanye West") == Artist("Ye", mbid="ye")
    assert mb.artist_by_mbid("d") == Artist("Twista", mbid="d")
    assert mb.tags(Artist("Twista", mbid="d"), n=2) == ["hip hop", "pop rap"]
    assert http.calls == []


def test_adapter_falls_back_to_the_api_without_a_store_hit():
    http = NoHttp({"/artist": {"artists": [{"id": "x", "name": "Somebody"}]}})
    mb = MusicBrainz(http, store=make_store())
    assert mb.find_artist("Somebody") == Artist("Somebody", mbid="x")
    assert MusicBrainz(http).find_artist("Somebody") == Artist("Somebody", mbid="x")  # no store at all
    assert len(http.calls) == 2


def test_adapter_genre_artists_or_and_unsupported():
    mb = MusicBrainz(NoHttp(), store=make_store())
    assert [a.mbid for a in mb.genre_artists(["hip hop"], "or", 0.5, 10)] == ["ye", "d", "a"]
    assert [a.mbid for a in mb.genre_artists(["hip hop", "chanson"], "or", 0.5, 2)] == ["ye", "b"]  # interleaved
    assert [a.mbid for a in mb.genre_artists(["Hip Hop", "trap"], "and", 0.5, 10)] == ["d"]
    assert mb.unsupported(["hip hop", "polka"]) == ["polka"]
    plain = MusicBrainz(NoHttp())
    assert plain.genre_artists(["hip hop"], "or", 0.5, 10) == [] and plain.unsupported(["hip hop"]) == ["hip hop"]


def test_adapter_genre_artists_from_the_small_index(tmp_path):
    (tmp_path / INDEX_FILE).write_text(json.dumps({"genres": {"hip hop": [["ye", "Ye"], ["d", "Twista"], ["bad"]]}}))
    mb = MusicBrainz(NoHttp(), index=GenreIndex.open(tmp_path))
    assert [a.name for a in mb.genre_artists(["hip hop"], "or", 0.5, 10)] == ["Ye", "Twista"]
    assert GenreIndex.open(tmp_path / "missing") is None and ArtistStore.open(tmp_path) is None


def test_genre_names_pages_through_the_api():
    pages = {0: {"genre-count": 103, "genres": [{"id": "1", "name": "rock"}, {"id": "2", "name": "pop"}]},
             100: {"genre-count": 103, "genres": [{"id": "3", "name": "jazz"}, {"id": None, "name": "bad"}]}}
    mb = MusicBrainz(NoHttp({"/genre/all": lambda p: pages[p["offset"]]}))
    assert mb.genre_names() == {"1": "rock", "2": "pop", "3": "jazz"}


def test_local_view_never_calls_the_api():
    http = NoHttp({"/artist": {"artists": [{"id": "x", "name": "Somebody"}]}})
    local = MusicBrainz(http, store=make_store()).local()
    assert local.find_artist("Kanye West") == Artist("Ye", mbid="ye")
    assert local.find_artist("Somebody") is None and local.tags(Artist("Nobody", mbid="c")) == []
    assert http.calls == []
