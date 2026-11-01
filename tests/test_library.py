"""Library use: folders, no implicit environment, typed errors, secrets never written down, typed results."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fakes import Resolver, Similar, Tracks
from test_flow import GenreArtists, MBTags, graph, radio

from postradio import config as config_mod
from postradio import errors
from postradio.api import Listener, Recommender
from postradio.engine import Providers, Radio
from postradio.models import Artist, Feedback, Like, Play
from postradio.providers import NoHistory
from postradio.providers.listenbrainz import LBTopTracks
from postradio.results import FlowPreview, GraphPreview, Playlist, PlaylistTrack, Profile


@pytest.fixture
def elsewhere(tmp_path, monkeypatch):
    """Run from an unrelated working directory, with a polluted environment and $HOME, to prove neither is
    used."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("LISTENBRAINZ_TOKEN", "env-token-must-not-be-read")
    monkeypatch.setenv("LISTENBRAINZ_USERNAME", "env-user")
    monkeypatch.setenv("POSTRADIO_HOME", str(tmp_path / "env-home"))
    (cwd / "config.yaml").write_text("defaults: {n: 3}\n")  # a stray config the library must ignore
    return tmp_path


def files_under(p: Path) -> set[str]:
    return {str(f.relative_to(p)) for f in p.rglob("*") if f.is_file()} if p.exists() else set()


def test_home_holds_everything_and_nothing_is_read_implicitly(elsewhere):
    home = elsewhere / "pv" / "postradio"
    with Recommender(home=home) as rec:
        assert rec.dirs == config_mod.Dirs(home / "cache", home / "data", home / "state")
        assert rec.save_dir == home / "state" / "playlists" and rec.data_dir == home / "data"
        assert rec.token is None and rec.lb_user is None  # the environment is not read by the library
        assert rec.cfg.defaults.n == 50  # ./config.yaml in the working directory is not read either
    assert files_under(elsewhere / "cwd") == {"config.yaml"}  # nothing written to the working directory
    assert not (elsewhere / "env-home").exists()  # $POSTRADIO_HOME is a CLI convention only
    assert files_under(home) == {"cache/http.sqlite"}


def test_explicit_folders_and_env_are_opt_in(elsewhere):
    store = elsewhere / "shared-store"
    with Recommender(home=elsewhere / "h", data_dir=store, env=os.environ) as rec:
        assert rec.data_dir == store and rec.dirs.cache == elsewhere / "h" / "cache"
        assert rec.token == "env-token-must-not-be-read" and rec.lb_user == "env-user"  # because env= was passed
    with Recommender(home=elsewhere / "h", env=os.environ, token="") as rec:
        assert rec.token is None  # "" = explicitly tokenless, even with the env passed


def test_without_home_the_users_own_folders_are_used(elsewhere):
    with Recommender() as rec:
        user = elsewhere / "userhome"
        assert rec.dirs.cache == user / ".cache" / "postradio"
        assert rec.data_dir == user / ".local" / "share" / "postradio" / "data"
        assert rec.save_dir == user / ".local" / "share" / "postradio" / "playlists"  # same as before 4.5


def test_config_paths_resolve_against_the_config_files_folder(elsewhere):
    cfg_dir = elsewhere / "conf"
    cfg_dir.mkdir()
    (cfg_dir / "config.yaml").write_text("storage: {data_dir: store, http_cache: c/http.sqlite}\n")
    cfg = config_mod.load(cfg_dir / "config.yaml")
    with Recommender(cfg, home=elsewhere / "h") as rec:
        assert rec.data_dir == cfg_dir / "store" and rec.http.db.execute("PRAGMA database_list").fetchone()[2] \
            == str(cfg_dir / "c" / "http.sqlite")
    with Recommender(cfg, home=elsewhere / "h", data_dir=elsewhere / "explicit") as rec:
        assert rec.data_dir == elsewhere / "explicit"  # an explicit argument beats the config


def test_find_config_precedence(elsewhere):
    env = {"POSTRADIO_HOME": str(elsewhere / "envhome")}
    assert config_mod.find_config({}, "x.yaml") == Path("x.yaml")
    assert config_mod.find_config({"POSTRADIO_CONFIG": "/c.yaml"}) == Path("/c.yaml")
    assert config_mod.find_config(env) == elsewhere / "cwd" / "config.yaml"  # home has no config.yaml yet
    (elsewhere / "envhome").mkdir()
    (elsewhere / "envhome" / "config.yaml").write_text("{}")
    assert config_mod.find_config(env) == elsewhere / "envhome" / "config.yaml"
    os.remove(elsewhere / "cwd" / "config.yaml")
    assert config_mod.find_config({}) is None
    with pytest.raises(errors.ConfigError):
        config_mod.load(elsewhere / "missing.yaml")


def test_every_error_is_a_postradio_error_and_old_catches_still_work():
    for cls in (errors.InvalidInput, errors.NotFound, errors.ConfigError, errors.DataError,
                errors.SourceUnavailable, errors.SourceAuthError):
        assert issubclass(cls, errors.PostRadioError)
    assert issubclass(errors.InvalidInput, ValueError) and issubclass(errors.SourceAuthError, errors.SourceError)


