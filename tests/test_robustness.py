"""Malformed payloads and failing providers degrade a run; they never abort it."""
from fakes import Resolver, Similar, Tracks
from test_providers import CannedHttp

from postradio.engine import Params, Providers, Radio
from postradio.models import Artist
from postradio.providers import HistoryProvider, SimilarArtists, parse_rows
from postradio.providers.deezer import Deezer
from postradio.providers.feedback import LBFeedback, NavidromeFeedback
from postradio.providers.history import FileHistory, ListenBrainzHistory, _timestamp
from postradio.providers.lb_playlists import LBCreatedFor
from postradio.providers.listenbrainz import LBMetadata, LBSimilar, LBSimilarRecordings, LBTopTracks
from postradio.providers.musicbrainz import MusicBrainz

JUNK = [None, 3, "x", [], {}, {"id": None}, {"title": None}]


def test_parse_rows_skips_bad_rows_and_bad_containers():
    assert parse_rows([{"a": 1}, {}, None, {"a": 2}], lambda r: r["a"]) == [1, 2]
    assert parse_rows(None, lambda r: r) == [] and parse_rows({"not": "a list"}, lambda r: r) == []


def test_deezer_survives_malformed_rows():
    good = {"id": 5, "title": "Ok", "title_short": "Ok", "rank": 10, "artist": {"id": 1, "name": "A"}}
    http = CannedHttp({"/artist/1/top": {"data": JUNK + [good]}, "/artist/1/radio": {"data": JUNK + [good]},
                       "/artist/1/related": {"data": JUNK + [{"id": 2, "name": "B", "nb_fan": 3}]},
                       "/genre": {"data": JUNK + [{"id": 116, "name": "Rap/Hip Hop"}]},
                       "/genre/116/artists": {"data": JUNK + [{"id": 2, "name": "B"}]}})
    dz = Deezer(http)
    a = Artist("A", deezer_id=1)
    assert [t.title for t in dz.top_tracks(a, 10)] == ["Ok"]
    assert [t.title for t in dz.radio(a, 0.5, "easy")] == ["Ok"]
    assert [e.target.name for e in dz.neighbours(a, 5)] == ["B"]
    assert [x.name for x in dz.genre_artists(["hip hop"], "or", 0.5, 5)] == ["B"]


def test_listenbrainz_adapters_survive_malformed_rows():
    http = CannedHttp({
        "/similar-artists/json": JUNK + [{"artist_mbid": "b", "name": "B", "score": None}],
        "/similar-recordings/json": JUNK + [{"recording_mbid": "r", "recording_name": "R", "artist_credit_name": "X"}],
        "/top-recordings-for-artist/m": JUNK + [{"recording_mbid": "r", "recording_name": "R", "total_user_count": 50}],
        "/metadata/recording/": {"r": {"recording": {"name": "R"}, "artist": {"artists": [{"name": "X"}]}},
                                 "bad": {"recording": None}, "worse": None},
    })
    assert [e.target.name for e in LBSimilar(http).neighbours(Artist("A", mbid="a"), 5)] == ["B"]
    from postradio.models import Track
    assert len(LBSimilarRecordings(http).similar(Track("t", Artist("A"), recording_mbid="s"), 5)) == 1
    assert [t.title for t in LBTopTracks(http, "tok").top_tracks(Artist("A", mbid="m"))] == ["R"]
    assert list(LBMetadata(http).recordings(["r", "bad", "worse"])) == ["r"]


def test_musicbrainz_and_playlists_and_feedback_survive_malformed_rows():
    mb_http = CannedHttp({"/artist": {"artists": JUNK + [{"id": "1", "name": "A"}]},
                          "/recording": {"recordings": JUNK + [
                              {"id": "r", "title": "T", "artist-credit": [{"artist": {"id": "1", "name": "A"}}]}]}})
    mb = MusicBrainz(mb_http)
    assert mb.find_artist("A").mbid == "1" and mb.find_recording("A", "T").recording_mbid == "r"
    pl = {"playlists": JUNK + [{"playlist": {"identifier": "https://lb/playlist/p1", "date": "2026-10-01T00:00",
                                             "extension": {"https://musicbrainz.org/doc/jspf#playlist": {
                                                 "additional_metadata": {"algorithm_metadata": {
                                                     "source_patch": "weekly-exploration"}}}}}}]}
    lb = CannedHttp({"/playlists/createdfor": pl, "/playlist/p1": {"playlist": {"track": JUNK + [
        {"title": "T", "creator": "A", "identifier": ["https://mb/recording/r"]}]}}})
    weeks = LBCreatedFor(lb).playlists("u")
    assert len(weeks) == 1 and [t.title for t in weeks[0].tracks] == ["T"]

    def fb(params):
        rows = [None, {"track_metadata": None}, {"track_metadata": {"artist_name": "A", "track_name": "T"}}]
        return {"feedback": rows if not params["offset"] else []}
    assert len(LBFeedback(CannedHttp({"/get-feedback": fb}), "u").load()) == 2  # one love, one hate
    ok = {"status": "ok"}
    nd = CannedHttp({"/rest/getStarred2": {"subsonic-response": {**ok, "starred2": {
        "song": JUNK + [{"id": "1", "title": "T", "artist": "A", "starred": "x"}], "artist": JUNK, "album": JUNK}}},
        "/rest/search3": {"subsonic-response": {**ok, "searchResult3": {"song": JUNK + [
            {"id": "2", "title": "U", "artist": "A", "userRating": "five"}]}}},
        "/rest/getArtists": {"subsonic-response": {**ok, "artists": {"index": JUNK}}}})
    assert [(f.title, f.score) for f in NavidromeFeedback(nd, "http://nd", "u", "p").load()] == [("T", 1.0)]


def test_lb_history_skips_bad_rows():
    def page(kind):
        return lambda params: {"payload": {kind: JUNK + [
            {"artist_name": "A", "artist_mbid": "a", "listen_count": "7", "track_name": "T"}]}}
    h = ListenBrainzHistory(CannedHttp({"/artists": page("artists"), "/recordings": page("recordings")}), "u").load()
    assert h.plays(Artist("A")) == 7


def test_timestamps_in_any_common_format(tmp_path):
    assert _timestamp("1700000000") == 1700000000 and _timestamp(1700000000.9) == 1700000000
    assert _timestamp("2023-11-14T22:13:20Z") == 1700000000 and _timestamp("2023-11-14 22:13:20") == 1700000000
    assert _timestamp("yesterday") is None and _timestamp(None) is None
    f = tmp_path / "plays.csv"
    f.write_text("artist,title,timestamp\nA,T,2023-11-14T22:13:20Z\nA,U,garbage\nA,V,1700000000.5\n")
    assert FileHistory(f).load().plays(Artist("A")) == 3


class Exploding(SimilarArtists):
    name = "boom"

    def neighbours(self, artist, limit):
        raise RuntimeError("upstream 500")


class BrokenHistory(HistoryProvider):
    def load(self):
        raise ValueError("invalid literal for int()")


def test_failing_provider_and_history_degrade_instead_of_aborting():
    logs = []
    r = Radio(Providers(resolver=Resolver(), similar=[Exploding(), Similar()], tracks=[Tracks()],
                        history=BrokenHistory()), log=logs.append)
    res = r.generate(Params(0.5, n=5, seed=1), artist="Seed")
    assert res["tracks"]  # the healthy similarity source and catalogue still produce a playlist
    assert any("upstream 500" in m for m in logs) and any("history unavailable" in m for m in logs)
    r.close()
