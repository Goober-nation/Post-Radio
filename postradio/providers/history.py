"""Listening-history suppliers. All produce a models.History; the engine never knows which one it got."""
import csv
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from ..http import Http
from ..models import History, Play
from . import HistoryProvider, parse_rows, seg, time_bucket

LB_API = "https://api.listenbrainz.org/1"


class ListenBrainzHistory(HistoryProvider):
    """All-time stats for a user. Public profiles work without a token."""

    def __init__(self, http: Http, user: str, token: str | None = None, max_artists: int = 1000,
                 max_tracks: int = 5000, refresh_s: int = 86400):
        self.http, self.user = http, user
        self.headers = {"Authorization": f"Token {token}"} if token else None
        self.max_artists, self.max_tracks, self.refresh_s = max_artists, max_tracks, refresh_s

    def version(self) -> object:
        return time_bucket(self.refresh_s)

    def _pages(self, kind: str, cap: int):
        """First page tells us the total; the rest are fetched in parallel (Http enforces LB's rate)."""
        def page(off):
            d = self.http.get(f"{LB_API}/stats/user/{seg(self.user)}/{kind}",
                              params={"count": 100, "offset": off, "range": "all_time"}, headers=self.headers)
            return ((d or {}).get("payload") or {})

        first = page(0)
        rows = list(first.get(kind) or [])
        total = min(cap, first.get(f"total_{kind[:-1]}_count") or len(rows))
        offsets = list(range(100, total, 100)) if len(rows) == 100 else []
        with ThreadPoolExecutor(max_workers=4) as pool:
            for p in pool.map(page, offsets):
                rows += p.get(kind) or []
        return rows

    def load(self) -> History:
        artists = []
        for rows in parse_rows(self._pages("artists", self.max_artists), lambda a: [
                (a["artist_name"], m, int(a.get("listen_count") or 0))
                for m in (a.get("artist_mbids") or ([a["artist_mbid"]] if a.get("artist_mbid") else [None]))]
                if a["artist_name"] else None):
            artists += rows
        tracks = parse_rows(self._pages("recordings", self.max_tracks), lambda r: (r["artist_name"], r["track_name"])
                            if r["artist_name"] and r["track_name"] else None)
        return History.from_counts(artists, tracks)


class FileHistory(HistoryProvider):
    """Plays from a JSONL or CSV file, e.g. exported from multi-scrobbler / Maloja / Last.fm.
    Fields: artist, title (or track), optional timestamp, artist_mbid, recording_mbid."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def version(self) -> object:
        st = self.path.stat() if self.path.exists() else None
        return (st.st_size, st.st_mtime_ns) if st else None

    def load(self) -> History:
        return History.from_plays(self._plays())

    def _plays(self):
        with self.path.open(newline="", encoding="utf-8") as f:
            if self.path.suffix == ".jsonl":
                rows = (json.loads(line) for line in f if line.strip())
            else:
                rows = csv.DictReader(f)
            for r in rows:
                title = r.get("title") or r.get("track")
                if r.get("artist") and title:
                    yield Play(r["artist"], title, _timestamp(r.get("timestamp")), r.get("artist_mbid") or None,
                               r.get("recording_mbid") or None)


def _timestamp(value) -> int | None:
    """Unix seconds from an int/float/numeric string or an ISO-8601 date ('2026-10-20T12:00:00Z'); else None.
    Exports from Maloja, Last.fm, multi-scrobbler disagree on the format, and a bad one must not sink the file."""
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        pass
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp())
