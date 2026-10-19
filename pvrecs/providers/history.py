"""Listening-history suppliers. All produce a models.History; the engine never knows which one it got."""
import csv
import json
from pathlib import Path

from ..http import Http
from ..models import History, Play
from . import HistoryProvider

LB_API = "https://api.listenbrainz.org/1"


class ListenBrainzHistory(HistoryProvider):
    """All-time stats for a user. Public profiles work without a token."""

    def __init__(self, http: Http, user: str, token: str | None = None, max_artists: int = 1000,
                 max_tracks: int = 5000):
        self.http, self.user = http, user
        self.headers = {"Authorization": f"Token {token}"} if token else None
        self.max_artists, self.max_tracks = max_artists, max_tracks

    def _pages(self, kind: str, cap: int):
        for off in range(0, cap, 100):
            d = self.http.get(f"{LB_API}/stats/user/{self.user}/{kind}",
                              params={"count": 100, "offset": off, "range": "all_time"}, headers=self.headers)
            rows = ((d or {}).get("payload") or {}).get(kind) or []
            yield from rows
            if len(rows) < 100:
                return

    def load(self) -> History:
        artists = []
        for a in self._pages("artists", self.max_artists):
            mbids = a.get("artist_mbids") or ([a["artist_mbid"]] if a.get("artist_mbid") else [None])
            artists += [(a["artist_name"], m, a["listen_count"]) for m in mbids]
        tracks = [(r["artist_name"], r["track_name"]) for r in self._pages("recordings", self.max_tracks)]
        return History.from_counts(artists, tracks)


class FileHistory(HistoryProvider):
    """Plays from a JSONL or CSV file, e.g. exported from multi-scrobbler / Maloja / Last.fm.
    Fields: artist, title (or track), optional timestamp, artist_mbid, recording_mbid."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

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
                    ts = r.get("timestamp")
                    yield Play(r["artist"], title, int(ts) if ts else None, r.get("artist_mbid") or None,
                               r.get("recording_mbid") or None)
