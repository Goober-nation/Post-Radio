"""Data build and fetch: the optional files in `storage.data_dir`, and the `data-YYYY-MM` releases that carry
them. Orchestration only: every fetch goes through an adapter (Wikidata, MusicBrainz, ListenBrainz) or `Http`.

build: Wikidata genre graph -> MusicBrainz JSON dump -> artist store + genre -> artists index ->
       ListenBrainz-based genre weights -> release folder (compressed store, genre files, licence, manifest).
fetch: newest `data-*` GitHub pre-release of this repository -> verify every file against manifest.json ->
       install into data_dir.

Licences: the genre graph is CC0 (Wikidata). The store, the index and the weights carry MusicBrainz genre data
under CC BY-NC-SA 3.0; release notes and the manifest say so. The code is MIT."""
import hashlib
import json
import lzma
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .brand import DATA_RELEASE_REPO
from .genres import GRAPH_FILE, PACKAGED_GRAPH, WEIGHTS_FILE, cotag, coverage, popularity, similar
from .http import Http
from .providers.listenbrainz import LBData, LBSimilar
from .providers.musicbrainz import MusicBrainz
from .providers.musicbrainz_store import INDEX_FILE, STORE_FILE, ArtistStore, import_dump
from .providers.wikidata import Wikidata

DUMPS = "https://data.metabrainz.org/pub/musicbrainz/data/json-dumps"
DUMP_FILE = "artist.tar.xz"
BUILD_INFO = "build.json"  # in data_dir: what the last build used (dump date), so a repack can name its source
INSTALLED = "data_release.json"  # in data_dir: which release is installed
LICENCE_FILE = Path(__file__).resolve().parent / "data" / "LICENSE-CC-BY-NC-SA-3.0.txt"
STEPS = ("graph", "dump", "weights")
NC_SA = "CC-BY-NC-SA-3.0"
LICENCE = {GRAPH_FILE: "CC0-1.0 (Wikidata)",
           WEIGHTS_FILE: f"{NC_SA} (MusicBrainz genre tags; ListenBrainz data CC0)",
           INDEX_FILE: f"{NC_SA} (MusicBrainz genre votes)",
           STORE_FILE + ".xz": f"{NC_SA} (MusicBrainz genre votes; core artist data CC0)",
           LICENCE_FILE.name: "licence text"}
RELEASE_NOTES = """Genre graph, genre weights, genre → artists index and the MusicBrainz artist store, built {built}
from MusicBrainz JSON dump {dump}. Install with `postradio data fetch` (checks manifest.json), or rebuild with
`postradio data build`.

**Licences.** `genre_graph.json` is CC0 (from Wikidata). `mb_artists.sqlite.xz`, `genre_artists.json` and
`genre_weights.json` contain data © MusicBrainz contributors: genres and tags are MusicBrainz supplementary data
under [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/) (attribution, non-commercial,
share-alike); full text in `LICENSE-CC-BY-NC-SA-3.0.txt`. Similar-artist data comes from ListenBrainz (CC0).
The application code is MIT and is not covered by this licence.
"""


@dataclass
class BuildOptions:
    skip: frozenset[str] = frozenset()  # any of STEPS
    keep_dump: bool = False
    seeds: int = 1000  # ListenBrainz sitewide top artists (the API stops at about 1000)
    similar: int = 10  # similar artists per seed
    index_cap: int = 300  # artists per genre in genre_artists.json
    top_tags: int = 5  # an artist's most-voted genres; stray single votes are noise


def graph_path(data_dir: Path) -> Path:
    """The installed/built graph when present (it matches the store), else the packaged one."""
    p = data_dir / GRAPH_FILE
    return p if p.is_file() else PACKAGED_GRAPH


def _write_json(path: Path, data, **kw) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, **kw))
    tmp.replace(path)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


# ---------------------------------------------------------------- build steps
def build_graph(mb: MusicBrainz, wd: Wikidata, data_dir: Path, log: Callable[[str], None]) -> dict:
    g = wd.genre_graph(mb.genre_names())
    if not g["nodes"]:
        raise RuntimeError("Wikidata returned an empty genre graph")
    _write_json(data_dir / GRAPH_FILE, g, indent=1)
    log(f"graph: {len(g['nodes'])} genres, {len(g['edges'])} edges")
    return g


