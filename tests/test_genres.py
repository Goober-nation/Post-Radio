"""Genre layer: structural graph, learned weights, spreading a profile over the graph."""
import json
import math

import pytest

from postradio import genres
from postradio.config import Advanced
from postradio.genres import (
    GenreGraph,
    GenreScore,
    combine,
    cotag,
    from_pairs,
    neighbours,
    popularity,
    similar,
    spread,
    yandex_edges,
)

KINDS = Advanced().genre_kind_weights
EDGES = [("drill", "hip hop", "subgenre_of"), ("uk drill", "drill", "subgenre_of"),
         ("grime", "hip hop", "subgenre_of"), ("grime", "uk drill", "influenced_by"),
         ("trap", "hip hop", "subgenre_of")]


def by_genre(scores: list[GenreScore]) -> dict[str, GenreScore]:
    return {s.genre: s for s in scores}


def test_spread_decays_per_hop_and_keeps_own_genres_first():
    out = by_genre(spread({"drill": 1.0}, neighbours(EDGES, KINDS)))
    assert out["drill"] == GenreScore("drill", 1.0, 0)
    assert out["hip hop"].score == 0.5 and out["uk drill"].hop == 1
    assert out["trap"] == GenreScore("trap", 0.25, 2)  # drill -> hip hop -> trap
    assert "nowhere" not in out


def test_best_path_wins_not_the_sum():
    # grime is reachable from drill via hip hop (1 × .5 × 1 × .5) and via uk drill (1 × .5 × .5 × .5)
    assert by_genre(spread({"drill": 1.0}, neighbours(EDGES, KINDS)))["grime"].score == 0.25


def test_own_share_is_not_overwritten_by_a_weaker_path():
    out = by_genre(spread({"drill": 0.6, "hip hop": 0.4}, neighbours(EDGES, KINDS)))
    assert out["hip hop"] == GenreScore("hip hop", 0.4, 0)


def test_hops_limit():
    assert {s.genre for s in spread({"uk drill": 1.0}, neighbours(EDGES, KINDS), hops=1)} == {
        "uk drill", "drill", "grime"}


def test_duplicate_link_keeps_strongest_kind_and_unknown_kinds_are_skipped():
    adj = neighbours([("a", "b", "yandex_parent"), ("a", "b", "subgenre_of"), ("a", "c", "made_up")], KINDS)
    assert adj["a"]["b"] == adj["b"]["a"] == 1.0 and "c" not in adj["a"]


def test_yandex_edges_link_mb_genres_of_child_and_parent():
    table = {"rap": {"parent": None, "mb": ["hip hop"]},
             "rusrap": {"parent": "rap", "mb": ["hip hop"]},  # same MB genre: no self-loop
             "phonkgenre": {"parent": "dance", "mb": ["phonk"]},
             "dance": {"parent": None, "mb": ["dance", "electronic"]}}
    assert yandex_edges(table) == {("phonk", "dance", "yandex_parent"), ("phonk", "electronic", "yandex_parent")}


def test_cotag_npmi():
    tags = {"a1": {"drill", "hip hop"}, "a2": {"drill", "hip hop"}, "a3": {"hip hop"}, "a4": {"jazz"},
            "a5": {"jazz", "hip hop"}}
    w = cotag(tags)
    p = 2 / 5
    assert w[("drill", "hip hop")] == round(math.log(p / (2 / 5 * 4 / 5)) / -math.log(p), 4)
    assert ("hip hop", "jazz") not in w  # shared by only one artist
    assert popularity(tags) == {"drill": 2, "hip hop": 4, "jazz": 2}


def test_cotag_drops_pairs_rarer_than_chance():
    tags = {f"x{i}": {"pop"} for i in range(8)} | {f"y{i}": {"rock"} for i in range(8)}
    tags |= {"z1": {"pop", "rock"}, "z2": {"pop", "rock"}}
    assert cotag(tags) == {}  # 2/18 together vs (10/18)^2 by chance: negative PMI


