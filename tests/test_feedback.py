"""Loves / hates / ratings: aggregation, adapters and their effect on ranking."""
from fakes import Resolver, Similar, Tracks
from test_providers import CannedHttp

from postradio.engine import Params, Providers, Radio
from postradio.models import Artist, Feedback, Taste, Track, rating_score
from postradio.providers import FeedbackProvider, TrackSimilarity
from postradio.providers.feedback import LBFeedback, NavidromeFeedback


class StaticFeedback(FeedbackProvider):
    def __init__(self, name, items):
        self.name, self.items = name, items

    def load(self):
        return self.items


class Broken(FeedbackProvider):
    name = "navidrome"

    def load(self):
        raise RuntimeError("down")


def radio(feedback=(), track_sim=None):
    prov = Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()], feedback=list(feedback),
                     track_similarity=track_sim)
    return Radio(prov, log=lambda *_: None)


def test_rating_scale_is_gradual_around_three():
    assert [rating_score(r) for r in (1, 2, 3, 4, 5)] == [-1.0, -0.5, 0.0, 0.5, 1.0]


def test_navidrome_wins_over_lb_per_track():
    t = Taste([Feedback("Beta", "Beta song 0", 1.0, "lb"), Feedback("Beta", "Beta song 0", -0.5, "navidrome")],
              priority=["navidrome", "lb"])
    assert t.track(Track("Beta song 0", Artist("Beta"))) == -0.5
    assert t.artist(Artist("Beta")) < 0


def test_artist_score_aggregates_and_saturates():
    t = Taste([Feedback("A", f"s{i}", 1.0, "lb") for i in range(20)])
    assert 0.99 < t.artist(Artist("a")) <= 1.0
    assert Taste([Feedback("A", None, 1.0, "navidrome")]).artist(Artist("A")) > \
        Taste([Feedback("A", "x", 1.0, "lb")]).artist(Artist("A"))  # explicit artist opinion counts double


def test_love_and_hate_move_artist_weight_by_factors():
    from postradio.engine import Graph
    n = Graph().add(Artist("Beta", mbid="B"), 2, 1.0, "lb")
    base = radio().artist_weight(n, Params(0.5))
    loved = radio([StaticFeedback("lb", [Feedback("Beta", None, 1.0, "lb", artist_mbid="B")])])
    hated = radio([StaticFeedback("lb", [Feedback("Beta", None, -1.0, "lb", artist_mbid="B")])])
    assert loved.artist_weight(n, Params(0.5)) > base > hated.artist_weight(n, Params(0.5))
    assert hated.artist_weight(n, Params(0.5)) / base < 0.4  # tanh(1) * ... -> close to hate_factor


def test_failing_feedback_source_is_skipped():
    r = radio([Broken(), StaticFeedback("lb", [Feedback("Beta", "x", 1.0, "lb")])])
    assert r.taste().artist(Artist("Beta")) > 0


def test_hated_artist_drops_out_of_results():
    p = Params(0.5, n=10, seed=1)
    before = {t["artist"] for t in radio().generate(p, artist="Seed")["tracks"]}
    hate = StaticFeedback("lb", [Feedback("Alpha", None, -1.0, "lb", artist_mbid="A")])
    after = radio([hate]).generate(p, artist="Seed")
    assert "Alpha" in before
    alpha = [t for t in after["tracks"] if t["artist"] == "Alpha"]
    assert all(t["taste"]["artist"] < 0 for t in alpha)
    assert len(alpha) <= len([t for t in before if t == "Alpha"]) + 2


class FakeSim(TrackSimilarity):
    """Every recording is similar to 'Beta song 3'."""

    def similar(self, track, limit):
        return [(Track("Beta song 3", Artist("Beta", mbid="B"), recording_mbid="b3"), 1.0)]


def test_loved_track_boosts_its_similar_tracks():
    love = StaticFeedback("lb", [Feedback("Gamma", "g", 1.0, "lb", recording_mbid="g1")])
    r = radio([love], FakeSim())
    r.generate(Params(0.5, n=5, seed=1), artist="Seed")
    assert r.last_run.track_boost["b3"] == 1.5


def test_track_seed_injects_similar_recordings():
    class TrackResolver(Resolver):
        def find_recording(self, a, title):
            return Track(title, Artist("Seed", mbid="S"), recording_mbid="s1")

    prov = Providers(resolver=TrackResolver(), similar=[Similar()], tracks=[Tracks()], track_similarity=FakeSim())
    res = Radio(prov, log=lambda *_: None).generate(Params(0.5, n=10, seed=1), track="Seed - x")
    beta3 = [t for t in res["tracks"] if t["title"] == "Beta song 3"]
    assert beta3 and "lb-similar-rec" not in beta3[0]["sources"] and "track-sim" in beta3[0]["sources"]
    assert beta3[0]["taste"]["similar_boost"] == 2.0


def test_lb_feedback_adapter():
    def page(params):
        if params["offset"]:
            return {"feedback": []}
        name = "Loved" if params["score"] == 1 else "Hated"
        return {"feedback": [{"recording_mbid": f"{name}-r", "track_metadata": {
            "artist_name": "X", "track_name": name, "mbid_mapping": {"artist_mbids": ["x"]}}}]}

    items = LBFeedback(CannedHttp({"/get-feedback": page}), "u").load()
    assert {(f.title, f.score, f.recording_mbid, f.artist_mbid) for f in items} == {
        ("Loved", 1.0, "Loved-r", "x"), ("Hated", -1.0, "Hated-r", "x")}


def test_navidrome_adapter_stars_and_ratings():
    ok = {"status": "ok"}
    http = CannedHttp({
        "/rest/getStarred2": {"subsonic-response": {**ok, "starred2": {
            "song": [{"id": "1", "title": "Creep", "artist": "Radiohead", "starred": "2026"}],
            "artist": [{"id": "a1", "name": "Pixies"}], "album": [{"id": "al", "artist": "Blur"}]}}},
        "/rest/search3": {"subsonic-response": {**ok, "searchResult3": {"song": [
            {"id": "1", "title": "Creep", "artist": "Radiohead", "starred": "2026", "userRating": 2},
            {"id": "2", "title": "Song 2", "artist": "Blur", "userRating": 3},
            {"id": "3", "title": "Fake", "artist": "Muse", "userRating": 1}]}}},
        "/rest/getArtists": {"subsonic-response": {**ok, "artists": {"index": [
            {"artist": [{"id": "a2", "name": "Muse", "userRating": 1}]}]}}},
    })
    items = {(f.artist, f.title): f.score for f in NavidromeFeedback(http, "http://nd", "u", "p").load()}
    assert items[("Radiohead", "Creep")] == 1.0  # star kept: stronger than the 2-star rating
    assert ("Blur", "Song 2") not in items  # 3 stars = neutral
    assert items[("Muse", "Fake")] == -1.0 and items[("Muse", None)] == -1.0
    assert items[("Pixies", None)] == 1.0 and items[("Blur", None)] == 0.5
