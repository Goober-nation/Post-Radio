"""Flow mode: the pure planner (ported tests) and Radio.flow end to end with fakes."""
import random

import pytest
from fakes import NAMES, Resolver, Tracks

from postradio.config import Advanced
from postradio.engine import Params, Providers, Radio
from postradio.errors import InvalidInput
from postradio.flow import (
    Pick,
    PlannerConfig,
    allocate,
    artist_weights,
    destination,
    familiarity,
    fill,
    fit,
    flow_score,
    order,
    plan_path,
    route_between,
    taste_profile,
)
from postradio.genres import GenreGraph, GenreNode, GenreScore, from_pairs
from postradio.models import Artist, Feedback, Track
from postradio.providers import FeedbackProvider, GenreSource, Tagger

ADJ = {"hip hop": {"trap": .8, "jazz rap": .6, "pop rap": .7},
       "trap": {"hip hop": .8, "cloud rap": .7},
       "jazz rap": {"hip hop": .6, "neo soul": .5},
       "pop rap": {"hip hop": .7},
       "cloud rap": {"trap": .7},
       "neo soul": {"jazz rap": .5}}
SPREAD = [GenreScore("hip hop", 1.0, 0), GenreScore("trap", .5, 0), GenreScore("jazz rap", .4, 0),
          GenreScore("pop rap", .3, 0), GenreScore("cloud rap", .3, 1), GenreScore("neo soul", .2, 1)]


def c(artist, genres, familiar=True, score=1.0, title=None):
    return Pick(Track(title or f"{artist} song", Artist(artist)), genres, familiar, score)


def test_explore_0_tours_own_genres_and_1_leaves_them():
    for seed in range(20):
        home = plan_path(SPREAD, ADJ, explore=0, stops=4, rng=random.Random(seed))
        assert not {"cloud rap", "neo soul"} & set(home) and len(home) == len(set(home)) == 4
    away = [set(plan_path(SPREAD, ADJ, explore=1, stops=4, rng=random.Random(s))) & {"cloud rap", "neo soul"}
            for s in range(20)]
    assert sum(bool(w) for w in away) >= 15
    assert familiarity(["hip hop"], SPREAD) == 1.0 and familiarity(["cloud rap"], SPREAD) == 0.0


def test_path_steps_along_edges_and_a_seed_repeats_it():
    path = plan_path(SPREAD, ADJ, explore=.5, stops=4, rng=random.Random(1))
    for a, b in zip(path, path[1:], strict=False):
        assert b in ADJ[a] or not set(ADJ[a]) - set(path[:path.index(b)])  # an edge, or a jump at a dead end
    assert plan_path(SPREAD, ADJ, explore=.5, stops=4, rng=random.Random(1)) == path


def test_allocate():
    assert allocate(25, 4) == [7, 6, 6, 6] and sum(allocate(7, 3)) == 7 and allocate(3, 0) == []


def test_fill_mixes_new_by_explore_and_caps_artists():
    pool = [c("Old", ["trap"], True, 3, "o1"), c("Old", ["trap"], True, 3, "o2"), c("Old", ["trap"], True, 3, "o3"),
            c("New1", ["trap"], False, 2), c("New2", ["trap"], False, 1), c("Old2", ["trap"], True, 1)]
    (picks,) = fill(["trap"], {"trap": pool}, [4], explore=.5)
    assert sum(not p.familiar for p in picks) == 2 and [p.artist for p in picks].count("Old") == 2
    (one_each,) = fill(["trap"], {"trap": pool}, [4], explore=.5, per_artist=1)
    assert [p.artist for p in one_each].count("Old") == 1
    (all_new,) = fill(["trap"], {"trap": pool}, [4], explore=1)
    assert [p.artist for p in all_new][:2] == ["New1", "New2"] and len(all_new) == 4  # topped up with familiar
    assert {p.stop for p in all_new} == {"trap"}