def build_store(http: Http, data_dir: Path, nodes: set[str], opts: BuildOptions,
                log: Callable[[str], None]) -> str:
    """Latest artist dump -> store -> index. Returns the dump's date (its folder name)."""
    latest = (http.text(f"{DUMPS}/LATEST") or "").strip()
    if not latest:
        raise RuntimeError(f"couldn't read {DUMPS}/LATEST")
    archive = data_dir / DUMP_FILE
    stamp = data_dir / (DUMP_FILE + ".date")
    if stamp.is_file() and stamp.read_text().strip() != latest:
        archive.unlink(missing_ok=True)  # a partial file from an older dump can't be resumed
    stamp.write_text(latest)
    log(f"dump {latest}: downloading {DUMP_FILE} (~1.7 GB, resumable)")
    http.download(f"{DUMPS}/{latest}/{DUMP_FILE}", archive, log=log)
    import_dump(archive, data_dir / STORE_FILE, nodes, log=log)
    store = ArtistStore(data_dir / STORE_FILE)
    try:
        idx = store.genre_index(nodes, opts.index_cap)
    finally:
        store.close()
    _write_json(data_dir / INDEX_FILE, {"source": "MusicBrainz JSON dump; genre votes are CC BY-NC-SA 3.0",
                                        "dump": latest, "cap": opts.index_cap,
                                        "format": "genre -> [[mbid, name], ...] strongest first", "genres": idx},
                separators=(",", ":"))
    log(f"index: {len(idx)} genres with artists, {sum(map(len, idx.values())):,} entries")
    if not opts.keep_dump:
        archive.unlink(missing_ok=True)
        stamp.unlink(missing_ok=True)
    return latest


def build_weights(lb: LBData, sim: LBSimilar, data_dir: Path, nodes: set[str], opts: BuildOptions,
                  log: Callable[[str], None], workers: int = 4) -> dict:
    """Seeds = ListenBrainz sitewide top artists; pairs = their LB similar artists; genres = every involved
    artist's top MusicBrainz genres (via LB metadata), restricted to the graph."""
    seeds = lb.sitewide_artists(opts.seeds)
    log(f"weights: {len(seeds)} seed artists")
    with ThreadPoolExecutor(workers) as ex:  # Http paces the host; this only overlaps latency
        got = list(ex.map(lambda a: sim.neighbours(a, opts.similar), seeds))
    # LBSimilar weights are 0.5 + 0.5 × (score / best score); undo the offset to get the plain 0..1 strength
    pairs = [(a.mbid, e.target.mbid, round((e.weight - 0.5) * 2, 6)) for a, edges in zip(seeds, got, strict=True)
             for e in edges if e.target.mbid]
    log(f"weights: {len(pairs)} similar pairs")
    involved = sorted({a.mbid for a in seeds} | {m for p in pairs for m in p[:2]})
    tags = lb.artist_genres(involved, opts.top_tags)
    at = {m: {g for g in gs if g in nodes} for m, gs in tags.items()}
    at = {m: gs for m, gs in at.items() if gs}
    seed_tags = {a.mbid: at[a.mbid] for a in seeds if a.mbid in at}
    ct, sm = cotag(seed_tags), similar(pairs, at)
    out = {"cotag": {"|".join(p): w for p, w in ct.items()}, "similar": {"|".join(p): w for p, w in sm.items()},
           "popularity": popularity(at), "fetched": time.strftime("%Y-%m-%d"),
           "counts": {"seeds": len(seeds), "artists_looked_up": len(involved), "artists_with_genres": len(at),
                      "seed_tag_coverage": round(len(seed_tags) / max(len(seeds), 1), 3),
                      "similar_pairs": len(pairs), "cotag_edges": len(ct), "similar_edges": len(sm),
                      "cotag_genres": coverage(ct), "similar_genres": coverage(sm), "graph_genres": len(nodes)}}
    _write_json(data_dir / WEIGHTS_FILE, out)
    log("weights: " + ", ".join(f"{k} {v}" for k, v in out["counts"].items()))
    return out


def compress(src: Path, dst: Path) -> None:
    if shutil.which("xz"):  # multithreaded, much faster than Python's lzma
        with dst.open("wb") as out:
            subprocess.run(["xz", "-T0", "-6", "-c", str(src)], stdout=out, check=True)
    else:
        with src.open("rb") as f, lzma.open(dst, "wb", preset=6) as out:
            shutil.copyfileobj(f, out, 1 << 20)


def package(data_dir: Path, out_root: Path, dump: str, log: Callable[[str], None]) -> Path:
    """data_dir files -> out_root/data-YYYY-MM/ with manifest.json and RELEASE.md. Missing files are skipped."""
    tag = f"data-{time.strftime('%Y-%m')}"
    out = out_root / tag
    out.mkdir(parents=True, exist_ok=True)
    shutil.copy2(graph_path(data_dir), out / GRAPH_FILE)
    for name in (WEIGHTS_FILE, INDEX_FILE):
        if (data_dir / name).is_file():
            shutil.copy2(data_dir / name, out / name)
    shutil.copy2(LICENCE_FILE, out / LICENCE_FILE.name)
    if (data_dir / STORE_FILE).is_file():
        log("package: compressing the store")
        compress(data_dir / STORE_FILE, out / (STORE_FILE + ".xz"))
    files = {p.name: {"bytes": p.stat().st_size, "sha256": sha256(p), "licence": LICENCE.get(p.name, "")}
             for p in sorted(out.iterdir()) if p.name not in ("manifest.json", "RELEASE.md")}
    manifest = {"tag": tag, "built": time.strftime("%Y-%m-%d"), "musicbrainz_dump": dump, "files": files}
    _write_json(out / "manifest.json", manifest, indent=1)
    (out / "RELEASE.md").write_text(RELEASE_NOTES.format(built=manifest["built"], dump=dump))
    log(f"package: {out}: " + ", ".join(f"{k} {v['bytes'] / 1e6:.1f} MB" for k, v in files.items()))
    log(f"upload: gh release create {tag} --repo {DATA_RELEASE_REPO} --prerelease --title {tag} "
        f"--notes-file {out}/RELEASE.md {out}/*")
    return out


