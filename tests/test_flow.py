"""Flow mode: the pure planner (ported tests) and Radio.flow end to end with fakes."""
import random

import pytest
from fakes import NAMES, Resolver, Tracks

from postradio.config import Advanced
from postradio.engine import Params, Providers, Radio
from postradio.errors import InvalidInput
from postradio.flow import (
    Pick,
    allocate,
    artist_weights,
    familiarity,
    fill,
    fit,
    flow_score,
    interp,
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


def test_taste_weight_keeps_the_walk_on_own_genres_and_0_follows_links():
    near = {"cloud rap", "neo soul"}  # spread hop 1: not the listener's own
    heavy = [set(plan_path(SPREAD, ADJ, stops=4, taste_weight=4, rng=random.Random(s))) & near for s in range(30)]
    light = [set(plan_path(SPREAD, ADJ, stops=4, taste_weight=0, rng=random.Random(s))) & near for s in range(30)]
    assert sum(map(bool, heavy)) < sum(map(bool, light))
    assert familiarity(["hip hop"], SPREAD) == 1.0 and familiarity(["cloud rap"], SPREAD) == 0.0


def test_path_steps_along_edges_and_a_seed_repeats_it():
    path = plan_path(SPREAD, ADJ, stops=4, rng=random.Random(1))
    for a, b in zip(path, path[1:], strict=False):
        assert b in ADJ[a] or not set(ADJ[a]) - set(path[:path.index(b)])  # an edge, or routed past a dead end
    assert plan_path(SPREAD, ADJ, stops=4, rng=random.Random(1)) == path
    assert len(path) == len(set(path)) == 4


def test_a_stuck_walk_routes_through_the_graph_and_says_so():
    # from neo soul the only link is jazz rap; after it the walk must go back through hip hop
    warnings: list[str] = []
    path = plan_path(SPREAD, ADJ, stops=6, rng=random.Random(3), start="neo soul", warnings=warnings)
    assert path[:2] == ["neo soul", "jazz rap"] and len(path) == len(set(path)) == 6
    for a, b in zip(path, path[1:], strict=False):
        assert b in ADJ[a] or any("routed" in w for w in warnings)
    island = {"x": {}, **ADJ}
    w2: list[str] = []
    assert plan_path(SPREAD, island, stops=3, start="x", warnings=w2) == ["x"] and "ended after 1" in w2[0]


def test_radius_keeps_every_stop_near_the_start():
    for s in range(10):
        path = plan_path(SPREAD, ADJ, stops=6, rng=random.Random(s), start="trap", radius=1)
        assert set(path) <= {"trap", "hip hop", "cloud rap"}  # trap and its direct neighbours only


def test_interp():
    assert interp((2, 5), 0) == 2 and interp((2, 5), 1) == 5 and interp((2.0, 0.7), 0.5) == 1.35


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


def test_explore_sets_the_plan_and_explicit_values_win():
    likes = [(Feedback("Seed", "Seed song 0", 1.0, "likes"), 100)]
    with radio(likes) as r:
        low = r.flow(Params(explore=0, n=6, seed=1), graph(), dry=True)["plan"]
        high = r.flow(Params(explore=1, n=6, seed=1), graph(), dry=True)["plan"]
        mine = r.flow(Params(explore=1, n=6, seed=1, stops=3, taste_weight=1.5), graph(), dry=True)
    assert (low["stops"], low["taste_weight"], low["new_artist_share"]) == (2, 2.0, 0)
    assert (high["stops"], high["taste_weight"], high["new_artist_share"]) == (5, 0.7, 1)
    assert mine["plan"]["stops"] == 3 and mine["plan"]["taste_weight"] == 1.5
    assert set(mine["plan"]["explicit"]) == {"stops", "taste_weight"} and len(mine["path"]) <= 3
    assert {"min_link_weight", "min_genre_weight", "radius", "half_life_days", "warnings"} <= set(low)


def test_flow_dry_previews_the_path_without_fetching_tracks():
    likes = [(Feedback("Seed", "Seed song 0", 1.0, "likes"), 100)]
    with radio(likes) as r:
        fetched = []
        r._top_tracks = lambda a: fetched.append(a) or []
        dry = r.flow(Params(explore=0.5, n=8, seed=7, stops=3), graph(), dry=True)
        assert dry["dry"] and dry["rng_seed"] == 7 and fetched == []
        assert dry["tracks_per_stop"] == [3, 3, 2] and dry["profile"][0]["genre"] == "hip hop"
        del r._top_tracks
        full = r.flow(Params(explore=0.5, n=8, seed=7, stops=3), graph())
        assert full["path"] == dry["path"]  # the preview is the path the real run takes


def test_radio_dry_builds_the_graph_but_picks_no_tracks():
    from fakes import Similar

    r = Radio(Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()]), Advanced(), log=lambda *_: None)
    fetched = []
    r._top_tracks = lambda a: fetched.append(a) or []
    res = r.generate(Params(explore=1, seed=5), dry=True, artist="Seed")
    assert res["dry"] and "tracks" not in res and fetched == []
    assert res["hops"][1]["top"][0]["artist"] == "Alpha" and res["hops"][1]["top"][0]["path"] == ["Seed", "Alpha"]
    assert res["max_hops"] == 3 and res["graph_size"] == 4
    r.close()


