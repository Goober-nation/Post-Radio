import argparse
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from . import config as config_mod
from .api import Recommender
from .brand import CONFIG_ENV, NAME
from .providers import NoHistory
from .providers.history import FileHistory


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def _parse_mix(text: str | None) -> dict[str, float] | None:
    if not text:
        return None
    out = {}
    for part in text.split(","):
        k, sep, v = part.partition("=")
        if not sep:
            raise SystemExit(f"{NAME}: error: --mix expects STREAM=X, got '{part}'")
        try:
            out[k.strip()] = float(v)
        except ValueError:
            raise SystemExit(f"{NAME}: error: --mix {k.strip()}: '{v}' is not a number (expected 0..1)") from None
    return out


def _unit(v: str) -> float:
    f = float(v)
    if not 0 <= f <= 1:
        raise argparse.ArgumentTypeError("must be in [0, 1]")
    return f


class _Sub:
    """add_parser that attaches the shared global flags to every subcommand."""

    def __init__(self, subparsers, common):
        self.subparsers, self.common = subparsers, common

    def add_parser(self, *args, **kw):
        return self.subparsers.add_parser(*args, parents=[self.common], **kw)


def parser() -> argparse.ArgumentParser:
    # global flags work before or after the subcommand: `--json radio ...` == `radio ... --json`
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=argparse.SUPPRESS,
                        help=f"YAML config (default: ${CONFIG_ENV} or ./config.yaml)")
    common.add_argument("--no-token", action="store_true", default=argparse.SUPPRESS,
                        help="ignore $LISTENBRAINZ_TOKEN (fully tokenless)")
    common.add_argument("--offline", action="store_true", default=argparse.SUPPRESS, help="HTTP cache only")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="print full JSON")
    ap = argparse.ArgumentParser(prog=NAME, description="Tunable exploration radio + LB created-for playlists",
                                 parents=[common])
    sub = _Sub(ap.add_subparsers(dest="cmd", required=True), common)

    r = sub.add_parser("radio", help="generate an exploration radio playlist")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--artist")
    g.add_argument("--track", help="'Artist - Title'")
    g.add_argument("--artist-mbid")
    g.add_argument("--recording-mbid")
    g.add_argument("--genre", help="genre seed; comma-separate several (e.g. 'shoegaze,dream pop')")
    r.add_argument("--genre-op", choices=["or", "and"], default="or", help="several genres: any (or) / all (and)")
    r.add_argument("--unheard", choices=["none", "tracks", "artists"],
                   help="tracks: drop played tracks; artists: also drop known artists; none: off (overrides config)")
    r.add_argument("--mix", metavar="STREAM=X,...",
                   help="per-run stream multipliers 0..1, e.g. 'deezer-top=0.2,troi=1' (streams: deezer-top, lb, "
                        "deezer-radio, troi, track-sim, lb-tags)")
    r.add_argument("-e", "--explore", type=_unit, help="0 = close, 1 = ~3 hops out")
    r.add_argument("-p", "--popularity", type=_unit, help="0 = deep cuts/small artists, 1 = hits")
    r.add_argument("--auto-popularity", action="store_true", help="ignore configured popularity; follow explore")
    r.add_argument("-n", type=int)
    r.add_argument("--per-artist", type=int)
    r.add_argument("--rng-seed", type=int)
    h = r.add_mutually_exclusive_group()
    h.add_argument("--lb-user", help="history from this LB user")
    h.add_argument("--history-file", help="history from a .jsonl/.csv plays file")
    h.add_argument("--no-history", action="store_true")
    r.add_argument("--yandex", metavar="URL", help="public Yandex Music playlist (e.g. your likes) as history + taste")
    r.add_argument("--yandex-tracks", type=int, metavar="N", help="only the newest N playlist tracks (default: all)")
    r.add_argument("--no-feedback", action="store_true", help="ignore loves/hates/ratings")
    r.add_argument("--navidrome-url", help="read stars/ratings from this Navidrome (creds from env)")
    r.add_argument("--save", nargs="?", const="", metavar="NAME", help="save the result (default name if omitted)")

    d = sub.add_parser("deep-cuts", help="LB Weekly Exploration (or Jams), both weeks")
    d.add_argument("--user", help="LB user (default: config / $LISTENBRAINZ_USERNAME)")
    d.add_argument("--kind", choices=["exploration", "jams"], default="exploration")
    d.add_argument("--save", nargs="?", const="", metavar="NAME")

    fb = sub.add_parser("feedback", help="your own loves/hates store")
    fbs = fb.add_subparsers(dest="fb_cmd", required=True)
    fbi = fbs.add_parser("import", help="append a JSON batch of loves/hates (dated; newest wins)")
    fbi.add_argument("file", help="JSON file, or - for stdin")
    fbs.add_parser("batches", help="list imported batches with dates")

    s = sub.add_parser("saved", help="list saved playlists, or print one")
    s.add_argument("name", nargs="?")

    dt = sub.add_parser("data", help="optional data in storage.data_dir: artist store, genre weights")
    dts = dt.add_subparsers(dest="data_cmd", required=True)
    db = dts.add_parser("build", help="build the data files and package a data-YYYY-MM release folder")
    db.add_argument("--out", default="dist", help="release folder parent (default: dist)")
    db.add_argument("--skip", default="", help="comma list of steps to skip: graph, dump, weights")
    db.add_argument("--keep-dump", action="store_true", help="keep the downloaded artist.tar.xz")
    db.add_argument("--seeds", type=int, default=1000, help="LB sitewide top artists for the weights (1000)")
    db.add_argument("--similar", type=int, default=10, help="similar artists per seed (10)")
    df = dts.add_parser("fetch", help="install the newest data release (verified against its manifest)")
    df.add_argument("--tag", help="a specific data-YYYY-MM release")
    df.add_argument("--no-store", action="store_true", help="skip the ~600 MB artist store; genre files only")
    dts.add_parser("status", help="show what is installed")

    sv = sub.add_parser("serve", help="run the HTTP API")
    sv.add_argument("--host")
    sv.add_argument("--port", type=int)
    return ap


