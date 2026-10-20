"""ListenBrainz 'created for you' playlists (Weekly Exploration = LB's deep cuts, Weekly Jams).
Public profiles need no token. LB keeps the two most recent weeks of each."""
from dataclasses import dataclass

from ..http import Http
from ..models import Artist, Track
from . import parse_rows, seg

API = "https://api.listenbrainz.org/1"
JSPF_PLAYLIST = "https://musicbrainz.org/doc/jspf#playlist"
JSPF_TRACK = "https://musicbrainz.org/doc/jspf#track"
KINDS = {"exploration": "weekly-exploration", "jams": "weekly-jams"}


@dataclass
class CreatedPlaylist:
    kind: str  # weekly-exploration | weekly-jams
    week: str  # ISO date the playlist was generated
    title: str
    mbid: str
    tracks: list[Track]


class LBCreatedFor:
    def __init__(self, http: Http, token: str | None = None):
        self.http = http
        self.headers = {"Authorization": f"Token {token}"} if token else None

    def playlists(self, user: str, kind: str = "weekly-exploration") -> list[CreatedPlaylist]:
        """Newest first. The listing changes weekly, so it bypasses the long HTTP cache."""
        d = self.http.get(f"{API}/user/{seg(user)}/playlists/createdfor", params={"count": 100}, headers=self.headers,
                          max_age=3600)
        def convert(item):
            pl = item["playlist"]
            meta = ((pl.get("extension") or {}).get(JSPF_PLAYLIST) or {}).get("additional_metadata") or {}
            if (meta.get("algorithm_metadata") or {}).get("source_patch") != kind:
                return None
            mbid = _last_segment(pl["identifier"])
            return (kind, pl["date"][:10], pl.get("title") or "", mbid) if mbid else None

        found = parse_rows((d or {}).get("playlists"), convert)
        out = [CreatedPlaylist(k, week, title, mbid, self._tracks(mbid)) for k, week, title, mbid in found]
        return sorted(out, key=lambda p: p.week, reverse=True)

    def _tracks(self, mbid: str) -> list[Track]:
        d = self.http.get(f"{API}/playlist/{seg(mbid)}", headers=self.headers)  # a playlist's body never changes
        def convert(t):
            if not (t.get("title") and t.get("creator")):
                return None
            ext = (t.get("extension") or {}).get(JSPF_TRACK) or {}
            arts = (ext.get("additional_metadata") or {}).get("artists") or []
            rec = t.get("identifier") or [""]
            rec = rec[0] if isinstance(rec, list) and rec else rec
            artist = Artist(t["creator"], mbid=(arts[0] or {}).get("artist_mbid") if arts else None)
            return Track(t["title"], artist, recording_mbid=_last_segment(rec) if isinstance(rec, str) else None,
                         sources={"lb-created-for"})

        return parse_rows(((d or {}).get("playlist") or {}).get("track"), convert)


def _last_segment(url: str) -> str | None:
    return url.rstrip("/").rsplit("/", 1)[-1] or None