def test_similar_projection_normalises_by_tag_count_and_genre_mass():
    tags = {"a": {"drill"}, "b": {"grime"}, "c": {"drill", "trap"}, "d": {"grime"}}
    w = similar([("a", "b", 1.0), ("a", "d", 1.0), ("c", "b", 1.0), ("c", "d", 0.5)], tags)
    # drill-grime: 1 + 1 + .5 + .25 = 2.75; trap-grime: .5 + .25 = .75 (c has two tags: half each)
    dg, tg = 2.75 / math.sqrt(2.75 * 3.5), 0.75 / math.sqrt(0.75 * 3.5)
    assert w[("drill", "grime")] == 1.0 and w[("grime", "trap")] == round(tg / dg, 4)


def test_similar_skips_untagged_and_single_pairs():
    assert similar([("a", "b", 1.0), ("a", "x", 1.0)], {"a": {"pop"}, "b": {"rock"}}) == {}


def test_combine_data_ranks_fallback_fills_only_uncovered_genres():
    data = [{("drill", "hip hop"): 0.4}, {("drill", "hip hop"): 0.7, ("grime", "hip hop"): 0.2}]
    fb = {("drill", "hip hop"): 1.0, ("drill", "grime"): 1.0, ("hip hop", "rare"): 1.0}
    out = combine(data, fb, fallback_scale=0.2)
    assert out[("drill", "hip hop")] == 0.7  # strongest data layer, not the structural 1.0
    assert ("drill", "grime") not in out  # both covered by data, data says nothing: no edge
    assert out[("hip hop", "rare")] == 0.2  # rare has no data: fallback, scaled down


def test_spread_follows_learned_weights():
    adj = from_pairs({("drill", "hip hop"): 0.2, ("drill", "uk drill"): 0.9})
    out = {s.genre: s.score for s in spread({"drill": 1.0}, adj)}
    assert out["uk drill"] == pytest.approx(0.45) and out["hip hop"] == pytest.approx(0.1)


def write_graph(path):
    path.write_text(json.dumps({"nodes": {g: {"qid": "Q", "label": g, "roots": ["hip hop"], "depth": 1}
                                          for g in ("hip hop", "drill", "trap", "rare")},
                                "edges": [{"src": s, "dst": d, "kind": k} for s, d, k in
                                          [("drill", "hip hop", "subgenre_of"), ("trap", "hip hop", "subgenre_of"),
                                           ("rare", "hip hop", "influenced_by")]]}))


def test_graph_load_structural_then_learned_with_fallback(tmp_path):
    g_path, data = tmp_path / "graph.json", tmp_path / "data"
    write_graph(g_path)
    data.mkdir()
    g = GenreGraph.load(KINDS, 0.2, data_dir=data, graph_path=g_path)
    assert not g.learned and g.related("drill") == {"hip hop": 1.0} and g.related("rare") == {"hip hop": 0.5}
    (data / genres.WEIGHTS_FILE).write_text(json.dumps({"cotag": {"drill|hip hop": 0.6},
                                                        "similar": {"drill|trap": 0.3}}))
    g = GenreGraph.load(KINDS, 0.2, data_dir=data, graph_path=g_path)  # new file version: reloaded
    assert g.learned
    assert g.related("drill") == {"hip hop": 0.6, "trap": 0.3}
    assert g.related("rare") == {"hip hop": 0.1}  # uncovered genre: structural 0.5 × 0.2
    assert g.nodes["drill"].roots == ("hip hop",)


def test_graph_load_with_yandex_overlay(tmp_path):
    g_path = tmp_path / "graph.json"
    write_graph(g_path)
    table = {"rap": {"parent": None, "mb": ["hip hop"]}, "trapy": {"parent": "rap", "mb": ["trap", "unknown"]}}
    g = GenreGraph.load({"yandex_parent": 0.3}, 0.2, graph_path=g_path, yandex=table)
    assert g.related("trap") == {"hip hop": 0.3} and "unknown" not in g.adj  # off-graph genres dropped


def test_packaged_graph_loads():
    g = GenreGraph.load(KINDS, 0.2)
    assert len(g.nodes) > 2000 and "hip hop" in g.nodes and g.related("trap")
    assert g.spread({"shoegaze": 1.0})[0] == GenreScore("shoegaze", 1.0, 0)
