"""Engine tests: fake providers, real graph/ranking logic."""
from fakes import Resolver, Similar, StaticHistory, TagRad, Tags, Tracks

from pvrecs.engine import Graph, Params, Providers, Radio, artist_pop_fit, hop_fit, pop_fit
from pvrecs.models import Artist, History, Play


def radio(history=None, similar=None, **kw):
    prov = Providers(resolver=Resolver(), similar=[similar or Similar()], tracks=[Tracks()],
                     history=StaticHistory(history or History()), **kw)
    return Radio(prov, log=lambda *_: None)


def mean(res, key):
    return sum(t[key] for t in res["tracks"]) / len(res["tracks"])


def test_params_scale_with_explore():
    lo, hi = Params(0.0), Params(1.0)
    assert lo.max_hops == 1 and hi.max_hops == 3
    assert lo.pop > hi.pop
    assert lo.radio_mode == "easy" and hi.radio_mode == "hard"


def test_fit_functions_peak_at_targets():
    p = Params(1.0)
    assert hop_fit(3, p) > hop_fit(1, p)
    assert pop_fit(0.15, p) > pop_fit(0.95, p)
    assert artist_pop_fit(0.2, p) > artist_pop_fit(0.95, p)
    assert artist_pop_fit(0.1, Params(0.0)) == 1.0  # obscurity never penalised


def test_graph_merges_sources_and_keeps_shortest_hop():
    g = Graph()
    g.add(Artist("X", mbid="x"), 2, 0.5, "lb")
    n = g.add(Artist("x", deezer_id=7, popularity=0.3), 1, 0.4, "deezer")
    assert n.hop == 1 and n.artist.deezer_id == 7 and n.artist.mbid == "x" and n.via == {"lb", "deezer"}
    assert n.affinity > 0.5 and n.artist.popularity == 0.3


def test_explore_moves_outward_and_deeper():
    near = radio().generate(Params(0.0, n=10, seed=1), artist="Seed")
    far = radio().generate(Params(1.0, n=10, seed=1), artist="Seed")
    assert mean(far, "hop") > mean(near, "hop")
    assert mean(far, "popularity") < mean(near, "popularity")


def test_popularity_slider_overrides_explore():
    assert Params(0.0, popularity=0.1).pop == 0.1
    assert Params(1.0, popularity=0.9).pop == 0.9
    hits = radio().generate(Params(0.5, n=10, seed=1, popularity=1.0), artist="Seed")
    cuts = radio().generate(Params(0.5, n=10, seed=1, popularity=0.0), artist="Seed")
    assert mean(cuts, "popularity") < mean(hits, "popularity")


def test_obscure_seed_keeps_ceiling_unless_slider_set():
    p = Params(0.5)
    assert artist_pop_fit(0.9, p, seed_pop=0.2) < artist_pop_fit(0.9, p, seed_pop=None)
    pinned = Params(0.5, popularity=1.0)
    assert artist_pop_fit(0.9, pinned, seed_pop=0.2) == artist_pop_fit(0.9, pinned)


def test_per_artist_cap():
    res = radio().generate(Params(0.5, n=10, per_artist=2, seed=1), artist="Seed")
    counts = {}
    for t in res["tracks"]:
        counts[t["artist"]] = counts.get(t["artist"], 0) + 1
    assert max(counts.values()) <= 2


def test_heard_tracks_skipped_when_exploring():
    h = History.from_plays([Play("Alpha", "Alpha song 0"), Play("Alpha", "Alpha song 1")])
    res = radio(history=h).generate(Params(0.5, n=20, seed=1), artist="Seed")
    assert not {"Alpha song 0", "Alpha song 1"} & {t["title"] for t in res["tracks"]}


def test_known_artist_penalised_only_when_exploring():
    h = History.from_counts([("Beta", "B", 500)], [])
    n = Graph().add(Artist("Beta", mbid="B"), 2, 1.0, "lb")
    known, fresh = radio(history=h), radio()
    assert known.artist_weight(n, Params(1.0)) < fresh.artist_weight(n, Params(1.0))
    assert known.artist_weight(n, Params(0.0)) == fresh.artist_weight(n, Params(0.0))


def test_runs_without_history():
    res = radio().generate(Params(0.5, n=5, seed=1), artist="Seed")
    assert res["tracks"] and not any(t["known_artist"] for t in res["tracks"])


def test_sparse_seed_falls_back_to_tag_neighbours():
    r = radio(similar=Similar({"S": []}), tagger=Tags(), tag_radio=TagRad())
    g = r.build_graph(Artist("Seed", mbid="S"), Params(0.3))
    assert "beta" in g.nodes and g.nodes["beta"].via == {"tag"} and g.path(g.nodes["beta"]) == ["Seed", "Beta"]
