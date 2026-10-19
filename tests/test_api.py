"""Config, result cache, save/load and the HTTP wrapper."""
import dataclasses
import time

import pytest
import yaml
from fakes import Resolver, Similar, Tracks
from fastapi.testclient import TestClient

from pvrecs import config as config_mod
from pvrecs.api import Recommender, ResultCache, slug
from pvrecs.engine import Providers, Radio
from pvrecs.server import create_app


@pytest.fixture
def rec(tmp_path):
    cfg = config_mod.Config()
    cfg = dataclasses.replace(cfg, storage=dataclasses.replace(cfg.storage, save_dir=str(tmp_path / "saved"),
                                                               http_cache=str(tmp_path / "http.sqlite")),
                              sources=dataclasses.replace(cfg.sources, history="none"),
                              feedback=dataclasses.replace(cfg.feedback, enabled=False))
    r = Recommender(cfg, token="")
    r.radio = Radio(Providers(resolver=Resolver(), similar=[Similar()], tracks=[Tracks()]), cfg.advanced,
                    log=lambda *_: None)
    return r


def test_config_partial_yaml_and_unknown_keys(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("defaults: {n: 7}\nadvanced: {source_weights: {deezer: 0.1}, fanout: {1: 3}}\n")
    c = config_mod.load(p)
    assert c.defaults.n == 7 and c.defaults.explore == 0.5
    assert c.advanced.source_weights["deezer"] == 0.1 and c.advanced.source_weights["lb"] == 1.0
    assert c.advanced.fanout[1] == 3 and c.advanced.fanout[2] == 8
    p.write_text("defaults: {explor: 1}\n")
    with pytest.raises(ValueError, match="defaults.explor"):
        config_mod.load(p)


def test_example_config_matches_defaults():
    data = yaml.safe_load((config_mod.ROOT / "config.example.yaml").read_text())
    assert config_mod.load(config_mod.ROOT / "config.example.yaml") == config_mod.Config()
    assert set(data) == {"defaults", "mix", "storage", "sources", "feedback", "server", "advanced"}


def test_result_cache_ttl_and_lru(monkeypatch):
    c = ResultCache(ttl_s=10, max_items=2)
    c.put("a", {"x": 1})
    c.put("b", {"x": 2})
    c.put("c", {"x": 3})
    assert c.get("a") is None and c.get("c") == {"x": 3}
    now = time.monotonic()
    monkeypatch.setattr("pvrecs.api.time.monotonic", lambda: now + 11)
    assert c.get("c") is None


def test_generate_uses_config_defaults_and_save_roundtrip(rec):
    res = rec.generate(artist="Seed")
    assert len(res["tracks"]) <= rec.cfg.defaults.n and res["explore"] == rec.cfg.defaults.explore
    assert rec.get(res["result_id"]) is res
    assert "_seed_e0.50_pauto" in res["default_name"]
    p = rec.save(res["result_id"])
    assert p.stem == res["default_name"] and p.parent == rec.save_dir
    named = rec.save(res["result_id"], "My Fav/../x")
    assert named.parent == rec.save_dir and named.stem == "my-fav-..-x"
    with pytest.raises(FileExistsError):
        rec.save(res["result_id"], "My Fav/../x")
    assert {s["name"] for s in rec.list_saved()} == {p.stem, named.stem}
    assert rec.load_saved(p.stem)["tracks"] == res["tracks"]
    with pytest.raises(KeyError):
        rec.save("nope")


def test_slug_is_path_safe():
    assert "/" not in slug("../../etc/passwd") and slug("") == "playlist"


def test_http_server_flow(rec):
    client = TestClient(create_app(rec))
    assert client.post("/radio", json={"artist": "Seed", "track": "x - y"}).status_code == 422
    assert client.post("/radio", json={"artist": "Seed", "explore": 2}).status_code == 422
    r = client.post("/radio", json={"artist": "Seed", "n": 5, "explore": 0.5})
    assert r.status_code == 200 and len(r.json()["tracks"]) == 5
    rid = r.json()["result_id"]
    assert client.get(f"/results/{rid}").json()["result_id"] == rid
    saved = client.post(f"/results/{rid}/save", json={"name": "mine"})
    assert saved.json() == {"saved_as": "mine"}
    assert client.post(f"/results/{rid}/save", json={"name": "mine"}).status_code == 409
    assert client.get("/saved").json()[0]["name"] == "mine"
    assert client.get("/saved/mine").json()["result_id"] == rid
    assert client.post("/radio", json={"artist": "Nobody"}).status_code == 404
