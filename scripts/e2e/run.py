#!/usr/bin/env python3
"""End-to-end test run of Post-Radio against the real services. Standard library only.

    python3 scripts/e2e/run.py --yandex-export playlist.json            # new run
    python3 scripts/e2e/run.py --resume e2e-runs/<run>                    # continue a dead run
    python3 scripts/e2e/run.py --only flows,radios --venv e2e-runs/venv  # reuse an install

Parts: 1 install (fresh venv, pip install from GitHub or --source), 2 data release (dry check + fetch),
3 flows, 4 radios. Each case runs in its own process in the installed venv (scripts/e2e/case.py), so a crash
or a timeout loses only that case.

Everything lands in e2e-runs/<UTC timestamp>/ (gitignored: it holds the listener's playlist-derived results):
  events.jsonl   every step, timestamped (start, done, grade); tail it to follow a run
  results/       one JSON per finished case, written atomically (a dead run keeps every finished case)
  summary.md     regenerated after every case: times, grades, failed checks
  status.json    current part / case, counts
The data release goes to e2e-runs/home (shared between runs; --fresh-data for a run-local one).
The ListenBrainz token is read from $LISTENBRAINZ_TOKEN, passed per call, never written.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CASE = Path(__file__).with_name("case.py")
YANDEX_URL = "https://music.yandex.ru/playlists/lk.export-replay"
JUNK = re.compile(r"karaoke|tribute|originally performed|instrumental version|made famous|in the style of", re.I)

# ----------------------------------------------------------------- the cases
FLOWS = [
    ("flow-default", {"explore": 0.5, "n": 30, "stops": 5}, {"repeat": True, "dry_first": True}),
    ("flow-explore-0", {"explore": 0.0, "n": 30, "stops": 5}, {}),
    ("flow-explore-1", {"explore": 1.0, "n": 30, "stops": 5}, {}),
    ("flow-pop-low", {"explore": 0.5, "popularity": 0.1, "n": 30}, {}),
    ("flow-pop-high", {"explore": 0.5, "popularity": 0.9, "n": 30}, {}),
    ("flow-start-own", {"explore": 0.5, "n": 25, "start": "hip hop"}, {}),
    ("flow-start-foreign", {"explore": 0.3, "n": 25, "start": "shoegaze"}, {}),
    ("flow-short", {"explore": 0.5, "n": 12, "stops": 3, "per_artist": 1}, {}),
    ("flow-long", {"explore": 0.7, "n": 60, "stops": 8}, {}),
    ("flow-timeout", {"explore": 0.5, "n": 30, "timeout_s": 0.5}, {"expect_error": "Cancelled"}),
    ("flow-bad-start", {"explore": 0.5, "start": "hip hopp"}, {"expect_error": "InvalidInput"}),
]
RADIOS = [
    ("radio-artist-one", {"artist": "Mac Miller", "n": 30, "explore": 0.5}, {"repeat": True, "dry_first": True}),
    ("radio-artist-multi", {"artist": ["Mac Miller", "Steve Lacy", "General Elektriks"], "n": 30, "explore": 0.5}, {}),
    ("radio-artist-explore-0", {"artist": "Steve Lacy", "n": 30, "explore": 0.0}, {}),
    ("radio-artist-explore-1", {"artist": "Steve Lacy", "n": 30, "explore": 1.0}, {}),
    ("radio-artist-pop-low", {"artist": "Kanye West", "n": 30, "explore": 0.5, "popularity": 0.1}, {}),
    ("radio-artist-pop-high", {"artist": "Kanye West", "n": 30, "explore": 0.5, "popularity": 0.9}, {}),
    ("radio-artist-no-taste", {"artist": "Mac Miller", "n": 30, "explore": 0.5, "taste": False}, {}),
    ("radio-track-one", {"track": "Kendrick Lamar - DUCKWORTH.", "n": 30, "explore": 0.5}, {}),
    ("radio-track-multi", {"track": ["Chris Isaak - Wicked Game", "Steve Lacy - Bad Habit",
                                     "alt-J - Breezeblocks"], "n": 30, "explore": 0.5}, {}),
    ("radio-genre-one", {"genres": ["jazz rap"], "n": 30, "explore": 0.5}, {}),
    ("radio-genre-or", {"genres": ["neo soul", "trip hop"], "genre_op": "or", "n": 30, "explore": 0.5}, {}),
    ("radio-genre-and", {"genres": ["hip hop", "jazz"], "genre_op": "and", "n": 30, "explore": 0.5}, {}),
    ("radio-unknown-artist", {"artist": "Zqxjkv Nonexistent Band 9", "n": 10}, {"expect_error": "NotFound"}),
    ("radio-mixed-kinds", {"artist": "Mac Miller", "genres": ["jazz"], "n": 10}, {"expect_error": "InvalidInput"}),
]


# ----------------------------------------------------------------- run folder
class Run:
    def __init__(self, root: Path):
        self.root = root
        (root / "results").mkdir(parents=True, exist_ok=True)
        self.events = root / "events.jsonl"
        self.attempt = sum(1 for _ in (root / "attempts.jsonl").open()) + 1 if (root / "attempts.jsonl").exists() else 1
        with (root / "attempts.jsonl").open("a") as f:
            f.write(json.dumps({"attempt": self.attempt, "started": now(), "argv": sys.argv[1:]}) + "\n")

    def log(self, **event) -> None:
        event = {"at": now(), "attempt": self.attempt, **event}
        with self.events.open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        print(json.dumps(event, ensure_ascii=False), flush=True)

    def done(self, name: str) -> dict | None:
        p = self.root / "results" / f"{name}.json"
        if not p.exists():
            return None
        got = json.loads(p.read_text())
        return got if got.get("final") else None

    def save(self, name: str, data: dict) -> None:
        p = self.root / "results" / f"{name}.json"
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({**data, "final": True, "attempt": self.attempt, "saved": now()},
                                  ensure_ascii=False, indent=1))
        os.replace(tmp, p)

    def status(self, **s) -> None:
        (self.root / "status.json").write_text(json.dumps({"at": now(), "attempt": self.attempt, **s}, indent=1))


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").casefold()
    return re.sub(r"[^\w]+", " ", s).strip()


# ----------------------------------------------------------------- grading
def time_grade(seconds: float | None, limits=(30, 90, 240, 600)) -> str:
    if seconds is None:
        return "F"
    return next((g for g, lim in zip("ABCD", limits, strict=True) if seconds <= lim), "F")


def sanity_grade(checks: list[dict]) -> str:
    if not checks:
        return "-"
    share = sum(c["ok"] for c in checks) / len(checks)
    return "A" if share == 1 else "B" if share >= .85 else "C" if share >= .7 else "F"


def check(checks: list, name: str, ok: bool, detail="") -> None:
    checks.append({"check": name, "ok": bool(ok), "detail": str(detail)[:300]})


def playlist_checks(pl: dict, args: dict, liked: set, kind: str) -> tuple[list, dict]:
    checks: list[dict] = []
    tracks = pl.get("tracks") or []
    n = args.get("n", 50)
    per_artist = args.get("per_artist", 2)
    keys = [(norm(t["artist"]), norm(t["title"])) for t in tracks]
    by_artist: dict[str, int] = {}
    for a, _ in keys:
        by_artist[a] = by_artist.get(a, 0) + 1
    pops = [t["popularity"] for t in tracks if t.get("popularity") is not None]
    apops = [t["artist_popularity"] for t in tracks if t.get("artist_popularity") is not None]
    data = {
        "tracks": len(tracks), "artists": len(by_artist),
        "artist_mbid_share": round(sum(bool(t.get("artist_mbid")) for t in tracks) / max(1, len(tracks)), 2),
        "recording_mbid_share": round(sum(bool(t.get("recording_mbid")) for t in tracks) / max(1, len(tracks)), 2),
        "deezer_id_share": round(sum(bool(t.get("deezer_id")) for t in tracks) / max(1, len(tracks)), 2),
        "familiar_share": round(sum(bool(t.get("familiar")) for t in tracks) / max(1, len(tracks)), 2),
        "liked_repeats": sum(k in liked for k in keys),
        "mean_track_popularity": round(sum(pops) / len(pops), 2) if pops else None,
        "mean_artist_popularity": round(sum(apops) / len(apops), 2) if apops else None,
        "sources": sorted({s for t in tracks for s in t.get("sources") or []}),
        "via": sorted({v for t in tracks for v in t.get("via") or []}),
        "stats": pl.get("stats"),
    }
    check(checks, "length", len(tracks) >= 0.9 * n, f"{len(tracks)} of {n}")
    check(checks, "no duplicates", len(set(keys)) == len(keys), f"{len(keys) - len(set(keys))} dupes")
    check(checks, "per-artist cap", max(by_artist.values(), default=0) <= per_artist,
          f"max {max(by_artist.values(), default=0)} (cap {per_artist})")
    check(checks, "no junk titles", not any(JUNK.search(t["title"]) for t in tracks),
          [t["title"] for t in tracks if JUNK.search(t["title"])][:3])
    check(checks, "every track explained (via)", all(t.get("via") for t in tracks))
    check(checks, "artist variety", len(by_artist) >= min(len(tracks), n) / max(1, per_artist) * 0.6,
          f"{len(by_artist)} artists")
    check(checks, "rng_seed reported", pl.get("rng_seed") is not None)
    if kind == "flow":
        stops = args.get("stops", 5)
        path = pl.get("genre_path") or []
        check(checks, "liked tracks never repeat", data["liked_repeats"] == 0, data["liked_repeats"])
        check(checks, "path length = stops", len(path) == stops, f"{len(path)} vs {stops}: {path}")
        check(checks, "tracks belong to path stops", all(t.get("stop") in path for t in tracks))
        check(checks, "path has no repeats", len(set(path)) == len(path))
        if args.get("start"):
            check(checks, "starts at the chosen genre", path[:1] == [args["start"].casefold()], path[:1])
        inp = (pl.get("stats") or {}).get("input") or {}
        check(checks, "input mostly matched", inp.get("matched", 0) >= 0.5 * max(1, inp.get("considered", 0)), inp)
        data.update(genre_path=path, familiarity=pl.get("familiarity"), flow_score=pl.get("flow_score"),
                    input=inp, top_profile=(pl.get("profile") or [])[:5])
    else:
        hops = [t["hop"] for t in tracks if t.get("hop") is not None]
        data["mean_hop"] = round(sum(hops) / len(hops), 2) if hops else None
        seeds = [s.get("artist") for s in pl.get("seeds") or []]
        data["seeds"] = seeds
        if args.get("artist") or args.get("track"):
            want = args.get("artist") or args.get("track")
            want = [want] if isinstance(want, str) else want
            check(checks, "every seed echoed", len(seeds) == len(want), f"{seeds} vs {want}")
            if len(want) > 1:
                roots = {norm((t.get("path") or [""])[0]) for t in tracks}
                reached = [s for s in seeds if s and norm(s) in roots]
                check(checks, "several seeds feed the result", len(reached) >= 2, reached)
                data["seeds_reached"] = reached
        if args.get("taste") is False:
            check(checks, "taste off: no taste factors", all(
                (t.get("taste") or {}).get("artist", 0) == 0 and (t.get("taste") or {}).get("track", 0) == 0
                for t in tracks))
    return checks, data


def compare_checks(results: dict) -> list[dict]:
    """Checks across cases: the knobs move results the way they should."""
    out: list[dict] = []

    def get(name, key):
        r = results.get(name) or {}
        return (r.get("data") or {}).get(key)

    def cmp(name, a, b, key, lower_first=True):
        x, y = get(a, key), get(b, key)
        if x is None or y is None:
            return
        check(out, name, x < y if lower_first else x > y, f"{a}={x} {b}={y}")

    cmp("flow: explore 0 more familiar than 1", "flow-explore-0", "flow-explore-1", "familiar_share", False)
    cmp("flow: explore 0 higher genre familiarity than 1", "flow-explore-0", "flow-explore-1", "familiarity", False)
    cmp("flow: popularity 0.1 picks less popular tracks than 0.9", "flow-pop-low", "flow-pop-high",
        "mean_artist_popularity")
    cmp("radio: explore 0 stays closer than 1", "radio-artist-explore-0", "radio-artist-explore-1", "mean_hop")
    cmp("radio: popularity 0.1 picks less popular artists than 0.9", "radio-artist-pop-low", "radio-artist-pop-high",
        "mean_artist_popularity")
    for name in ("flow-default", "radio-artist-one"):
        r = results.get(name) or {}
        if "repeat_same" in (r.get("data") or {}):
            check(out, f"{name}: rng_seed repeats the playlist", r["data"]["repeat_same"],
                  r["data"].get("repeat_overlap"))
        if "dry_matches" in (r.get("data") or {}):
            check(out, f"{name}: dry preview matches the run", r["data"]["dry_matches"])
    return out


# ----------------------------------------------------------------- parts
def sh(cmd: list[str], env=None, timeout=1800) -> tuple[int, str, float]:
    t = time.monotonic()
    p = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr)[-4000:], round(time.monotonic() - t, 1)


def part_install(run: Run, args) -> dict:
    venv = Path(args.venv) if args.venv else run.root / "venv"
    if args.venv and (venv / "bin" / "postradio").exists():
        rc, out, secs = sh([str(venv / "bin" / "python"), "-c", "import postradio.brand as b; print(b.VERSION)"])
        return {"ok": rc == 0, "reused": str(venv), "seconds": 0, "version": out.strip(), "checks": []}
    shutil.rmtree(venv, ignore_errors=True)
    steps, checks = {}, []
    rc, out, steps["venv"] = sh([sys.executable, "-m", "venv", str(venv)])
    check(checks, "venv created", rc == 0, out[-200:])
    pip = [str(venv / "bin" / "pip"), "install", "-q", "--disable-pip-version-check"]
    rc, out, steps["pip"] = sh(pip + [args.source], timeout=1800)
    check(checks, "pip install", rc == 0, out[-500:])
    py = str(venv / "bin" / "python")
    rc, out, _ = sh([py, "-c", "import postradio, postradio.brand as b, pathlib; "
                               "p = pathlib.Path(postradio.__file__).parent; "
                               "print(b.VERSION, (p/'py.typed').exists(), (p/'data'/'genre_graph.json').exists())"])
    parts = out.split()
    check(checks, "imports", rc == 0, out[-300:])
    check(checks, "ships py.typed", parts[1:2] == ["True"])
    check(checks, "ships the genre graph", parts[2:3] == ["True"])
    rc, out, _ = sh([str(venv / "bin" / "postradio"), "--help"])
    check(checks, "postradio CLI runs", rc == 0 and "flow" in out)
    rc, out, _ = sh([str(venv / "bin" / "pip"), "list", "--format=json"])
    pkgs = len(json.loads(out)) if rc == 0 else None
    size = sum(f.stat().st_size for f in venv.rglob("*") if f.is_file()) // 1_000_000
    return {"ok": all(c["ok"] for c in checks), "seconds": round(sum(steps.values()), 1), "steps": steps,
            "version": parts[0] if parts else None, "packages": pkgs, "venv_mb": size, "source": args.source,
            "checks": checks, "venv": str(venv)}


def part_data(run: Run, args, venv: Path, home: Path) -> dict:
    cli = [str(venv / "bin" / "postradio"), "--json"]
    env = {**os.environ, "POSTRADIO_HOME": str(home)}
    checks, steps = [], {}
    rc, out, steps["check"] = sh(cli + ["data", "fetch", "--dry"], env=env, timeout=300)
    check(checks, "dry check answers", rc == 0, out[-300:])
    try:
        dry = json.loads(out[out.index("{"):])
    except ValueError:
        dry = {"raw": out[-500:]}
    rc, out, steps["fetch"] = sh(cli + ["data", "fetch"], env=env, timeout=3600)
    check(checks, "fetch succeeds", rc == 0, out[-500:])
    rc, out, steps["status"] = sh(cli + ["data", "status"], env=env, timeout=60)
    try:
        status = json.loads(out[out.index("{"):])
    except ValueError:
        status = {"raw": out[-500:]}
    files = status.get("files") or {}
    check(checks, "store installed", (files.get("mb_artists.sqlite") or 0) > 100_000_000, files)
    check(checks, "release recorded", bool((status.get("release") or {}).get("tag")), status.get("release"))
    rc, out, _ = sh([str(venv / "bin" / "python"), "-c",
                     "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
                     "print(c.execute('select count(*) from artists').fetchone()[0])",
                     str(home / "data" / "mb_artists.sqlite")])
    artists = int(out.strip()) if rc == 0 and out.strip().isdigit() else None
    check(checks, "store has millions of artists", (artists or 0) > 1_000_000, artists)
    return {"ok": all(c["ok"] for c in checks), "seconds": round(sum(steps.values()), 1), "steps": steps,
            "dry": {k: dry.get(k) for k in ("tag", "installed", "files", "download_bytes", "state") if k in dry},
            "release": status.get("release"), "files": files, "store_artists": artists, "checks": checks}


def run_case(run: Run, args, venv: Path, home: Path, name: str, kind: str, cargs: dict, opts: dict,
             liked: set) -> dict:
    spec = {"home": str(home), "kind": kind, "args": cargs, "yandex_url": YANDEX_URL,
            "yandex_export": str(Path(args.yandex_export).resolve()) if args.yandex_export else None,
            "repeat": opts.get("repeat", False), "dry_first": opts.get("dry_first", False)}
    spec_p, out_p = run.root / "results" / f"{name}.spec.json", run.root / "results" / f"{name}.raw.json"
    spec_p.write_text(json.dumps(spec, indent=1))
    t = time.monotonic()
    try:
        p = subprocess.run([str(venv / "bin" / "python"), str(CASE), str(spec_p), str(out_p)],
                           capture_output=True, text=True, timeout=args.case_timeout)
        tail = (p.stdout + p.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        tail = f"killed after {args.case_timeout} s"
    wall = round(time.monotonic() - t, 1)
    raw = json.loads(out_p.read_text()) if out_p.exists() else {}
    runs = {r["label"]: r for r in raw.get("runs") or []}
    main = runs.get("main")
    checks: list[dict] = []
    data: dict = {"http_calls": main and main.get("http_calls"), "http_failures": main and main.get("http_failures"),
                  "stages": stage_times(raw.get("events") or [])}
    if raw.get("crash") or not main:
        check(checks, "case completed", False, raw.get("crash") or tail)
    elif opts.get("expect_error"):
        got = (main.get("error") or {}).get("type")
        check(checks, f"fails with {opts['expect_error']}", got == opts["expect_error"], main.get("error"))
        data["error"] = main.get("error")
    elif not main["ok"]:
        check(checks, "call succeeded", False, main.get("error"))
    else:
        c, d = playlist_checks(main["result"], cargs, liked, kind)
        checks += c
        data.update(d)
        if "repeat" in runs:
            a = [(x["artist"], x["title"]) for x in main["result"]["tracks"]]
            b = [(x["artist"], x["title"]) for x in runs["repeat"]["result"]["tracks"]]
            data["repeat_same"] = a == b
            data["repeat_overlap"] = round(len(set(a) & set(b)) / max(1, len(set(a))), 2)
            data["repeat_seconds"] = runs["repeat"]["seconds"]
        if "dry" in runs and runs["dry"]["ok"]:
            dres = runs["dry"]["result"]
            data["dry_seconds"] = runs["dry"]["seconds"]
            if kind == "flow":
                same_seed = dres.get("rng_seed") == main["result"].get("rng_seed")
                data["dry_matches"] = (not same_seed) or dres.get("genre_path") == main["result"].get("genre_path")
                data["dry_path"] = dres.get("genre_path")
            else:
                data["dry_matches"] = dres.get("seed", {}).get("artist") == main["result"]["seed"]["artist"]
                data["dry_graph_size"] = dres.get("graph_size")
    seconds = main["seconds"] if main else None
    return {"name": name, "kind": kind, "args": cargs, "ok": all(c["ok"] for c in checks), "seconds": seconds,
            "wall_seconds": wall, "time_grade": "-" if opts.get("expect_error") else time_grade(seconds),
            "sanity_grade": sanity_grade(checks), "checks": checks, "data": data,
            "sample": [f"{t['artist']} - {t['title']}" + (f" [{t.get('stop') or t.get('hop')}]")
                       for t in ((main or {}).get("result") or {}).get("tracks", [])[:12]]}


def stage_times(events: list[dict]) -> dict:
    """Seconds spent per progress stage (first event of a stage to first event of the next)."""
    marks, out = [(e["t"], e["stage"]) for e in events if "stage" in e], {}
    seen: list[tuple[float, str]] = []
    for t, s in marks:
        if not seen or seen[-1][1] != s:
            seen.append((t, s))
    if not seen:
        return out
    for (t, s), (t2, _) in zip(seen, seen[1:] + [(seen[-1][0], "")], strict=True):
        out[s] = round(out.get(s, 0) + t2 - t, 1)
    return out


def write_summary(run: Run) -> None:
    rows, results = [], {}
    for p in sorted((run.root / "results").glob("*.json")):
        if p.name.endswith((".spec.json", ".raw.json")):
            continue
        r = json.loads(p.read_text())
        results[p.stem] = r
    lines = [f"# Post-Radio e2e run {run.root.name}", "", f"Updated {now()}, attempt {run.attempt}.", ""]
    for part in ("install", "data"):
        r = results.get(part)
        if r:
            lines.append(f"**{part}**: {'ok' if r['ok'] else 'FAILED'} in {r.get('seconds')} s; "
                         + ", ".join(f"{c['check']} {'✓' if c['ok'] else '✗ ' + c['detail']}" for c in r["checks"]))
    lines += ["", "| case | time (s) | time | sanity | tracks | artists | familiar | mbid | notes |",
              "|---|---|---|---|---|---|---|---|---|"]
    for name, r in results.items():
        if name in ("install", "data", "compare"):
            continue
        d = r.get("data") or {}
        bad = "; ".join(f"{c['check']}: {c['detail']}" for c in r["checks"] if not c["ok"])
        extra = " → ".join(d.get("genre_path") or []) if r["kind"] == "flow" else f"hop {d.get('mean_hop')}"
        rows.append(f"| {name} | {r.get('seconds')} | {r['time_grade']} | {r['sanity_grade']} | {d.get('tracks', '')} "
                    f"| {d.get('artists', '')} | {d.get('familiar_share', '')} | {d.get('artist_mbid_share', '')} "
                    f"| {extra}{' — ✗ ' + bad if bad else ''} |")
    lines += rows
    comp = compare_checks(results)
    if comp:
        lines += ["", "**Across cases**", ""] + [f"- {'✓' if c['ok'] else '✗'} {c['check']} ({c['detail']})"
                                                 for c in comp]
    (run.root / "summary.md").write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", help="an existing run folder: finished cases are kept, the rest run")
    ap.add_argument("--yandex-export", help="playlist JSON exported locally (Yandex blocks some regions)")
    ap.add_argument("--source", default="git+https://github.com/Goober-nation/Post-Radio@flow-integration",
                    help="what to pip install (a git URL or a local path)")
    ap.add_argument("--venv", help="reuse this venv instead of a fresh install")
    ap.add_argument("--fresh-data", action="store_true", help="data release into the run folder, not the shared one")
    ap.add_argument("--only", default="install,data,flows,radios")
    ap.add_argument("--cases", help="comma-separated case names (default: all)")
    ap.add_argument("--case-timeout", type=int, default=900)
    args = ap.parse_args()
    if not os.environ.get("LISTENBRAINZ_TOKEN"):
        print("note: no $LISTENBRAINZ_TOKEN: flows will be much slower", file=sys.stderr)
    base = REPO / "e2e-runs"
    root = Path(args.resume) if args.resume else base / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run = Run(root)
    if args.resume:
        prev = json.loads((root / "args.json").read_text())
        for k, v in prev.items():
            if getattr(args, k, None) in (None, False) and k not in ("resume",):
                setattr(args, k, v)
    (root / "args.json").write_text(json.dumps({k: v for k, v in vars(args).items() if k != "resume"}, indent=1))
    home = root / "home" if args.fresh_data else base / "home"
    parts = args.only.split(",")
    liked = set()
    if args.yandex_export:
        for t in json.load(open(args.yandex_export))["tracks"]:
            for a, _ in t["artists"]:
                liked.add((norm(a), norm(t["title"])))
    run.log(event="run start", root=str(root), parts=parts, yandex_export=bool(args.yandex_export))

    def part(name: str, fn):
        got = run.done(name)
        if got:
            run.log(event="skip (done earlier)", case=name)
            return got
        run.status(part=name)
        run.log(event="start", case=name)
        res = fn()
        run.save(name, res)
        write_summary(run)
        run.log(event="done", case=name, ok=res["ok"], seconds=res.get("seconds"),
                failed=[c["check"] for c in res.get("checks", []) if not c["ok"]])
        return res

    venv = Path(args.venv) if args.venv else root / "venv"
    if "install" in parts:
        inst = part("install", lambda: part_install(run, args))
        venv = Path(inst.get("venv") or inst.get("reused") or venv)
        if not inst["ok"]:
            run.log(event="stop", reason="install failed")
            return
    if "data" in parts:
        part("data", lambda: part_data(run, args, venv, home))
    wanted = set(args.cases.split(",")) if args.cases else None
    for group, kind, cases in (("flows", "flow", FLOWS), ("radios", "radio", RADIOS)):
        if group not in parts:
            continue
        for i, (name, cargs, opts) in enumerate(cases, 1):
            if wanted and name not in wanted:
                continue
            got = run.done(name)
            if got:
                run.log(event="skip (done earlier)", case=name)
                continue
            run.status(part=group, case=name, index=i, of=len(cases))
            run.log(event="start", case=name, args=cargs)
            res = run_case(run, args, venv, home, name, kind, cargs, opts, liked)
            run.save(name, res)
            write_summary(run)
            run.log(event="done", case=name, ok=res["ok"], seconds=res["seconds"], time=res["time_grade"],
                    sanity=res["sanity_grade"], failed=[c["check"] for c in res["checks"] if not c["ok"]])
        run.log(event="part done", part=group)
    write_summary(run)
    run.status(part="finished")
    run.log(event="run finished", summary=str(root / "summary.md"))


if __name__ == "__main__":
    main()
