"""Adapter tests: canned API payloads in, normalised models out."""
from pvrecs.http import Http
from pvrecs.models import Artist, History, Play, Track, track_key
from pvrecs.providers.deezer import Deezer
from pvrecs.providers.history import FileHistory
from pvrecs.providers.lb_playlists import LBCreatedFor
from pvrecs.providers.listenbrainz import troi_window
from pvrecs.providers.musicbrainz import MusicBrainz


class CannedHttp:
    def __init__(self, routes):
        self.routes = routes  # url suffix -> payload (or callable(params) -> payload)

    def get(self, url, params=None, headers=None, shape=None, max_age=None, cache_params=None, valid=None):
        for suffix, payload in self.routes.items():
            if url.endswith(suffix):
                return payload(params or {}) if callable(payload) else payload
        return None

    def post(self, url, body, headers=None):
        return self.get(url)


def dz_track(i, did, title, rank=1000):
    return {"id": i, "title": title, "title_short": title, "rank": rank, "artist": {"id": did, "name": "Bilal"}}


def test_deezer_name_collision_resolved_by_known_titles():
    http = CannedHttp({
        "/search/artist": {"data": [{"id": 1, "name": "Bilal", "nb_fan": 9_000_000},
                                    {"id": 2, "name": "Bilal", "nb_fan": 50_000}]},
        "/artist/1/top": {"data": [dz_track(10 + i, 1, f"rai {i}") for i in range(5)]},
        "/artist/2/top": {"data": [dz_track(20 + i, 2, t) for i, t in enumerate(["Soul Sista", "Sometimes", "x"])]},
    })
    dz = Deezer(http, known_titles=lambda a: {"soulsista", "sometimes"})
    a = Artist("Bilal", mbid="m")
    assert dz.resolve(a) == 2 and a.popularity is not None
    assert dz.resolve(Artist("Bilal")) == 1  # no MBID: most-followed exact match


def test_deezer_top_tracks_normalised_within_artist():
    http = CannedHttp({"/artist/5/top": {"data": [dz_track(i, 5, f"t{i}") for i in range(3)]
                                         + [dz_track(9, 6, "feature")]}})
    tracks = Deezer(http).top_tracks(Artist("X", deezer_id=5), 10)
    assert [t.popularity for t in tracks] == [1.0, 0.5, 0.0]
    assert all(isinstance(t, Track) and t.sources == {"deezer-top"} for t in tracks)


def test_mb_tags_skip_junk_and_fall_back_to_release_groups():
    http = CannedHttp({
        "/artist/m": {"tags": [{"name": "_interview is member only?", "count": 1}]},
        "/release-group": {"release-groups": [{"tags": [{"name": "shoegaze", "count": 2},
                                                        {"name": "rock", "count": 1}]}]},
    })
    assert MusicBrainz(http).tags(Artist("B", mbid="m")) == ["shoegaze", "rock"]


def test_troi_window_tracks_popularity():
    assert troi_window(1.0) == (0, 34)  # troi percentile 0 = most played
    assert troi_window(0.0)[1] == 100


def test_history_matches_by_mbid_or_name():
    h = History.from_plays([Play("Radiohead", "Creep", artist_mbid="r")])
    assert h.plays(Artist("radiohead")) == 1 and h.plays(Artist("Other", mbid="r")) == 1
    assert h.heard(Track("Creep (Remastered)", Artist("Radiohead")))
    assert track_key("A", "Song [Live]") == track_key("a", "song")


def test_file_history_jsonl_and_csv(tmp_path):
    j = tmp_path / "plays.jsonl"
    j.write_text('{"artist": "Beachglass", "title": "Glass", "timestamp": 1}\n\n{"artist": "X", "track": "Y"}\n')
    c = tmp_path / "plays.csv"
    c.write_text("artist,title,artist_mbid\nBeachglass,Glass,\nBeachglass,Lay Low,\n")
    assert FileHistory(j).load().plays(Artist("Beachglass")) == 1
    hc = FileHistory(c).load()
    assert hc.plays(Artist("Beachglass")) == 2 and hc.heard(Track("Lay Low", Artist("Beachglass")))


def test_http_backs_off_on_503_instead_of_failing(tmp_path, monkeypatch):
    class R:
        def __init__(self, code):
            self.status_code, self.headers = code, {}

        def json(self):
            return {"ok": 1}

    codes = iter([503, 503, 200])
    http = Http(cache_path=tmp_path / "c.sqlite")
    monkeypatch.setattr(http.session, "request", lambda *a, **k: R(next(codes)))
    slept = []
    monkeypatch.setattr("pvrecs.http.time.sleep", slept.append)
    assert http.get("https://musicbrainz.org/ws/2/artist/x") == {"ok": 1}
    assert http.failures == 0 and sum(slept) >= 6  # 2s + 4s exponential backoff


def test_lb_created_for_splits_weeks_and_filters_kind():
    def pl(mbid, date, patch):
        return {"playlist": {"identifier": f"https://listenbrainz.org/playlist/{mbid}", "date": date + "T00:00:00",
                             "title": f"{patch} {date}", "extension": {
                                 "https://musicbrainz.org/doc/jspf#playlist": {
                                     "additional_metadata": {"algorithm_metadata": {"source_patch": patch}}}}}}

    def body(title):
        return {"playlist": {"track": [{"title": title, "creator": "Kendrick Lamar",
                                        "identifier": ["https://musicbrainz.org/recording/r1"],
                                        "extension": {"https://musicbrainz.org/doc/jspf#track": {
                                            "additional_metadata": {"artists": [{"artist_mbid": "k"}]}}}}]}}

    http = CannedHttp({
        "/playlists/createdfor": {"playlists": [pl("old", "2026-08-24", "weekly-exploration"),
                                                pl("new", "2026-09-14", "weekly-exploration"),
                                                pl("jam", "2026-09-14", "weekly-jams")]},
        "/playlist/new": body("New song"), "/playlist/old": body("Old song"),
    })
    weeks = LBCreatedFor(http).playlists("u", "weekly-exploration")
    assert [w.week for w in weeks] == ["2026-09-14", "2026-08-24"]
    t = weeks[0].tracks[0]
    assert (t.title, t.artist.name, t.artist.mbid, t.recording_mbid) == ("New song", "Kendrick Lamar", "k", "r1")
