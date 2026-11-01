"""Data build and fetch: ListenBrainz data reads, weights, store step, packaging, and verified install."""
import io
import json
import shutil
import tarfile
from pathlib import Path

import pytest

from postradio import data
from postradio.genres import GenreGraph
from postradio.models import Artist, Edge
from postradio.providers.listenbrainz import LBData
from postradio.providers.musicbrainz_store import INDEX_FILE, STORE_FILE, ArtistStore

NODES = {"hip hop", "trap", "drill", "jazz"}


class FakeHttp:
    """get: url-suffix routes; text: dict; download: copies local files (url -> Path) or writes bytes."""

    def __init__(self, routes=None, texts=None, files=None):
        self.routes, self.texts, self.files = routes or {}, texts or {}, files or {}
        self.downloads = []

    def get(self, url, params=None, **_):
        for suffix, payload in self.routes.items():
            if url.endswith(suffix):
                return payload(params or {}) if callable(payload) else payload
        return None

    def text(self, url, **_):
        return self.texts.get(url)

    def download(self, url, dest: Path, resume=True, log=None, headers=None):
        self.downloads.append(url)
        src = self.files[url]
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(src, bytes):
            dest.write_bytes(src)
        else:
            shutil.copy(src, dest)
        return dest


def test_lb_data_sitewide_artists_and_genres():
    pages = {0: {"payload": {"artists": [{"artist_name": "A", "artist_mbid": "a"}, {"artist_name": "NoId"},
                                         {"artist_name": "B", "artist_mbid": "b"}]}},
             100: {"payload": {"artists": []}}}
    meta = [{"artist_mbid": "a", "tag": {"artist": [
        {"tag": "trap", "count": 2, "genre_mbid": "g1"}, {"tag": "hip hop", "count": 5, "genre_mbid": "g2"},
        {"tag": "seen live", "count": 9}, {"tag": "drill", "count": 0, "genre_mbid": "g3"}]}},
        {"artist_mbid": "zzz", "tag": {}}, "garbage"]
    http = FakeHttp({"/stats/sitewide/artists": lambda p: pages[p["offset"]], "/metadata/artist/": meta})
    lb = LBData(http)
    assert lb.sitewide_artists(5) == [Artist("A", mbid="a"), Artist("B", mbid="b")]
    assert lb.artist_genres(["a", "b"], top=5) == {"a": ["hip hop", "trap"], "b": []}


class FakeLB:
    def __init__(self, seeds, tags):
        self.seeds, self.tags = seeds, tags

    def sitewide_artists(self, n):
        return [Artist(m, mbid=m) for m in self.seeds][:n]

    def artist_genres(self, mbids, top=5):
        return {m: self.tags.get(m, []) for m in mbids}


class FakeSim:
    def __init__(self, sim):
        self.sim = sim

    def neighbours(self, a, limit):  # LBSimilar's weighting: 0.5 + 0.5 × relative score
        return [Edge(Artist(m, mbid=m), 0.5 + 0.5 * s, "lb") for m, s in self.sim.get(a.mbid, [])][:limit]


def test_build_weights_from_seeds_pairs_and_genres(tmp_path):
    tags = {"s1": ["hip hop", "trap"], "s2": ["hip hop", "trap"], "s3": ["jazz"], "x": ["drill"],
            "y": ["drill", "not-in-graph"]}
    sim = {"s1": [("x", 1.0), ("y", 0.5)], "s2": [("x", 1.0)], "s3": []}
    out = data.build_weights(FakeLB(["s1", "s2", "s3"], tags), FakeSim(sim), tmp_path, NODES,
                             data.BuildOptions(seeds=3, similar=10), log=lambda _m: None)
    c = out["counts"]
    assert c["seeds"] == 3 and c["similar_pairs"] == 3 and c["artists_looked_up"] == 5
    assert out["cotag"] == {"hip hop|trap": 1.0}  # both seeds carry both: maximal association
    assert set(out["similar"]) == {"drill|hip hop", "drill|trap"}  # off-graph genre dropped
    assert json.loads((tmp_path / "genre_weights.json").read_text())["counts"] == c