def main(argv=None):
    a = parser().parse_args(argv)
    for flag, default in (("config", None), ("no_token", False), ("offline", False), ("json", False)):
        if not hasattr(a, flag):
            setattr(a, flag, default)
    load_env(config_mod.ROOT / ".env")
    cfg = config_mod.load(a.config)
    log = (lambda *_: None) if a.json else (lambda m: print(f"# {m}", file=sys.stderr))
    token = "" if a.no_token else None  # "" disables, None = env

    history = None
    if a.cmd == "radio":
        if a.no_history:
            history = NoHistory()
        elif a.history_file:
            history = FileHistory(a.history_file)
        elif a.lb_user:
            cfg = replace(cfg, sources=replace(cfg.sources, history="lb", lb_user=a.lb_user))
        if a.yandex:
            hist = "yandex" if not (a.no_history or a.history_file or a.lb_user) else cfg.sources.history
            cfg = replace(cfg, sources=replace(cfg.sources, yandex_playlist=a.yandex, history=hist,
                                               yandex_tracks=a.yandex_tracks or cfg.sources.yandex_tracks))
            fb = list(cfg.feedback.sources)
            if "yandex" not in fb:  # the flag means "use my playlist", even with an older config's source list
                fb.insert(1 if fb[:1] == ["json"] else 0, "yandex")
                cfg = replace(cfg, feedback=replace(cfg.feedback, sources=fb))
        if a.no_feedback:
            cfg = replace(cfg, feedback=replace(cfg.feedback, enabled=False))
        if a.navidrome_url:
            cfg = replace(cfg, feedback=replace(cfg.feedback, navidrome_url=a.navidrome_url))
    rec = Recommender(cfg, token=token, history=history, offline=a.offline, log=log)

    if a.cmd == "serve":
        import uvicorn

        from .server import create_app
        uvicorn.run(create_app(rec), host=a.host or cfg.server.host, port=a.port or cfg.server.port)
        return
    if a.cmd == "feedback":
        if a.fb_cmd == "import":
            raw = sys.stdin.read() if a.file == "-" else Path(a.file).read_text()
            out = rec.import_feedback(json.loads(raw))
        else:
            out = rec.feedback_batches()
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    if a.cmd == "data":
        progress = lambda m: print(f"# {m}", file=sys.stderr, flush=True)  # noqa: E731 - builds are long
        rec.log = progress
        if a.data_cmd == "build":
            steps = [s.strip() for s in a.skip.split(",") if s.strip()]
            out = {"release_dir": str(rec.data_build(a.out, steps, a.keep_dump, a.seeds, a.similar))}
        elif a.data_cmd == "fetch":
            out = rec.data_fetch(a.tag, store=not a.no_store)
        else:
            out = rec.data_status()
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return
    if a.cmd == "saved":
        out = rec.load_saved(a.name) if a.name else rec.list_saved()
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    if a.cmd == "radio":
        genres = [x.strip() for x in a.genre.split(",") if x.strip()] if a.genre else None
        res = rec.generate(artist=a.artist, track=a.track, artist_mbid=a.artist_mbid, recording_mbid=a.recording_mbid,
                           genres=genres, genre_op=a.genre_op, unheard=a.unheard, explore=a.explore,
                           popularity=a.popularity, n=a.n, per_artist=a.per_artist, rng_seed=a.rng_seed,
                           auto_popularity=a.auto_popularity, mix=_parse_mix(a.mix))
    else:
        res = rec.deep_cuts(a.user, a.kind)

    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif a.cmd == "radio":
        log(f"{len(res['tracks'])} tracks from {res['candidates']} candidates, "
            f"{rec.http.calls} network calls, {rec.http.failures} failed")
        for i, t in enumerate(res["tracks"], 1):
            mark = "*" if t["known_artist"] else " "
            print(f"{i:>3} h{t['hop']} p{t['popularity']:.2f}{mark} {t['artist']} — {t['title']}  "
                  f"[{','.join(t['sources'])}]")
    else:
        for w in res["weeks"]:
            print(f"== {w['title']} ({len(w['tracks'])} tracks) {w['url']}")
            for i, t in enumerate(w["tracks"], 1):
                print(f"{i:>3} {t['artist']} — {t['title']}")
    if a.save is not None:
        log(f"saved: {rec.save(res['result_id'], a.save or None)}")


def run(argv=None):
    """Entry point: user errors (bad seed, unknown stream, out-of-range value) print one line, no traceback."""
    try:
        main(argv)
    except (ValueError, FileNotFoundError, FileExistsError, KeyError) as e:
        raise SystemExit(f"{NAME}: error: {e}") from None


if __name__ == "__main__":
    run()
