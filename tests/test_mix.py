"""Per-stream multipliers: each cuts only its own stream, keeping tracks nearest the popularity target."""
import pytest
from fakes import Resolver, Similar, Tracks
from test_api import rec  # noqa: F401  (fixture)

from pvrecs.engine import Params, Providers, Radio
from pvrecs.models import Artist, Track


def radio():
    return Radio(Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()]), log=lambda *_: None)


def tracks(n, src):
    return [Track(f"t{i}", Artist("A"), popularity=i / (n - 1), sources={src}) for i in range(n)]


def test_cut_keeps_closest_to_target_and_uses_nominal_size():
    r = radio()
    p = Params(popularity=0.0, mix={"deezer-top": 0.2})
    kept = r._cut(tracks(100, "deezer-top"), "deezer-top", p, full=100)
    assert len(kept) == 20 and max(t.popularity for t in kept) < 0.2  # deep cuts, not the top 20
    p_hits = Params(popularity=1.0, mix={"deezer-top": 0.2})
    assert min(t.popularity for t in r._cut(tracks(100, "deezer-top"), "deezer-top", p_hits, 100)) > 0.8
    assert len(r._cut(tracks(30, "deezer-top"), "deezer-top", p, full=100)) == 20  # cap is of the nominal 100


def test_multipliers_are_independent():
    r = radio()
    p = Params(mix={"deezer-top": 0.0, "troi": 1.0})
    assert r._cut(tracks(10, "deezer-top"), "deezer-top", p) == []
    assert len(r._cut(tracks(10, "troi"), "troi", p)) == 10
    assert len(r._cut(tracks(10, "lb"), "lb", p)) == 10  # unmentioned = 1


def test_zero_mix_removes_stream_from_results():
    res = radio().generate(Params(0.5, n=10, seed=1, mix={"lb": 0.0}), artist="Seed")
    assert res["tracks"] == [] and res["mix"] == {"lb": 0.0}  # the fake catalogue is all 'lb'


def test_api_mix_overrides_and_validation(rec):  # noqa: F811
    assert rec._mix({"deezer_top": 0.2})["deezer-top"] == 0.2 and rec._mix(None)["troi"] == 1.0
    assert set(rec._mix(None)) == {"deezer-top", "lb", "deezer-radio", "troi", "track-sim", "lb-tags"}
    with pytest.raises(ValueError, match="unknown mix stream"):
        rec._mix({"spotify": 0.5})
    with pytest.raises(ValueError, match="must be in"):
        rec._mix({"troi": 2})