def make_dump(path: Path) -> None:
    artists = [{"id": "ye", "name": "Ye", "sort-name": "Ye", "aliases": [{"name": "Kanye West"}],
                "genres": [{"name": "hip hop", "count": 9}]},
               {"id": "t", "name": "Trapper", "sort-name": "Trapper", "genres": [{"name": "trap", "count": 3}]}]
    body = "\n".join(json.dumps(a) for a in artists).encode()
    with tarfile.open(path, "w:xz") as tar:
        info = tarfile.TarInfo("mbdump/artist")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))


def test_build_store_step_downloads_imports_indexes_and_drops_the_dump(tmp_path):
    dump = tmp_path / "src.tar.xz"
    make_dump(dump)
    http = FakeHttp(texts={f"{data.DUMPS}/LATEST": "20261031-001001\n"},
                    files={f"{data.DUMPS}/20261031-001001/artist.tar.xz": dump})
    d = tmp_path / "data"
    d.mkdir()
    assert data.build_store(http, d, NODES, data.BuildOptions(), log=lambda _m: None) == "20261031-001001"
    assert ArtistStore(d / STORE_FILE).resolve("kanye west")[0]["mbid"] == "ye"
    idx = json.loads((d / INDEX_FILE).read_text())
    assert idx["genres"] == {"hip hop": [["ye", "Ye"]], "trap": [["t", "Trapper"]]} and idx["dump"] == "20261031-001001"
    assert not (d / "artist.tar.xz").exists()


def test_build_skipping_the_dump_still_indexes_an_existing_store(tmp_path):
    dump = tmp_path / "src.tar.xz"
    make_dump(dump)
    d = tmp_path / "data"
    d.mkdir()
    http = FakeHttp(texts={f"{data.DUMPS}/LATEST": "D1"}, files={f"{data.DUMPS}/D1/artist.tar.xz": dump})
    data.build_store(http, d, NODES, data.BuildOptions(), log=lambda _m: None)
    (d / INDEX_FILE).unlink()
    out = data.build(FakeHttp(), None, d, tmp_path / "dist", data.BuildOptions(skip=frozenset({"graph", "dump",
                                                                                              "weights"})),
                     log=lambda _m: None)
    assert (d / INDEX_FILE).exists() and (out / INDEX_FILE).exists()


def test_build_rejects_unknown_steps(tmp_path):
    with pytest.raises(ValueError, match="unknown build step"):
        data.build(FakeHttp(), None, tmp_path, tmp_path, data.BuildOptions(skip=frozenset({"grpah"})))


def release_from(out: Path, tag: str) -> tuple[FakeHttp, dict]:
    base = f"https://example.invalid/{tag}"
    assets = [{"name": p.name, "browser_download_url": f"{base}/{p.name}"} for p in sorted(out.iterdir())]
    rel = {"tag_name": tag, "assets": assets}
    http = FakeHttp({"/releases": [{"tag_name": "v1.0.0"}, {"tag_name": "data-2026-09"}, rel],
                     f"/releases/tags/{tag}": rel},
                    files={a["browser_download_url"]: out / a["name"] for a in assets})
    return http, rel