def build(http: Http, mb: MusicBrainz, data_dir: Path, out_root: Path, opts: BuildOptions | None = None,
          log: Callable[[str], None] = print) -> Path:
    opts = opts or BuildOptions()
    unknown = set(opts.skip) - set(STEPS)
    if unknown:
        raise ValueError(f"unknown build step(s) {sorted(unknown)} (steps: {', '.join(STEPS)})")
    data_dir.mkdir(parents=True, exist_ok=True)
    info_path = data_dir / BUILD_INFO
    info = json.loads(info_path.read_text()) if info_path.is_file() else {}
    if "graph" not in opts.skip:
        build_graph(mb, Wikidata(http), data_dir, log)
    nodes = set(json.loads(graph_path(data_dir).read_text())["nodes"])
    if "dump" not in opts.skip:
        info["musicbrainz_dump"] = build_store(http, data_dir, nodes, opts, log)
        _write_json(info_path, info)
    if "weights" not in opts.skip:
        build_weights(LBData(http), LBSimilar(http), data_dir, nodes, opts, log)
    return package(data_dir, out_root, info.get("musicbrainz_dump", "unknown"), log)


# ---------------------------------------------------------------- fetch
def release(http: Http, tag: str | None, repo: str = DATA_RELEASE_REPO) -> dict:
    """A data release by tag, or the newest `data-*` one (pre-releases included)."""
    api = f"https://api.github.com/repos/{repo}/releases"
    if tag:
        rel = http.get(f"{api}/tags/{tag}", max_age=600)
        if not isinstance(rel, dict) or not rel.get("tag_name"):
            raise FileNotFoundError(f"no release {tag} in {repo}")
        return rel
    rels = http.get(api, params={"per_page": 50}, max_age=600)
    data = [r for r in rels if isinstance(r, dict) and str(r.get("tag_name", "")).startswith("data-")] \
        if isinstance(rels, list) else []
    if not data:
        raise FileNotFoundError(f"no data-* release in {repo}")
    return max(data, key=lambda r: r["tag_name"])


def fetch(http: Http, data_dir: Path, tag: str | None = None, store: bool = True, repo: str = DATA_RELEASE_REPO,
          log: Callable[[str], None] = print) -> dict:
    """Download a release, check every file against its manifest, then install into data_dir. Nothing is
    installed unless every wanted file verifies. store=False leaves the big artist store out."""
    rel = release(http, tag, repo)
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets") or []
              if isinstance(a, dict) and a.get("name") and a.get("browser_download_url")}
    if "manifest.json" not in assets:
        raise FileNotFoundError(f"{rel['tag_name']}: no manifest.json")
    log(f"release {rel['tag_name']}: {', '.join(sorted(assets))}")
    data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=data_dir) as tmp:
        tmp = Path(tmp)
        http.download(assets["manifest.json"], tmp / "manifest.json", resume=False)
        manifest = json.loads((tmp / "manifest.json").read_text())
        files = manifest.get("files") or {}
        wanted = [n for n in files if n.endswith(".json") or (store and n == STORE_FILE + ".xz")]
        for name in wanted:
            if name not in assets:
                raise FileNotFoundError(f"{rel['tag_name']}: manifest lists {name} but the release lacks it")
            http.download(assets[name], tmp / name, resume=False, log=log)
            if sha256(tmp / name) != files[name]["sha256"]:
                raise ValueError(f"{name}: checksum mismatch, nothing installed")
            log(f"  {name} ok")
        for name in wanted:
            if name == STORE_FILE + ".xz":
                part = data_dir / (STORE_FILE + ".tmp")
                with lzma.open(tmp / name) as src, part.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                part.replace(data_dir / STORE_FILE)
            else:
                shutil.copy2(tmp / name, data_dir / name)
    installed = {"tag": rel["tag_name"], "musicbrainz_dump": manifest.get("musicbrainz_dump"),
                 "files": sorted(n.removesuffix(".xz") for n in wanted), "installed": time.strftime("%Y-%m-%d")}
    _write_json(data_dir / INSTALLED, installed, indent=1)
    log(f"installed {rel['tag_name']} (MusicBrainz dump {manifest.get('musicbrainz_dump')}) into {data_dir}")
    return installed


def status(data_dir: Path) -> dict:
    """What's in data_dir: each optional file (size or absent), the installed release, the last build."""
    def size(name: str) -> int | None:
        p = data_dir / name
        return p.stat().st_size if p.is_file() else None

    def read(name: str) -> dict | None:
        p = data_dir / name
        return json.loads(p.read_text()) if p.is_file() else None

    return {"data_dir": str(data_dir), "graph": str(graph_path(data_dir)),
            "files": {n: size(n) for n in (STORE_FILE, WEIGHTS_FILE, INDEX_FILE, GRAPH_FILE)},
            "release": read(INSTALLED), "build": read(BUILD_INFO)}

