"""ListenBrainz. Similarity, troi LB-radio and tag radio work tokenless; the popularity
(top-recordings) endpoint needs a token, so LBTopTracks is only wired in when one is set."""
from troi.plist import plist

from ..http import Http
from ..models import Artist, Edge, Track, log_popularity
from . import RadioSource, SimilarArtists, TagRadio, TopTracks, TrackSimilarity

API = "https://api.listenbrainz.org/1"
LABS = "https://labs.api.listenbrainz.org"
SIMILAR_REC_ALGO = "session_based_days_9000_session_300_contribution_5_threshold_15_limit_50_skip_30"
SIMILAR_ALGO = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
LISTENERS_FULL_SCALE = 5.5  # log10 LB listeners of a superstar


def troi_window(popularity: float, width: int = 34) -> tuple[int, int]:
    """troi popularity percentiles: 0 = an artist's most-played recordings, 100 = least."""
    lo = min(max(0, round(100 * (1 - popularity)) - width // 2), 100 - width)
    return lo, lo + width


def _headers(token: str | None):
    return {"Authorization": f"Token {token}"} if token else None


class LBMetadata:
    def __init__(self, http: Http, token: str | None = None):
        self.http, self.headers = http, _headers(token)

    def recordings(self, mbids: list[str]) -> dict[str, Track]:
        out: dict[str, Track] = {}
        for i in range(0, len(mbids), 100):
            d = self.http.post(f"{API}/metadata/recording/", {"recording_mbids": mbids[i:i + 100], "inc": "artist"},
                               headers=self.headers) or {}
            for mbid, m in d.items():
                title = (m.get("recording") or {}).get("name")
                arts = (m.get("artist") or {}).get("artists") or []
                if title and arts:
                    out[mbid] = Track(title, Artist(arts[0]["name"], mbid=arts[0].get("artist_mbid")),
                                      recording_mbid=mbid)
        return out


class LBSimilar(SimilarArtists):
    name = "lb"

    def __init__(self, http: Http):
        self.http = http

    def neighbours(self, artist: Artist, limit: int) -> list[Edge]:
        if not artist.mbid:
            return []
        d = self.http.post(f"{LABS}/similar-artists/json", [{"artist_mbids": [artist.mbid], "algorithm": SIMILAR_ALGO}])
        rows = [a for a in (d or []) if a.get("artist_mbid") and a["artist_mbid"] != artist.mbid][:limit]
        top = max((float(a["score"]) for a in rows), default=1.0) or 1.0
        return [Edge(Artist(a["name"], mbid=a["artist_mbid"]), 0.5 + 0.5 * float(a["score"]) / top, self.name)
                for a in rows]


class LBSimilarRecordings(TrackSimilarity):
    """Track-to-track similarity from LB listening sessions. Tokenless."""

    def __init__(self, http: Http):
        self.http = http

    def similar(self, track: Track, limit: int) -> list[tuple[Track, float]]:
        if not track.recording_mbid:
            return []
        d = self.http.post(f"{LABS}/similar-recordings/json",
                           [{"recording_mbids": [track.recording_mbid], "algorithm": SIMILAR_REC_ALGO}])
        rows = [r for r in (d or []) if r.get("recording_mbid") and r["recording_mbid"] != track.recording_mbid]
        rows = rows[:limit]
        top = max((float(r.get("score") or 0) for r in rows), default=1.0) or 1.0
        out = []
        for r in rows:
            mbids = r.get("artist_credit_mbids") or [None]
            t = Track(r["recording_name"], Artist(r["artist_credit_name"], mbid=mbids[0]),
                      recording_mbid=r["recording_mbid"], sources={"lb-similar-rec"})
            out.append((t, float(r.get("score") or 0) / top))
        return out


class LBTopTracks(TopTracks):
    name = "lb"

    def __init__(self, http: Http, token: str, listeners_full_scale: float = LISTENERS_FULL_SCALE,
                 min_listener_share: float = 0.002):
        self.http, self.headers = http, _headers(token)
        self.full_scale, self.min_share = listeners_full_scale, min_listener_share

    def top_tracks(self, artist: Artist, limit: int = 120) -> list[Track]:
        if not artist.mbid:
            return []
        rows = self.http.get(f"{API}/popularity/top-recordings-for-artist/{artist.mbid}", headers=self.headers,
                             shape=_trim_top)
        if not isinstance(rows, list) or not rows:
            return []
        top_users = rows[0]["total_user_count"] or 0
        if artist.popularity is None:
            artist.popularity = log_popularity(top_users, self.full_scale)
        floor = max(5, top_users * self.min_share)  # drop one-off bootlegs nobody plays
        rows = [r for r in rows if (r["total_user_count"] or 0) >= floor][:limit]
        n = len(rows)
        return [Track(r["recording_name"], artist, recording_mbid=r["recording_mbid"],
                      popularity=1 - i / max(1, n - 1), sources={"lb"}) for i, r in enumerate(rows)]


class TroiRadio(RadioSource):
    """LB-radio artist search (the endpoint troi's LB Radio uses): the seed + LB-similar artists within a
    popularity window. Fetched through our Http layer (cache, throttle, backoff); troi's own `plist`
    selection picks the recordings, so results match troi's semantics."""

    name = "troi"
    MODES = {"easy", "medium", "hard"}

    def __init__(self, http: Http, token: str | None = None, window: int = 34, per_artist: int = 8,
                 similar_artists: int = 6):
        self.http, self.headers = http, _headers(token)
        self.meta = LBMetadata(http, token)
        self.window, self.per_artist, self.similar_artists = window, per_artist, similar_artists

    def radio(self, artist: Artist, popularity: float, mode: str) -> list[Track]:
        if not artist.mbid or mode not in self.MODES:
            return []
        lo, hi = troi_window(popularity, self.window)
        d = self.http.get(f"{API}/lb-radio/artist/{artist.mbid}", headers=self.headers, params={
            "mode": mode, "max_similar_artists": self.similar_artists,
            "max_recordings_per_artist": self.per_artist, "pop_begin": lo, "pop_end": hi})
        if not isinstance(d, dict):
            return []
        picked: dict[str, list[str]] = {}
        for amb, rows in d.items():
            pool = plist([r["recording_mbid"] for r in rows if r.get("recording_mbid")])
            chosen = pool.random_item(lo, hi, self.per_artist) if pool else []
            picked[amb] = chosen if isinstance(chosen, list) else [chosen]
        meta = self.meta.recordings([m for ms in picked.values() for m in ms])
        names = {amb: rows[0].get("similar_artist_name") for amb, rows in d.items() if rows}
        out = []
        for amb, mbids in picked.items():
            for m in mbids:
                t = meta.get(m)
                if t:
                    t.artist = artist if amb == artist.mbid else Artist(names.get(amb) or t.artist.name, mbid=amb)
                    t.popularity, t.sources = 1 - (lo + hi) / 200, {"troi"}
                    out.append(t)
        return out


class TroiTagRadio(TagRadio):
    """LB-radio tag search (troi's tag element endpoint), through our Http layer."""

    def __init__(self, http: Http, token: str | None = None, window: int = 34):
        self.http, self.headers = http, _headers(token)
        self.meta = LBMetadata(http, token)
        self.window = window

    def recordings_for_tags(self, tags: list[str], popularity: float, limit: int) -> list[str]:
        lo, hi = troi_window(popularity, self.window)
        d = self.http.get(f"{API}/lb-radio/tags", headers=self.headers, params={
            "tag": tags, "operator": "or", "pop_begin": lo, "pop_end": hi, "count": limit})
        return [r["recording_mbid"] for r in d or [] if r.get("recording_mbid")] if isinstance(d, list) else []

    def artists_for_tags(self, tags: list[str], popularity: float, limit: int) -> list[Artist]:
        seen: dict[str, Artist] = {}
        for t in self.meta.recordings(self.recordings_for_tags(tags, popularity, limit)).values():
            seen.setdefault(t.artist.key, t.artist)
        return list(seen.values())


def _trim_top(data):
    """Keep only what ranking needs; the raw payload (tags, colours, 2k rows) is ~1MB per artist."""
    if not isinstance(data, list):
        return data
    keep = ("recording_mbid", "recording_name", "total_user_count")
    return [{k: r.get(k) for k in keep} for r in data[:400]]