def test_a_token_is_sent_but_never_written_to_the_cache_or_the_log(elsewhere, monkeypatch):
    token, logs = "secret-token-1234", []
    with Recommender(home=elsewhere / "h", token=token, log=logs.append) as rec:
        sent = []

        class Resp:
            status_code, headers = 200, {}

            def json(self):
                return [{"recording_mbid": "r1", "recording_name": "Song", "total_user_count": 10}]

        def request(method, url, headers=None, **_):
            sent.append(headers or {})
            return Resp()

        monkeypatch.setattr(rec.http.session, "request", request)
        tracks = LBTopTracks(rec.http, rec.token).top_tracks(Artist("A", mbid="a"), 10)
        assert tracks and sent[0]["Authorization"] == f"Token {token}"
        rec.http.db.commit()
    cache = (elsewhere / "h" / "cache" / "http.sqlite").read_bytes()
    assert token.encode() not in cache and not any(token in m for m in logs)


# ---------------------------------------------------------------- typed results


@pytest.fixture
def lib(tmp_path):
    rec = Recommender(home=tmp_path / "home")
    rec.radio = Radio(Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()]), rec.cfg.advanced,
                      log=lambda *_: None)
    yield rec
    rec.close()


def test_radio_and_flow_return_one_typed_shape(lib):
    r = lib.generate(artist="Seed", n=5, rng_seed=1)
    assert isinstance(r, Playlist) and r.kind == "radio" and r.rng_seed == 1 and r.seed.artist == "Seed"
    assert all(isinstance(t, PlaylistTrack) and t.artist_mbid and t.hop is not None and t.stop is None
               for t in r.tracks)
    lib.radio = radio([(Feedback("Seed", "Seed song 0", 1.0, "likes"), 100)])
    lib.genre_graph = graph
    f = lib.flow(explore=0.5, n=6, stops=3, rng_seed=2)
    assert isinstance(f, Playlist) and f.kind == "flow" and f.genre_path and f.seed is None
    assert all(t.stop in f.genre_path and t.hop is None and t.artist_mbid for t in f.tracks)
    assert isinstance(lib.flow(n=6, stops=3, dry=True), FlowPreview) and isinstance(lib.profile(), Profile)


def test_to_dict_is_plain_json_and_reads_back(lib):
    r = lib.generate(artist="Seed", n=5, rng_seed=1)
    d = json.loads(json.dumps(r.to_dict()))
    assert Playlist.from_dict(d) == r and d["schema"] == 1
    assert set(d["tracks"][0]) == {f for f in PlaylistTrack.__dataclass_fields__}
    assert Playlist.from_dict({**d, "added_later": 1}) == r  # unknown keys from newer versions are ignored


def test_dry_radio_is_a_graph_preview(lib):
    g = lib.generate(artist="Seed", explore=1, dry=True, rng_seed=3)
    assert isinstance(g, GraphPreview) and g.rng_seed == 3 and g.hops[1]["top"][0]["artist"] == "Alpha"
    assert json.loads(json.dumps(g.to_dict()))["dry"] is True


def test_http_sends_the_same_shape_as_to_dict(lib):
    from fastapi.testclient import TestClient

    from postradio.server import create_app

    with TestClient(create_app(lib)) as client:
        body = client.post("/radio", json={"artist": "Seed", "n": 5, "rng_seed": 1}).json()
        assert set(body) == set(Playlist.__dataclass_fields__) and body["result_id"]
        assert Playlist.from_dict(body).tracks == lib.generate(artist="Seed", n=5, rng_seed=1).tracks
        assert client.post("/radio", json={"artist": "Seed", "dry": True}).json()["dry"] is True


# ---------------------------------------------------------------- per-call listeners


@pytest.fixture
def shared(tmp_path):
    """One long-lived Recommender whose sources are fakes; per-call ones get the listener's taste."""
    rec = Recommender(home=tmp_path / "home")
    tokens = []

    def providers(token=None, history=None, feedback=None, yandex=None):
        tokens.append(token)
        return Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()],
                         feedback=feedback or [], history=history or NoHistory(),
                         profile_taggers={"mb": MBTags()}, genres=[GenreArtists()])

    rec._providers = providers
    rec.radio = Radio(providers(), rec.cfg.advanced, log=lambda *_: None)
    rec.genre_graph = graph
    rec.tokens = tokens
    yield rec
    rec.close()


def test_listeners_on_one_recommender_get_their_own_taste_concurrently(shared):
    hip = Listener(likes=[Like("Seed", "Seed song 0", at=100)], lb_token="tok-a")
    jazz = Listener(likes=[Like("Gamma", "Gamma song 0", at=100)], lb_token="tok-b")
    with ThreadPoolExecutor(4) as ex:
        runs = list(ex.map(lambda li: shared.profile(li).profile[0]["genre"], [hip, jazz] * 4))
    assert runs == ["hip hop", "jazz rap"] * 4
    assert {"tok-a", "tok-b"} <= set(shared.tokens)  # each call's own token reached its sources
    with pytest.raises(errors.InvalidInput, match="needs likes"):
        shared.flow(n=4)  # the shared, configured taste is untouched (and empty here)


def test_listener_likes_and_plays_as_data(shared):
    li = Listener(likes=[Like("Seed", "Seed song 0", at=100), Like("Seed", "Seed song 1", at=90)],
                  plays=[Play("Alpha", "Alpha song 0")])
    f = shared.flow(explore=0, n=6, stops=2, rng_seed=1, listener=li)
    titles = {(t.artist, t.title) for t in f.tracks}
    assert ("Seed", "Seed song 0") not in titles and ("Alpha", "Alpha song 0") not in titles  # liked / played
    r = shared.generate(artist="Seed", n=20, per_artist=20, explore=0.5, listener=li)
    assert ("Alpha", "Alpha song 0") not in {(t.artist, t.title) for t in r.tracks}
    assert any(t.familiar for t in r.tracks if t.artist == "Alpha")  # played artist counts as known
