"""Clients for ListenBrainz (via troi where it has a service), MusicBrainz and Deezer."""
from dataclasses import dataclass, field

from troi.recording_search_service import RecordingSearchByArtistService

from .http import Http

LB = "https://api.listenbrainz.org/1"
LB_LABS = "https://labs.api.listenbrainz.org"
MB = "https://musicbrainz.org/ws/2"
DZ = "https://api.deezer.com"
SIMILAR_ALGO = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
# MB artist-artist relation types that imply musical proximity
MB_REL_TYPES = {"member of band", "collaboration", "is person", "supporting musician", "founder", "subgroup"}


@dataclass
class Track:
    artist: str
    title: str
    artist_mbid: str | None = None
    recording_mbid: str | None = None
    deezer_id: int | None = None
    popularity: float = 0.5  # 0 = obscure, 1 = hit, relative within its source
    sources: set = field(default_factory=set)


def norm(s: str) -> str:
    return "".join(ch for ch in s.casefold() if ch.isalnum())


class ListenBrainz:
    def __init__(self, http: Http, token: str | None, user: str | None):
        self.http, self.user = http, user
        self.headers = {"Authorization": f"Token {token}"} if token else None
        self.troi_search = RecordingSearchByArtistService(auth_token=token)

    def similar_artists(self, mbid: str, limit: int = 25) -> list[tuple[str, str, float]]:
        data = self.http.post(f"{LB_LABS}/similar-artists/json", [{"artist_mbids": [mbid], "algorithm": SIMILAR_ALGO}])
        out = [(a["artist_mbid"], a["name"], float(a["score"])) for a in (data or []) if a.get("artist_mbid") != mbid]
        top = max((s for *_, s in out), default=1.0)
        return [(m, n, s / top) for m, n, s in out[:limit]]

    def top_recordings(self, mbid: str) -> list[dict]:
        """Artist's recordings ordered by total LB listens (most popular first)."""
        data = self.http.get(f"{LB}/popularity/top-recordings-for-artist/{mbid}", headers=self.headers,
                             shape=_trim_top)
        return data if isinstance(data, list) else []

    def radio_artist(self, mbid: str, mode: str, pop_begin: int, pop_end: int, n: int) -> dict:
        """troi's LB-radio artist search (seed + similar artists, popularity window)."""
        try:
            recs, _ = self.troi_search.search(mode, mbid, pop_begin, pop_end, n, 6)
        except RuntimeError:
            return {}
        return recs

    def recording_metadata(self, mbids: list[str]) -> dict:
        out = {}
        for i in range(0, len(mbids), 100):
            chunk = mbids[i : i + 100]
            data = self.http.post(f"{LB}/metadata/recording/", {"recording_mbids": chunk, "inc": "artist"},
                                  headers=self.headers)
            out.update(data or {})
        return out

    def history(self, max_artists: int = 1000) -> tuple[dict[str, int], set[str]]:
        """(artist_mbid or "n:"+normalised name -> listen count, set of normalised 'artist|title' keys heard)."""
        artists: dict[str, int] = {}
        heard: set[str] = set()
        if not self.user:
            return artists, heard
        for off in range(0, max_artists, 100):
            d = self.http.get(f"{LB}/stats/user/{self.user}/artists",
                              params={"count": 100, "offset": off, "range": "all_time"}, headers=self.headers)
            rows = ((d or {}).get("payload") or {}).get("artists") or []
            for a in rows:
                artists["n:" + norm(a["artist_name"])] = a["listen_count"]
                for m in a.get("artist_mbids") or ([a["artist_mbid"]] if a.get("artist_mbid") else []):
                    artists[m] = a["listen_count"]
            if len(rows) < 100:
                break
        for off in range(0, 5000, 100):
            d = self.http.get(f"{LB}/stats/user/{self.user}/recordings",
                              params={"count": 100, "offset": off, "range": "all_time"}, headers=self.headers)
            rows = ((d or {}).get("payload") or {}).get("recordings") or []
            for r in rows:
                heard.add(norm(r["artist_name"]) + "|" + norm(r["track_name"]))
            if len(rows) < 100:
                break
        return artists, heard

    def top_artists(self, n: int) -> list[tuple[str, str]]:
        d = self.http.get(f"{LB}/stats/user/{self.user}/artists", params={"count": n, "range": "all_time"},
                          headers=self.headers)
        rows = ((d or {}).get("payload") or {}).get("artists") or []
        return [(a["artist_mbid"], a["artist_name"]) for a in rows if a.get("artist_mbid")]


