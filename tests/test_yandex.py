"""Yandex adapter: playlist links, paging, host fallback, likes as taste/history/tags, similar artists."""
import dataclasses
from collections import Counter

import pytest
from fakes import Resolver, Similar, Tracks

from postradio import config as config_mod
from postradio.api import Recommender
from postradio.engine import Params, Providers, Radio
from postradio.genres import GenreGraph
from postradio.models import Artist, Track
from postradio.providers.yandex import (
    YandexClient,
    YandexError,
    YandexLikes,
    YandexSimilar,
    genre_table,
    non_artist_genres,
    playlist_path,
)

RU, NET = "https://api.music.yandex.ru", "https://api.music.yandex.net"


def item(i, artists, title, genre="rusrap", ts=None):
    return {"timestamp": ts or f"2026-10-{i + 1:02d}T12:00:00Z",
            "track": {"title": title, "albums": [{"genre": genre}],
                      "artists": [{"id": aid, "name": name} for name, aid in artists]}}


PLAYLIST = {"title": "Мне нравится", "trackCount": 4, "pager": {"perPage": 2}, "tracks": [
    item(0, [("Alpha", 1)], "Alpha song 0", "rap"),
    item(1, [("Kanye West", 2), ("Jay-Z", 3)], "Otis", "foreignrap")]}
PAGE2 = {"tracks": [item(2, [("Alpha", 1)], "Alpha song 1", "indie"),
                    {"track": {"title": "no artists", "artists": []}}, "garbage"]}


class Http:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, **_):
        self.calls.append((url, dict(params or {})))
        v = self.routes.get((url, (params or {}).get("page")))
        return v(url) if callable(v) else v


def playlist_http(blocked_ru=False):
    path = "/users/me/playlists/3"
    page1 = None if blocked_ru else {"result": PLAYLIST}
    return Http({(RU + path, None): page1, (NET + path, None): {"result": PLAYLIST},
                 (RU + path, 1): None if blocked_ru else {"result": PAGE2}, (NET + path, 1): {"result": PAGE2},
                 (RU + path, 2): None, (NET + path, 2): None,
                 (RU + "/artists/1/similar", None): {"result": {"similarArtists": [
                     {"id": 7, "name": "Beta", "genres": ["rap"]},
                     {"id": 8, "name": "VALORANT", "genres": ["videogame"]},
                     {"id": 9, "name": "Gamma", "genres": []}, {"name": "no id"}]}}})


URL = "https://music.yandex.ru/users/me/playlists/3"


def test_playlist_links():
    assert playlist_path("https://music.yandex.ru/playlists/lk.abc-123?utm=x") == "/playlist/lk.abc-123"
    assert playlist_path(URL) == "/users/me/playlists/3"
    assert playlist_path("<iframe src='https://music.yandex.ru/iframe/#/playlist/me/3/'>") == "/users/me/playlists/3"
    with pytest.raises(YandexError):
        playlist_path("https://example.com/playlists/1")


def test_playlist_pages_falls_back_to_net_and_sorts_newest_first():
    logs = []
    pl = YandexClient(playlist_http(blocked_ru=True)).playlist(URL, log=logs.append)
    assert pl.total == 4 and [t.title for t in pl.tracks] == ["Alpha song 1", "Otis", "Alpha song 0"]
    assert pl.tracks[1].artists == (("Kanye West", "2"), ("Jay-Z", "3")) and pl.tracks[1].genre == "foreignrap"
    assert logs == []  # 2 + 3 rows reached trackCount 4: done, nothing missing
    assert [t.title for t in YandexClient(playlist_http()).playlist(URL, limit=1).tracks] == ["Alpha song 1"]


def test_paging_that_stops_early_keeps_what_arrived_and_says_so():
    http = playlist_http()
    path = RU + "/users/me/playlists/3"
    http.routes[(path, None)] = {"result": {**PLAYLIST, "trackCount": 9}}
    logs = []
    assert len(YandexClient(http).playlist(URL, log=logs.append).tracks) == 3
    assert logs == ["yandex: paging stopped at 5 of 9 tracks"]


def test_playlist_that_never_loads_is_a_clear_error():
    with pytest.raises(YandexError):
        YandexClient(Http({})).playlist(URL)


def test_likes_as_feedback_history_and_tags():
    likes = YandexLikes(playlist_http(), URL)
    fb = likes.load()
    assert {(f.artist, f.title, f.score, f.source) for f in fb} == {
        ("Alpha", "Alpha song 1", 1.0, "yandex"), ("Kanye West", "Otis", 1.0, "yandex"),
        ("Alpha", "Alpha song 0", 1.0, "yandex")}
    h = likes.as_history().load()
    assert h.heard(Track("Otis", Artist("Kanye West"))) and h.plays(Artist("Alpha")) == 2
    table = genre_table()
    counts = Counter(table["rap"]["mb"] + table["indie"]["mb"])  # Alpha's two albums
    assert likes.tags(Artist("Alpha"), n=10) == sorted(counts, key=lambda g: (-counts[g], g))[:10]
    assert likes.tags(Artist("Jay-Z")) == sorted(table["foreignrap"]["mb"])[:4]  # featured artists count too
    assert likes.top_artists(1) == [("Alpha", "1")]


def test_similar_drops_soundtrack_catalogues_and_unknown_artists():
    http = playlist_http()
    sim = YandexSimilar(http, YandexLikes(http, URL))
    edges = sim.neighbours(Artist("ALPHA"), 10)
    assert [e.target.name for e in edges] == ["Beta", "Gamma"] and edges[0].weight > edges[1].weight
    assert sim.neighbours(Artist("Somebody Else"), 10) == []
    assert {"soundtrack", "films", "videogame", "animemusic"} <= non_artist_genres(genre_table())


def cfg_with(tmp_path, **sources):
    c = config_mod.Config()
    return dataclasses.replace(c, storage=dataclasses.replace(c.storage, save_dir=str(tmp_path / "s"),
                                                              http_cache=str(tmp_path / "h.sqlite"),
                                                              data_dir=str(tmp_path / "d")),
                               sources=dataclasses.replace(c.sources, **sources),
                               feedback=dataclasses.replace(c.feedback, sources=["yandex"]))


def test_recommender_wires_yandex_as_history_feedback_and_similar(tmp_path):
    with Recommender(cfg_with(tmp_path, yandex_playlist=URL, history="yandex"), token="") as rec:
        assert rec.feedback == [rec.yandex] and type(rec.history).__name__ == "_History"
        assert any(isinstance(s, YandexSimilar) for s in rec.radio.p.similar)
        assert GenreGraph.load({"yandex_parent": 0.3}, 0.2, yandex=genre_table()).related("phonk")


def test_config_rejects_yandex_history_without_a_playlist(tmp_path):
    with pytest.raises(ValueError, match="yandex_playlist"):
        config_mod.validate(cfg_with(tmp_path, history="yandex"))


def test_generate_drops_liked_tracks_and_boosts_liked_artists():
    likes = YandexLikes(playlist_http(), URL)
    radio = Radio(Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()],
                            history=likes.as_history(), feedback=[likes]), config_mod.Advanced(), log=lambda *_: None)
    res = radio.generate(Params(explore=0.5, n=20, per_artist=10, seed=1), artist="Seed")
    titles = {(t["artist"], t["title"]) for t in res["tracks"]}
    assert ("Alpha", "Alpha song 0") not in titles and ("Alpha", "Alpha song 1") not in titles
    assert any(a == "Alpha" for a, _ in titles)  # loved artist still in, just not the songs already liked
    radio.close()
