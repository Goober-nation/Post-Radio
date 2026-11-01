"""The same scenarios through every entry point (Python, CLI, HTTP sync, HTTP job), with the outside services
replaced by the fakes but everything else real: config files, the CLI's own handling, the server, the
feedback store, and config-driven taste vs per-call Listener taste. Bugs that live *between* those layers
(a rule only the CLI applied, a field only one endpoint sent) show up here."""
import json
import time

import pytest
from fakes import Resolver, Similar, Tracks
from fastapi.testclient import TestClient
from test_flow import GenreArtists, MBTags, graph

from postradio import cli
from postradio import config as config_mod
from postradio.api import Listener, Recommender
from postradio.engine import Providers
from postradio.models import Like
from postradio.providers import NoHistory
from postradio.server import create_app

LOVES = {"loves": {"tracks": [{"artist": "Seed", "title": "Seed song 0"}, {"artist": "Seed", "title": "Seed song 1"}]}}


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Every Recommender built anywhere (CLI included) gets fake outside services but its real taste wiring."""

    def providers(self, token=None, history=None, feedback=None, yandex=None):
        return Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()],
                         feedback=self.feedback if feedback is None else feedback,
                         history=(self.history if history is None else history) or NoHistory(),
                         profile_taggers={"mb": MBTags()}, genres=[GenreArtists()])

    monkeypatch.setattr(Recommender, "_providers", providers)
    monkeypatch.setattr(Recommender, "genre_graph", lambda self: graph())
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yaml").write_text("sources: {history: none}\nfeedback: {sources: [json]}\n")
    monkeypatch.setenv("POSTRADIO_HOME", str(home))
    monkeypatch.delenv("POSTRADIO_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    return home


def library(home) -> Recommender:
    """What a host app does: an explicit config file and home, nothing from the environment."""
    return Recommender(config_mod.load(home / "config.yaml"), home=home)


def run_cli(capsys, *argv) -> dict:
    cli.main([*argv, "--json"])
    return json.loads(capsys.readouterr().out)


def wait_job(client, url) -> dict:
    for _ in range(300):
        got = client.get(url).json()
        if got["status"] in ("done", "error"):
            return got
        time.sleep(0.01)
    raise AssertionError("job never finished")


def tracks(d: dict) -> list[tuple]:
    return [(t["artist"], t["title"], t.get("hop"), t.get("stop")) for t in d["tracks"]]


def test_radio_is_the_same_through_python_cli_http_and_jobs(world, capsys):
    args = {"artist": "Seed", "n": 6, "explore": 0.5, "rng_seed": 7}
    with library(world) as rec:
        py = rec.generate(**args).to_dict()
        with TestClient(create_app(rec)) as client:
            sync = client.post("/radio", json=args).json()
            job = wait_job(client, client.post("/jobs/radio", json=args).json()["status_url"])["result"]
    shell = run_cli(capsys, "radio", "--artist", "Seed", "-n", "6", "-e", "0.5", "--rng-seed", "7")
    assert tracks(py) == tracks(sync) == tracks(job) == tracks(shell) and len(tracks(py)) == 6
    assert py["rng_seed"] == sync["rng_seed"] == shell["rng_seed"] == 7


def test_flow_from_config_taste_equals_flow_from_a_listener(world, capsys):
    """Imported loves in the configured store (CLI path) vs the same loves as per-call data (library/HTTP)."""
    (world / "loves.json").write_text(json.dumps(LOVES))
    cli.main(["feedback", "import", str(world / "loves.json")])
    capsys.readouterr()
    shell = run_cli(capsys, "flow", "-e", "0.5", "-n", "6", "--stops", "3", "--rng-seed", "4")
    li = Listener(likes=[Like(t["artist"], t["title"]) for t in LOVES["loves"]["tracks"]])
    with library(world) as rec:
        from_config = rec.flow(explore=0.5, n=6, stops=3, rng_seed=4).to_dict()  # same store, library path
    with Recommender(home=world / "other") as rec:  # no config, no store: taste only from the listener
        from_listener = rec.flow(explore=0.5, n=6, stops=3, rng_seed=4, listener=li).to_dict()
        with TestClient(create_app(rec)) as client:
            body = {"explore": 0.5, "n": 6, "stops": 3, "rng_seed": 4,
                    "listener": {"likes": [{"artist": t["artist"], "title": t["title"]}
                                           for t in LOVES["loves"]["tracks"]]}}
            over_http = client.post("/flow", json=body).json()
    assert shell["genre_path"] == from_config["genre_path"] == from_listener["genre_path"] == over_http["genre_path"]
    assert tracks(shell) == tracks(from_config) == tracks(from_listener) == tracks(over_http)
    assert len(tracks(shell)) == 6 and len(shell["genre_path"]) == 3
    assert ("Seed", "Seed song 0") not in {(t["artist"], t["title"]) for t in shell["tracks"]}  # liked: not again


def test_dry_previews_match_the_real_runs(world, capsys):
    (world / "loves.json").write_text(json.dumps(LOVES))
    cli.main(["feedback", "import", str(world / "loves.json")])
    capsys.readouterr()
    dry = run_cli(capsys, "flow", "-e", "1", "-n", "6", "--stops", "3", "--rng-seed", "9", "--dry")
    real = run_cli(capsys, "flow", "-e", "1", "-n", "6", "--stops", "3", "--rng-seed", dry["rng_seed"].__str__())
    assert dry["dry"] and dry["genre_path"] == real["genre_path"]
    g = run_cli(capsys, "radio", "--artist", "Seed", "--dry", "-e", "1")
    assert g["dry"] and g["seed"]["artist"] == "Seed" and "1" in {str(h) for h in g["hops"]}


def test_errors_are_the_same_kind_everywhere(world, capsys):
    with library(world) as rec, TestClient(create_app(rec)) as client:
        assert client.post("/radio", json={"artist": "Nobody"}).status_code == 404
        assert client.post("/radio", json={"artist": "Seed", "explore": 3}).status_code == 422
        assert client.post("/flow", json={"n": 3}).status_code == 422  # no likes anywhere
        job = wait_job(client, client.post("/jobs/radio", json={"artist": "Nobody"}).json()["status_url"])
        assert job["error"] == {"type": "NotFound", "message": job["error"]["message"], "status": 404}
    with pytest.raises(SystemExit, match="error"):
        cli.run(["radio", "--artist", "Nobody"])
    with pytest.raises(SystemExit, match="needs likes"):
        cli.run(["flow", "-n", "3"])


def test_a_cli_run_writes_only_inside_the_home(world, capsys, tmp_path):
    before = {p for p in tmp_path.rglob("*")}
    run_cli(capsys, "radio", "--artist", "Seed", "-n", "3", "--save", "kept")
    new = {p for p in tmp_path.rglob("*")} - before
    assert new and all(world in p.parents or p == world for p in new)
    assert (world / "state" / "playlists" / "kept.json").exists()
