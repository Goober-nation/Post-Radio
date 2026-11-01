"""Radio from several seeds of one kind, radio without taste influence, and flow from a chosen start genre."""
import json

import pytest
from fakes import Resolver, Similar, Tracks, artist
from test_integration import LOVES, library, world  # noqa: F401 - pytest fixture

from postradio.api import Listener
from postradio.config import Advanced
from postradio.engine import Graph, Params, Providers, Radio
from postradio.errors import InvalidInput
from postradio.models import Like


def test_an_artist_reached_from_two_seeds_gets_the_blend_bonus():
    g = Graph(agreement_bonus=1.15, blend_bonus=1.3)
    one, two = g.add(artist("S"), 0, 1.0, "seed"), g.add(artist("C"), 0, 1.0, "seed")
    shared = g.add(artist("A"), 1, 0.5, "lb", one)
    only_one = g.add(artist("B"), 1, 0.5, "lb", one)
    g.add(artist("A"), 1, 0.5, "lb", two)
    assert shared.roots == {one.artist.key, two.artist.key}
    assert shared.affinity == pytest.approx(0.65) and only_one.affinity == 0.5
    g.add(artist("A"), 1, 0.5, "lb", two)  # the same seed again: no second bonus
    assert shared.affinity == pytest.approx(0.65)


def test_several_seeds_build_one_graph_around_all_of_them():
    sim = {"S": ["A"], "C": ["B"], "A": [], "B": []}
    providers = Providers(resolver=Resolver(), similar=[Similar(sim)], tracks=[Tracks()])
    r = Radio(providers, Advanced(), log=lambda *_: None)
    with r:
        res = r.generate(Params(explore=0.3, n=10, seed=1), artist=["Seed", "Gamma"])
    assert [s["artist"] for s in res["seeds"]] == ["Seed", "Gamma"]
    artists = {t["artist"] for t in res["tracks"]}
    assert {"Alpha", "Beta"} & artists and {"Seed", "Gamma"} & artists  # both seeds' neighbourhoods
    with pytest.raises(InvalidInput, match="1 to 5"):
        r.generate(Params(n=3, seed=1), artist=["Seed"] * 1 + [f"x{i}" for i in range(5)])


def test_seed_lists_through_the_library_and_http(world):  # noqa: F811
    from fastapi.testclient import TestClient

    from postradio.server import create_app

    with library(world) as rec:
        res = rec.generate(artist=["Seed", "Alpha"], n=6, rng_seed=3)
        assert [s.artist for s in res.seeds] == ["Seed", "Alpha"] and res.seed.artist == "Seed"
        assert res.to_dict()["seeds"][1]["artist"] == "Alpha"
        with TestClient(create_app(rec)) as client:
            got = client.post("/radio", json={"artist": ["Seed", "Alpha"], "n": 6, "rng_seed": 3}).json()
            assert [s["artist"] for s in got["seeds"]] == ["Seed", "Alpha"]
            mixed = client.post("/radio", json={"artist": ["Seed"], "genres": ["trap"]})
            assert mixed.status_code == 422  # one seed kind per radio
        with pytest.raises(InvalidInput):
            rec.generate(artist=["Seed"], track=["Seed - Seed song 1"])


def test_without_taste_opinions_dont_score_but_dislikes_still_never_play(world):  # noqa: F811
    hates = {"hates": {"artists": ["Alpha"]}, "loves": {"tracks": [{"artist": "Beta", "title": "Beta song 3"}]}}
    with library(world) as rec:
        rec.import_feedback(hates)
        on = rec.generate(artist="Seed", n=20, explore=1, rng_seed=5)
        off = rec.generate(artist="Seed", n=20, explore=1, rng_seed=5, taste=False)
    assert off.stats["taste"] is False and on.stats["taste"] is True
    assert "Alpha" not in {t.artist for t in off.tracks}  # disliked artist: dropped, not just scored down
    assert all(t.taste == {"artist": 0.0, "track": 0.0, "similar_boost": 1.0} for t in off.tracks)
    assert any(t.taste["artist"] != 0 or t.taste["track"] != 0 for t in on.tracks)


def test_flow_can_start_from_a_chosen_genre(world):  # noqa: F811
    li = Listener(likes=[Like(t["artist"], t["title"]) for t in LOVES["loves"]["tracks"]])
    with library(world) as rec:
        for e in (0, 0.5, 1):
            prev = rec.flow(explore=e, stops=3, start="Jazz Rap", listener=li, dry=True, rng_seed=2)
            assert prev.genre_path[0] == "jazz rap"
            full = rec.flow(explore=e, n=6, stops=3, start="jazz rap", listener=li, rng_seed=2)
            assert full.genre_path == prev.genre_path
        with pytest.raises(InvalidInput, match="did you mean: .*jazz rap"):
            rec.flow(start="jaz rap", listener=li, dry=True)
    assert json.dumps(prev.to_dict())