def test_package_then_fetch_installs_verified_files(tmp_path):
    src = tmp_path / "built"
    dump = tmp_path / "src.tar.xz"
    make_dump(dump)
    http = FakeHttp(texts={f"{data.DUMPS}/LATEST": "D1"}, files={f"{data.DUMPS}/D1/artist.tar.xz": dump})
    src.mkdir()
    data.build_store(http, src, NODES, data.BuildOptions(), log=lambda _m: None)
    (src / "genre_weights.json").write_text(json.dumps({"cotag": {"hip hop|trap": 0.5}, "similar": {}}))
    out = data.package(src, tmp_path / "dist", "D1", log=lambda _m: None)
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["musicbrainz_dump"] == "D1"
    assert set(manifest["files"]) == {"genre_graph.json", "genre_weights.json", "genre_artists.json",
                                      "mb_artists.sqlite.xz", "LICENSE-CC-BY-NC-SA-3.0.txt"}
    assert "CC BY-NC-SA 3.0" in (out / "RELEASE.md").read_text()

    http, _ = release_from(out, out.name)
    dest = tmp_path / "installed"
    got = data.fetch(http, dest, log=lambda _m: None)  # newest data-* wins over data-2026-09 and v1.0.0
    assert got["tag"] == out.name and STORE_FILE in got["files"]
    assert ArtistStore.open(dest).resolve("Kanye West")[0]["mbid"] == "ye"
    assert GenreGraph.load({"subgenre_of": 1.0}, 0.2, data_dir=dest).learned
    st = data.status(dest)
    assert st["release"]["tag"] == out.name and st["files"][STORE_FILE] > 0

    genres_only = tmp_path / "light"
    data.fetch(http, genres_only, tag=out.name, store=False, log=lambda _m: None)
    assert not (genres_only / STORE_FILE).exists() and (genres_only / INDEX_FILE).exists()


def test_dry_fetch_downloads_only_the_manifest_and_says_what_would_change(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "genre_weights.json").write_text('{"cotag": {}, "similar": {}}')
    out = data.package(tmp_path / "src", tmp_path / "dist", "D1", log=lambda _m: None)
    http, _ = release_from(out, out.name)
    dest = tmp_path / "installed"
    dest.mkdir()
    shutil.copy(out / "genre_graph.json", dest / "genre_graph.json")  # already up to date
    (dest / "genre_weights.json").write_text("{}")  # an older version
    got = data.check(http, dest)
    states = {f["file"]: f["state"] for f in got["files"]}
    assert states == {"genre_graph.json": "same", "genre_weights.json": "changed"}  # what fetch would install
    assert got["has_store"] is False and got["download_bytes"] > 0
    assert [u.rsplit("/", 1)[1] for u in http.downloads] == ["manifest.json"]  # nothing else pulled
    assert (dest / "genre_weights.json").read_text() == "{}"  # nothing installed


def test_fetch_installs_nothing_on_checksum_mismatch(tmp_path):
    (tmp_path / "empty").mkdir()
    out = data.package(tmp_path / "empty", tmp_path / "dist", "D1", log=lambda _m: None)
    (out / "genre_graph.json").write_text("{}")  # tampered after the manifest was written
    http, _ = release_from(out, out.name)
    dest = tmp_path / "installed"
    with pytest.raises(ValueError, match="checksum mismatch"):
        data.fetch(http, dest, log=lambda _m: None)
    assert not (dest / "genre_graph.json").exists() and not (dest / data.INSTALLED).exists()


def test_fetch_without_a_data_release(tmp_path):
    with pytest.raises(FileNotFoundError):
        data.fetch(FakeHttp({"/releases": [{"tag_name": "v1.0.0"}]}), tmp_path, log=lambda _m: None)


def test_http_download_resumes_a_partial_file(tmp_path, monkeypatch):
    from postradio.http import Http

    class Resp:
        def __init__(self, status, body):
            self.status_code, self.body = status, body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            assert self.status_code < 400

        def iter_content(self, n):
            yield self.body

    seen = []
    http = Http(tmp_path / "c.sqlite", min_interval={"default": 0})

    def get(url, headers=None, **_):
        seen.append(headers.get("Range"))
        return Resp(206, b"world") if headers.get("Range") else Resp(200, b"hello world")

    monkeypatch.setattr(http.session, "get", get)
    dest = tmp_path / "f.bin"
    http.download("https://x.invalid/f", dest)
    assert dest.read_bytes() == b"hello world" and seen == [None]
    dest.write_bytes(b"hello ")
    http.download("https://x.invalid/f", dest)
    assert dest.read_bytes() == b"hello world" and seen[-1] == "bytes=6-"
    http.close()