def test_rarity_and_parent_discount_shift_the_profile_to_specific_genres():
    from postradio.genres import ancestors_of
    weights = {"A": 1.0, "B": 1.0}
    mb = {"A": ["hip hop", "alternative hip hop"], "B": ["pop", "k-pop"]}
    plain = taste_profile(weights, [(mb, 1.0)])
    assert plain["hip hop"] > plain["alternative hip hop"]  # broad first tag wins without the fixes
    anc = ancestors_of([("alternative hip hop", "hip hop", "subgenre_of"), ("k-pop", "pop", "subgenre_of"),
                        ("hip hop", "x", "influenced_by")])
    assert anc["alternative hip hop"] == {"hip hop"} and not anc.get("hip hop")  # influence isn't parentage
    parent = taste_profile(weights, [(mb, 1.0)], ancestors=anc, parent_discount=0.3)
    assert parent["alternative hip hop"] > parent["hip hop"] and parent["k-pop"] > parent["pop"]
    rare = taste_profile(weights, [(mb, 1.0)], genre_weight={"hip hop": 0.2, "pop": 0.2})
    assert rare["alternative hip hop"] > rare["hip hop"]
    assert abs(sum(rare.values()) - 1) < 1e-3


def test_ancestors_are_transitive_and_survive_cycles():
    from postradio.genres import ancestors_of
    anc = ancestors_of([("c", "b", "subgenre_of"), ("b", "a", "subgenre_of"), ("a", "c", "subgenre_of")])
    assert anc["c"] >= {"a", "b"} and "c" not in anc["c"]


def test_cap_shares_spreads_the_excess_and_keeps_the_sum():
    from postradio.flow import cap_shares
    p = {"hip hop": 0.6, "pop": 0.2, "rock": 0.15, "jazz": 0.05}
    got = cap_shares(p, 0.4)  # hip hop's 0.2 excess goes to the rest in proportion: pop 0.3, rock 0.225, jazz 0.075
    assert got["hip hop"] == 0.4 and max(got.values()) <= 0.4 + 1e-4
    assert cap_shares(p, 0.3) == {"hip hop": 0.3, "pop": 0.3, "rock": 0.3, "jazz": 0.1}  # the excess can cap others
    assert abs(sum(got.values()) - 1) < 1e-3 and got["pop"] > got["rock"] > got["jazz"]  # order kept below the cap
    assert cap_shares(p, 1) == p and cap_shares(p, 0.1) == {g: 0.25 for g in p}  # too small: all equal
