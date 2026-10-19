import argparse
import json
import os
import sys
from pathlib import Path

from .engine import Params, Radio
from .http import Http
from .sources import Deezer, ListenBrainz, MusicBrainz


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pvrecs", description="Tunable exploration radio")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--artist", help="seed artist name")
    g.add_argument("--track", help="seed track as 'Artist - Title'")
    g.add_argument("--artist-mbid")
    g.add_argument("--recording-mbid")
    ap.add_argument("-e", "--explore", type=float, default=0.5, help="0 = close & popular, 1 = far & deep cuts")
    ap.add_argument("-p", "--popularity", type=float,
                    help="0 = deep cuts by obscure artists, 1 = hits; default follows --explore")
    ap.add_argument("-n", type=int, default=50)
    ap.add_argument("--per-artist", type=int, default=2)
    ap.add_argument("--rng-seed", type=int)
    ap.add_argument("--json", action="store_true", help="print full JSON instead of a table")
    ap.add_argument("--offline", action="store_true", help="cache only, no network")
    a = ap.parse_args(argv)
    if not 0 <= a.explore <= 1:
        ap.error("--explore must be in [0, 1]")
    if a.popularity is not None and not 0 <= a.popularity <= 1:
        ap.error("--popularity must be in [0, 1]")

    load_env(Path(__file__).resolve().parent.parent / ".env")
    http = Http(offline=a.offline)
    log = (lambda *_: None) if a.json else (lambda m: print(f"# {m}", file=sys.stderr))
    radio = Radio(ListenBrainz(http, os.environ.get("LISTENBRAINZ_TOKEN"), os.environ.get("LISTENBRAINZ_USERNAME")),
                  MusicBrainz(http), Deezer(http), log=log)
    res = radio.generate(Params(a.explore, a.n, a.per_artist, a.rng_seed, a.popularity), artist=a.artist, track=a.track,
                         artist_mbid=a.artist_mbid, recording_mbid=a.recording_mbid)
    if a.json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
        return
    log(f"{len(res['tracks'])} tracks from {res['candidates']} candidates, {http.calls} network calls")
    for i, t in enumerate(res["tracks"], 1):
        mark = "*" if t["known_artist"] else " "
        srcs = ",".join(t["sources"])
        print(f"{i:>3} h{t['hop']} p{t['popularity']:.2f}{mark} {t['artist']} — {t['title']}  [{srcs}]")


if __name__ == "__main__":
    main()