def test_order_bridges_and_spreads_artists():
    stops = [[c("X", ["hip hop", "trap"], title="x1"), c("Y", ["hip hop"])],
             [c("Z", ["trap"]), c("W", ["trap", "hip hop"])]]
    assert [t.artist for t in order(stops, ["hip hop", "trap"])] == ["Y", "X", "W", "Z"]
    same = [[c("A", ["g"], title="1"), c("A", ["g"], title="2"), c("B", ["g"]), c("C", ["g"])]]
    assert [t.artist for t in order(same, ["g"], gap=2)] == ["A", "B", "A", "C"]
    # a clash at a stop's end is never fixed by pulling a track out of the next stop
    two = [[c("A", ["g"], title="1"), c("A", ["g"], title="2")], [c("B", ["h"])]]
    for s, path in zip(two, ["g", "h"], strict=True):
        for p in s:
            p.stop = path
    assert [t.stop for t in order(two, ["g", "h"], gap=2)] == ["g", "g", "h"]


def test_fill_hands_a_thin_stops_shortfall_to_the_others():
    pools = {"a": [c("X", ["a"], title=f"x{i}") for i in range(4)] + [c("Y", ["a"], title=f"y{i}") for i in range(4)],
             "b": [c("X", ["b"], title="xb")]}  # X is already used up by stop a
    out = fill(["a", "b"], pools, [2, 2], explore=0, per_artist=2)
    assert [len(s) for s in out] == [4, 0] and sum(len(s) for s in out) == 4


def test_flow_score():
    a, b, d = c("a", ["hip hop"]), c("b", ["hip hop", "trap"]), c("d", ["neo soul"])
    assert flow_score([a, b], ADJ) == 1.0 and flow_score([b, d], ADJ) == 0.0
    assert flow_score([c("e", ["jazz rap"]), d], ADJ) == .5


def test_taste_profile_blends_sources_with_recency():
    day = 86400
    weights = artist_weights([("A", 200 * day), ("B", 20 * day)], half_life_days=180)  # B liked 180 days earlier
    assert weights == {"A": 1.0, "B": 0.5}
    prof = taste_profile(weights, [({"A": ["trap", "hip hop"], "B": ["jazz rap"]}, .5),
                                   ({"A": ["hip hop"], "B": ["hip hop"]}, .5)])
    # MB: trap 1, hip hop .5, jazz rap .5 (half weight) -> /2 ; Yandex: hip hop 1.5 -> all of its half
    assert prof == {"hip hop": round(.5 * .5 / 2 + .5, 4), "trap": .25, "jazz rap": .125}


def test_fill_prefers_artists_whose_top_genres_match_and_dedupes_titles():
    pool = [c("Loose", ["hip hop", "pop rap", "trap"], score=9), c("Strict", ["trap"], score=1),
            c("Dup1", ["trap"], score=5, title="Forever (Explicit Version)"),
            c("Dup2", ["trap"], score=4, title="Forever feat. Someone")]
    (picks,) = fill(["trap"], {"trap": pool}, [3], explore=0)
    assert [p.artist for p in picks] == ["Dup1", "Strict", "Loose"]  # Dup2 is the same song; Loose only fills up


def test_explore_heads_for_a_destination_outside_the_start_family():
    roots = {g: ("hip hop",) for g in ("hip hop", "trap", "jazz rap", "pop rap", "cloud rap")} | {"neo soul": ("r&b",)}
    for seed in range(10):
        path = plan_path(SPREAD, ADJ, explore=1, stops=4, rng=random.Random(seed), roots=roots)
        assert "neo soul" in path and path[path.index("neo soul") - 1] == "jazz rap"  # along the graph
        assert "neo soul" not in plan_path(SPREAD, ADJ, explore=0, stops=4, rng=random.Random(seed), roots=roots)


