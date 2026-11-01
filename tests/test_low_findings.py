"""Low-severity review findings: keys, error types, escaping, config validation, bounded memo."""
import pytest
from fakes import Resolver, Similar, Tracks
from fastapi.testclient import TestClient
from test_api import rec  # noqa: F401  (fixture)
from test_providers import CannedHttp

from postradio import config as config_mod
from postradio.engine import Params, Providers, Radio
from postradio.errors import InvalidInput, NotFound
from postradio.models import Artist, track_key
from postradio.providers.deezer import Deezer
from postradio.providers.musicbrainz import MusicBrainz
from postradio.server import create_app


def test_punctuation_only_names_get_distinct_keys():
    assert len({Artist("!!!").key, Artist("+/-").key, Artist("…").key}) == 3
    assert Artist("!!!", mbid="m").key == "mbid:m" and Artist("Radiohead").key == "radiohead"
    assert track_key("!!!", "Heart") != track_key("+/-", "Heart")


def test_errors_are_typed_and_mapped_to_http_codes(rec):  # noqa: F811
    r = Radio(Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()]), log=lambda *_: None)
    with pytest.raises(InvalidInput):
        r.generate(Params(), track="no separator")
    with pytest.raises(InvalidInput):
        r.generate(Params(), artist_mbid="not-a-uuid")
    with pytest.raises(NotFound):
        r.generate(Params(), artist="Nobody")
    client = TestClient(create_app(rec))
    assert client.post("/radio", json={"track": "no separator"}).status_code == 422
    assert client.post("/radio", json={"artist_mbid": "../../etc"}).status_code == 422
    assert client.post("/radio", json={"artist": "Nobody"}).status_code == 404
    assert client.post("/radio", json={"artist": "Seed", "mix": {"spotify": 1}}).status_code == 422


def test_musicbrainz_query_is_escaped_and_paths_are_encoded():
    seen = []

    class Spy(CannedHttp):
        def get(self, url, params=None, **k):
            seen.append((url, params))
            return None

    mb = MusicBrainz(Spy({}))
    mb.find_recording("AC/DC", 'He Said "Hi"')
    assert seen[-1][1]["query"] == 'recording:"He Said \\"Hi\\"" AND artist:"AC\\/DC"'
    from postradio.providers.lb_playlists import LBCreatedFor
    LBCreatedFor(Spy({})).playlists("a/../b")
    assert seen[-1][0].endswith("/user/a%2F..%2Fb/playlists/createdfor")


def test_config_rejects_wrong_types_ranges_and_streams(tmp_path):
    p = tmp_path / "c.yaml"
    for bad, msg in [("defaults: {n: 'ten'}", "'defaults.n' must be int"),
                     ("defaults: {n: 0}", "defaults.n must be at least 1"),
                     ("defaults: {explore: 2}", "explore must be in"),
                     ("advanced: {mix: {spotify: 0.5}}", "unknown stream 'spotify'"),
                     ("advanced: {mix: {troi: 'lots'}}", "'advanced.mix.troi' must be float"),
                     ("sources: {use_lb_token: 'yes'}", "'sources.use_lb_token' must be bool"),
                     ("sources: {history: maloja}", "sources.history must be")]:
        p.write_text(bad)
        with pytest.raises(ValueError, match=msg):
            config_mod.load(p)
    p.write_text("defaults: {n: 7, explore: 1}\nadvanced: {mix: {troi: 1}}")  # ints where floats expected: fine
    assert config_mod.load(p).defaults.n == 7


def test_api_rejects_n_zero_instead_of_silently_using_default(rec):  # noqa: F811
    with pytest.raises(InvalidInput):
        rec.generate(artist="Seed", n=0)
    assert len(rec.generate(artist="Seed", n=3).tracks) <= 3


def test_deezer_memo_is_bounded():
    http = CannedHttp({"/search/artist": lambda p: {"data": [{"id": 1, "name": p["q"], "nb_fan": 1}]}})
    dz = Deezer(http, memo_size=5)
    for i in range(20):
        dz.resolve(Artist(f"A{i}"))
    assert len(dz._resolved) == 5
