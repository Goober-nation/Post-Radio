"""Per-stream multipliers: each cuts only its own stream, keeping tracks nearest the popularity target."""
import pytest
from fakes import Resolver, Similar, Tracks
from test_api import rec  # noqa: F401  (fixture)

from pvrecs.engine import Graph, Params, Providers, Radio
from pvrecs.models import Artist, Track


def radio():
    return Radio(Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()]), log=lambda *_: None)


def entries(n, src, artists=1):
    """n candidates spread over `artists` artists, popularity 0..1."""
    nodes = [Graph().add(Artist(f"A{i}"), 1, 1.0, "lb") for i in range(artists)]
    return [(Track(f"t{i}", nodes[i % artists].artist, popularity=i / (n - 1), sources={src}), nodes[i % artists])
            for i in range(n)]


def test_cap_applies_to_the_whole_stream_nearest_target():
    r = radio()
    p = Params(popularity=0.0, mix={"deezer-top": 0.2})
    kept = r._cut(entries(100, "deezer-top", artists=10), "deezer-top", p)
    assert len(kept) == 20  # 20% of all 100 candidates, not 20 per artist
    assert max(t.popularity for t, _ in kept) < 0.2  # deep cuts, since -p 0
    hits = r._cut(entries(100, "deezer-top", artists=10), "deezer-top", Params(popularity=1.0, mix={"deezer-top": 0.2}))
    assert min(t.popularity for t, _ in hits) > 0.8


def test_ordered_stream_keeps_its_order():
    r = radio()
    es = entries(10, "track-sim")[::-1]  # most-similar-first order from LB; popularity irrelevant
    assert r._cut(es, "track-sim", Params(popularity=0.0, mix={"track-sim": 0.3})) == es[:3]


def test_multipliers_are_independent_and_unknown_streams_warn():
    logs = []
    r = Radio(Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()]), log=logs.append)
    p = Params(mix={"deezer-top": 0.0, "troi": 1.0, "lb": 1.0})
    assert r._cut(entries(10, "deezer-top"), "deezer-top", p) == []
    assert len(r._cut(entries(10, "troi"), "troi", p)) == 10
    assert len(r._cut(entries(10, "mystery"), "mystery", p)) == 10
    r._cut(entries(10, "mystery"), "mystery", p)
    assert sum("mystery" in m for m in logs) == 1  # warned once


def test_mix_parse_errors_name_the_flag():
    from pvrecs.cli import _parse_mix

    with pytest.raises(SystemExit, match="--mix troi: 'x' is not a number"):
        _parse_mix("troi=x")
    with pytest.raises(SystemExit, match="--mix expects STREAM=X"):
        _parse_mix("troi")


def test_zero_mix_removes_stream_from_results():
    res = radio().generate(Params(0.5, n=10, seed=1, mix={"lb": 0.0, "deezer-top": 1.0}), artist="Seed")
    assert res["tracks"] == [] and res["mix"] == {"lb": 0.0}  # the fake catalogue is all 'lb'


def test_api_mix_overrides_and_validation(rec):  # noqa: F811
    assert rec._mix({"deezer_top": 0.2})["deezer-top"] == 0.2 and rec._mix(None)["troi"] == 1.0
    assert set(rec._mix(None)) == {"deezer-top", "lb", "deezer-radio", "troi", "track-sim", "lb-tags"}
    with pytest.raises(ValueError, match="unknown mix stream"):
        rec._mix({"spotify": 0.5})
    with pytest.raises(ValueError, match="must be in"):
        rec._mix({"troi": 2})


def test_capped_track_sim_leaves_no_boost():
    from test_feedback import FakeSim

    class TrackResolver(Resolver):
        def find_recording(self, a, title):
            return Track(title, Artist("Seed", mbid="S"), recording_mbid="s1")

    def run(cap):
        r = Radio(Providers(resolver=TrackResolver(), similar=[Similar()], tracks=[Tracks()],
                            track_similarity=FakeSim()), log=lambda *_: None)
        r.generate(Params(0.5, n=5, seed=1, mix={"track-sim": cap, "lb": 1.0}), track="Seed - x")
        return r.track_boost

    assert run(1.0).get("b3") == 2.0
    assert "b3" not in run(0.0)  # off means off: no boost left behind for other streams to pick up
