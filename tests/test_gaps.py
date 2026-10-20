"""Remaining review test gaps: offline mode and a genre seed through the public API."""
from fakes import artist
from test_api import rec  # noqa: F401  (fixture)

from postradio.engine import Providers, Radio
from postradio.http import Http
from postradio.providers import GenreSource


def test_offline_serves_cache_and_never_touches_the_network(tmp_path, monkeypatch):
    online = Http(cache_path=tmp_path / "c.sqlite")
    online._store(online._key("GET", "https://x.test/a", None, None), {"cached": True})
    off = Http(cache_path=tmp_path / "c.sqlite", offline=True)
    monkeypatch.setattr(off.session, "request", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network!")))
    assert off.get("https://x.test/a") == {"cached": True}
    assert off.get("https://x.test/missing") is None and off.calls == 0


class Genres(GenreSource):
    name = "genres"

    def genre_artists(self, genres, op, popularity, limit):
        return [artist("B"), artist("C")]


def test_genre_seed_end_to_end_through_recommender(rec):  # noqa: F811
    from fakes import Resolver, Similar, Tracks

    rec.radio = Radio(Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()], genres=[Genres()]),
                      rec.cfg.advanced, log=lambda *_: None)
    res = rec.generate(genres=["shoegaze", "dream pop"], genre_op="and", n=6)
    assert res["genres"] == ["shoegaze", "dream pop"] and res["genre_op"] == "and"
    assert res["tracks"] and {t["artist"] for t in res["tracks"]} <= {"Beta", "Gamma"}
    assert "genre-shoegaze-dream-pop" in res["default_name"]
    assert rec.save(res["result_id"]).exists()