class MusicBrainz:
    def __init__(self, http: Http):
        self.http = http

    def find_artist(self, name: str) -> tuple[str, str] | None:
        d = self.http.get(f"{MB}/artist", params={"query": name, "fmt": "json", "limit": 5})
        arts = (d or {}).get("artists") or []
        exact = [a for a in arts if norm(a["name"]) == norm(name)]
        a = (exact or arts or [None])[0]
        return (a["id"], a["name"]) if a else None

    def artist_name(self, mbid: str) -> str | None:
        d = self.http.get(f"{MB}/artist/{mbid}", params={"fmt": "json"})
        return (d or {}).get("name")

    def find_recording(self, artist: str, title: str) -> dict | None:
        q = f'recording:"{title}" AND artist:"{artist}"'
        d = self.http.get(f"{MB}/recording", params={"query": q, "fmt": "json", "limit": 5})
        recs = (d or {}).get("recordings") or []
        if not recs:
            return None
        r = recs[0]
        ac = r["artist-credit"][0]["artist"]
        return {"recording_mbid": r["id"], "title": r["title"], "artist_mbid": ac["id"], "artist": ac["name"]}

    def related_artists(self, mbid: str) -> list[tuple[str, str]]:
        d = self.http.get(f"{MB}/artist/{mbid}", params={"inc": "artist-rels", "fmt": "json"})
        out = []
        for rel in (d or {}).get("relations") or []:
            if rel.get("type") in MB_REL_TYPES and rel.get("artist"):
                out.append((rel["artist"]["id"], rel["artist"]["name"]))
        return out


class Deezer:
    def __init__(self, http: Http):
        self.http = http

    def find_artists(self, name: str) -> list[tuple[int, int]]:
        """[(deezer id, fan count)] for exact name matches, most fans first; fuzzy hits are other artists."""
        d = self.http.get(f"{DZ}/search/artist", params={"q": name, "limit": 10})
        rows = [a for a in (d or {}).get("data") or [] if norm(a["name"]) == norm(name)]
        return sorted(((a["id"], a.get("nb_fan") or 0) for a in rows), key=lambda x: -x[1])

    def find_artist(self, name: str) -> tuple[int, int] | None:
        rows = self.find_artists(name)
        return rows[0] if rows else None

    def find_track(self, artist: str, title: str) -> dict | None:
        d = self.http.get(f"{DZ}/search/track", params={"q": f'artist:"{artist}" track:"{title}"', "limit": 1})
        rows = (d or {}).get("data") or []
        return rows[0] if rows else None

    def related(self, artist_id: int, limit: int = 20) -> list[tuple[int, str, int]]:
        d = self.http.get(f"{DZ}/artist/{artist_id}/related", params={"limit": limit})
        return [(a["id"], a["name"], a.get("nb_fan") or 0) for a in (d or {}).get("data") or []]

    def radio(self, artist_id: int) -> list[Track]:
        d = self.http.get(f"{DZ}/artist/{artist_id}/radio", params={"limit": 50})
        return [_dz_track(t, "deezer-radio") for t in (d or {}).get("data") or []]

    def top(self, artist_id: int, limit: int = 50) -> list[Track]:
        d = self.http.get(f"{DZ}/artist/{artist_id}/top", params={"limit": limit})
        return [_dz_track(t, "deezer-top") for t in (d or {}).get("data") or []]


def _dz_track(t: dict, src: str) -> Track:
    # Deezer rank is roughly 0..1e6; squash to 0..1
    pop = min(1.0, max(0.0, (t.get("rank") or 0) / 900_000))
    return Track(artist=t["artist"]["name"], title=t["title_short"] or t["title"], deezer_id=t["id"],
                 popularity=pop, sources={src})


def _trim_top(data):
    """Keep only what ranking needs; the raw payload (tags, colours, 2k rows) is ~1MB per artist."""
    if not isinstance(data, list):
        return data
    keep = ("recording_mbid", "recording_name", "artist_name", "artist_mbids", "release_name",
            "total_listen_count", "total_user_count")
    return [{k: r.get(k) for k in keep} for r in data[:400]]