def test_route_and_fit():
    assert route_between(ADJ, "pop rap", "neo soul") == ["pop rap", "hip hop", "jazz rap", "neo soul"]
    assert route_between(ADJ, "trap", "nowhere") is None
    assert fit(list("abcdefg"), 4) == ["a", "c", "e", "g"] and fit(list("ab"), 4) == ["a", "b"]
    assert fit(list("abc"), 1) == ["a"]


# ---------------------------------------------------------------- Radio.flow with fakes
GENRES = {"S": ["hip hop"], "A": ["hip hop", "trap"], "B": ["trap"], "C": ["jazz rap"]}


class Likes(FeedbackProvider):
    name = "likes"

    def __init__(self, liked):
        self.liked_ = liked

    def load(self):
        return [f for f, _ in self.liked_]

    def liked(self):
        return self.liked_


class MBTags(Tagger):
    def tags(self, artist, n=4):
        return GENRES.get(artist.mbid or "", [])[:n]


class GenreArtists(GenreSource):
    name = "mb"
    BY_GENRE = {"trap": ["B", "A"], "jazz rap": ["C"], "hip hop": ["A", "S"], "cloud rap": ["B"],
                "neo soul": ["C"], "pop rap": ["A"]}

    def genre_artists(self, genres, op, popularity, limit):
        return [Artist(NAMES[m], mbid=m) for g in genres for m in self.BY_GENRE.get(g, [])][:limit]


def graph():
    nodes = {g: GenreNode("Q", g, ("hip hop",), 1) for g in ADJ}
    return GenreGraph(nodes, from_pairs({(a, b): w for a, nb in ADJ.items() for b, w in nb.items() if a < b}), False)


def radio(likes):
    return Radio(Providers(resolver=Resolver(), similar=[], tracks=[Tracks()], feedback=[Likes(likes)],
                           genres=[GenreArtists()], profile_taggers={"mb": MBTags()}),
                 Advanced(), log=lambda *_: None)


def test_radio_flow_end_to_end_with_fakes():
    likes = [(Feedback("Seed", "Seed song 0", 1.0, "likes"), 100), (Feedback("Seed", "Seed song 1", 1.0, "likes"), 90)]
    with radio(likes) as r:
        res = r.flow(Params(explore=0.5, n=8, per_artist=2, seed=7, stops=3), graph())
        assert res["path"][0] == "hip hop" and len(res["path"]) == 3
        titles = [(t["artist"], t["title"]) for t in res["tracks"]]
        # only Seed, Alpha and Beta carry the path's genres; 2 tracks each is all there is
        assert len(titles) == len(set(titles)) == 6
        assert ("Seed", "Seed song 0") not in titles  # a liked track never comes back
        assert all(sum(a == x for x, _ in titles) <= 2 for a, _ in titles)
        assert {t["stop"] for t in res["tracks"]} <= set(res["path"])
        assert res["profile"][0] == {"genre": "hip hop", "share": 1.0}
        again = r.flow(Params(explore=0.5, n=8, per_artist=2, seed=7, stops=3), graph())
        assert again["tracks"] == res["tracks"]  # --rng-seed repeats a run


def test_radio_flow_without_likes_is_a_clear_error():
    with radio([]) as r, pytest.raises(InvalidInput, match="needs likes"):
        r.flow(Params(), graph())


def test_destination_distance_follows_explore_even_when_every_outside_genre_is_far():
    # a broad profile: the closest outside genre is only 0.2 of the top genre. Relative to it, "mid" is 0.7 and
    # "far" 0.4: exactly what explore 0.5 and 1 ask for (1 - 0.6 × explore)
    f = {"home": 1.0}
    r = {"home": 1.0, "near": 0.2, "mid": 0.14, "far": 0.08}
    roots = {"home": ("home",), "near": ("x",), "mid": ("y",), "far": ("z",)}
    cfg = PlannerConfig(destination_width=0.1)
    picks = {e: {destination("home", f, r, e, random.Random(s), roots, cfg) for s in range(20)} for e in (0.5, 1)}
    assert picks == {0.5: {"mid"}, 1: {"far"}}
