"""Offline tests: fake sources, real graph/ranking logic."""
from pvrecs.engine import Graph, Params, Radio, artist_pop_fit, hop_fit, pop_fit
from pvrecs.sources import Track


class FakeLB:
    # seed S -> A (close) -> B -> C (far); popularity falls with distance
    SIM = {"S": [("A", "Alpha", 1.0)], "A": [("B", "Beta", 1.0)], "B": [("C", "Gamma", 1.0)], "C": []}
    NAMES = {"S": "Seed", "A": "Alpha", "B": "Beta", "C": "Gamma"}
    USERS = {"S": 300000, "A": 100000, "B": 3000, "C": 200}

    def __init__(self, known=None, heard=None):
        self.known, self.heard = known or {}, heard or set()

    def similar_artists(self, mbid, limit=25):
        return self.SIM[mbid][:limit]

    def top_recordings(self, mbid):
        name = self.NAMES[mbid]
        return [{"recording_mbid": f"{mbid}{i}", "recording_name": f"{name} song {i}", "artist_name": name,
                 "total_user_count": self.USERS[mbid] // (i + 1)} for i in range(20)]

    def radio_artist(self, *a):
        return {}

    def radio_tags(self, *a):
        return []

    def recording_metadata(self, mbids):
        return {}

    def history(self):
        return self.known, self.heard


class FakeMB:
    def tags(self, mbid):
        return []

    def recording_titles(self, mbid):
        return set()

    def find_artist(self, name):
        return {"Seed": ("S", "Seed")}.get(name)

    def related_artists(self, mbid):
        return []

    def artist_name(self, mbid):
        return FakeLB.NAMES[mbid]


class FakeDZ:
    def find_artists(self, name):
        return []

    def find_artist(self, name):
        return None

    def related(self, *a, **k):
        return []

    def radio(self, *a):
        return []

    def top(self, *a, **k):
        return []


def radio(**kw):
    return Radio(FakeLB(**kw), FakeMB(), FakeDZ(), log=lambda *_: None)


def mean_hop(res):
    return sum(t["hop"] for t in res["tracks"]) / len(res["tracks"])


def test_params_scale_with_explore():
    lo, hi = Params(0.0), Params(1.0)
    assert lo.max_hops == 1 and hi.max_hops == 3
    assert lo.target_pop > hi.target_pop
    assert lo.troi_mode == "easy" and hi.troi_mode == "hard"


def test_fit_functions_peak_at_targets():
    p = Params(1.0)
    assert hop_fit(3, p) > hop_fit(1, p)
    assert pop_fit(0.15, p) > pop_fit(0.95, p)
    assert artist_pop_fit(0.2, p) > artist_pop_fit(0.95, p)
    assert artist_pop_fit(0.1, Params(0.0)) == 1.0  # obscurity never penalised


def test_graph_merges_sources_and_keeps_shortest_hop():
    g = Graph()
    g.add("X", 2, 0.5, "lb")
    n = g.add("x", 1, 0.4, "deezer", deezer_id=7, fans=1000)
    assert n.hop == 1 and n.deezer_id == 7 and n.via == {"lb", "deezer"}
    assert n.affinity > 0.5  # agreement bonus
    assert n.popularity is not None


def test_explore_moves_outward_and_deeper():
    near = radio().generate(Params(0.0, n=10, seed=1), artist="Seed")
    far = radio().generate(Params(1.0, n=10, seed=1), artist="Seed")
    assert mean_hop(far) > mean_hop(near)

    def avg(r):
        return sum(t["popularity"] for t in r["tracks"]) / len(r["tracks"])

    assert avg(far) < avg(near)


def test_per_artist_cap():
    res = radio().generate(Params(0.5, n=10, per_artist=2, seed=1), artist="Seed")
    counts = {}
    for t in res["tracks"]:
        counts[t["artist"]] = counts.get(t["artist"], 0) + 1
    assert max(counts.values()) <= 2


def test_heard_tracks_skipped_when_exploring():
    heard = {"alpha|alphasong0", "alpha|alphasong1"}
    res = radio(heard=heard).generate(Params(0.5, n=20, seed=1), artist="Seed")
    assert not {"Alpha song 0", "Alpha song 1"} & {t["title"] for t in res["tracks"]}


def test_known_artist_penalised_at_high_explore():
    p = Params(1.0)
    r = radio()
    g = Graph()
    n = g.add("Beta", 2, 1.0, "lb", mbid="B")
    assert r.artist_weight(n, p, {"B": 500}) < r.artist_weight(n, p, {})
    assert r.artist_weight(n, Params(0.0), {"B": 500}) == r.artist_weight(n, Params(0.0), {})


class CollidingDZ(FakeDZ):
    """Two Deezer artists named 'Alpha'; only id 2 has Alpha's songs."""

    def find_artists(self, name):
        return [(1, 9_000_000), (2, 5000)] if name == "Alpha" else []

    def top(self, did, limit=50):
        title = "Alpha song {}" if did == 2 else "Other song {}"
        return [Track("Alpha", title.format(i), deezer_id=did * 100 + i) for i in range(5)]


def test_deezer_name_collision_resolved_by_shared_titles():
    r = Radio(FakeLB(), FakeMB(), CollidingDZ(), log=lambda *_: None)
    n = Graph().add("Alpha", 1, 1.0, "lb", mbid="A")
    assert r._dz_match(n) == (2, 5000)
    n2 = Graph().add("Alpha", 1, 1.0, "deezer")  # no MBID: fall back to the most-followed match
    assert r._dz_match(n2) == (1, 9_000_000)


def test_popularity_slider_overrides_explore():
    assert Params(0.0, popularity=0.1).target_pop == 0.1
    assert Params(1.0, popularity=0.9).target_pop == 0.9
    assert Params(0.5).pop == Params(0.5, popularity=None).target_pop
    assert Params(0.0, popularity=0.0).target_artist_pop < Params(0.0).target_artist_pop


def test_popularity_slider_changes_picks_at_fixed_explore():
    def avg(r):
        return sum(t["popularity"] for t in r["tracks"]) / len(r["tracks"])

    hits = radio().generate(Params(0.5, n=10, seed=1, popularity=1.0), artist="Seed")
    cuts = radio().generate(Params(0.5, n=10, seed=1, popularity=0.0), artist="Seed")
    assert avg(cuts) < avg(hits)


def test_troi_window_tracks_popularity():
    r = radio()
    assert r._troi_window(Params(popularity=1.0)) == (0, 34)  # troi percentile 0 = most played
    assert r._troi_window(Params(popularity=0.0))[1] == 100
    assert r._troi_window(Params(0.0))[0] < r._troi_window(Params(1.0))[0]


def test_obscure_seed_keeps_ceiling_unless_slider_set():
    p = Params(0.5)
    assert artist_pop_fit(0.9, p, seed_pop=0.2) < artist_pop_fit(0.9, p, seed_pop=None)
    pinned = Params(0.5, popularity=1.0)
    assert artist_pop_fit(0.9, pinned, seed_pop=0.2) == artist_pop_fit(0.9, pinned)


class TagLB(FakeLB):
    SIM = {**FakeLB.SIM, "S": []}  # seed with no similarity edges at all

    def radio_tags(self, tags, lo, hi, n):
        return ["r1", "r2"] if tags == ["shoegaze"] else []

    def recording_metadata(self, mbids):
        return {m: {"artist": {"artists": [{"name": "Beta", "artist_mbid": "B"}]}} for m in mbids}


class TagMB(FakeMB):
    def tags(self, mbid):
        return ["shoegaze"] if mbid == "S" else []


def test_sparse_seed_falls_back_to_tag_neighbours():
    r = Radio(TagLB(), TagMB(), FakeDZ(), log=lambda *_: None)
    g = r.build_graph("Seed", "S", Params(0.3))
    assert "beta" in g.nodes and g.nodes["beta"].via == {"tag"} and g.nodes["beta"].parent == "Seed"
