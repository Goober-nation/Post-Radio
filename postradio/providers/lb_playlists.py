"""ListenBrainz 'created for you' playlists (Weekly Exploration = LB's deep cuts, Weekly Jams).
Public profiles need no token. LB keeps the two most recent weeks of each."""
from dataclasses import dataclass

from ..http import Http
from ..models import Artist, Track

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
        d = self.http.get(f"{API}/user/{user}/playlists/createdfor", params={"count": 100}, headers=self.headers,
                          max_age=3600)
        out = []
        for item in (d or {}).get("playlists") or []:
            pl = item["playlist"]
            meta = (pl.get("extension", {}).get(JSPF_PLAYLIST, {}).get("additional_metadata") or {})
            if (meta.get("algorithm_metadata") or {}).get("source_patch") != kind:
                continue
            mbid = pl["identifier"].rstrip("/").rsplit("/", 1)[-1]
            out.append(CreatedPlaylist(kind, pl["date"][:10], pl["title"], mbid, self._tracks(mbid)))
        return sorted(out, key=lambda p: p.week, reverse=True)

    def _tracks(self, mbid: str) -> list[Track]:
        d = self.http.get(f"{API}/playlist/{mbid}", headers=self.headers)  # a playlist's body never changes
        out = []
        for t in ((d or {}).get("playlist") or {}).get("track") or []:
            ext = (t.get("extension") or {}).get(JSPF_TRACK, {})
            arts = (ext.get("additional_metadata") or {}).get("artists") or []
            rec = (t.get("identifier") or [""])
            rec = rec[0] if isinstance(rec, list) else rec
            artist = Artist(t.get("creator") or "", mbid=arts[0]["artist_mbid"] if arts else None)
            out.append(Track(t.get("title") or "", artist, recording_mbid=rec.rstrip("/").rsplit("/", 1)[-1] or None,
                             sources={"lb-created-for"}))
        return out
