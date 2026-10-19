"""ListenBrainz. Similarity, troi LB-radio and tag radio work tokenless; the popularity
(top-recordings) endpoint needs a token, so LBTopTracks is only wired in when one is set."""
import random

from troi.plist import plist

from ..http import Http
from ..models import Artist, Edge, Track, log_popularity, norm
from . import GenreSource, RadioSource, SimilarArtists, TagRadio, TopTracks, TrackSimilarity

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

    def _rows(self, mbid: str) -> list[dict]:
        rows = self.http.get(f"{API}/popularity/top-recordings-for-artist/{mbid}", headers=self.headers,
                             shape=_trim_top)
        return rows if isinstance(rows, list) else []

    def titles(self, artist: Artist) -> set[str]:
        """Normalised titles of the artist's recordings. Read-only: never touches the Artist."""
        return {norm(r["recording_name"]) for r in self._rows(artist.mbid)} if artist.mbid else set()

    def top_tracks(self, artist: Artist, limit: int = 120) -> list[Track]:
        rows = self._rows(artist.mbid) if artist.mbid else []
        if not rows:
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

    def radio(self, artist: Artist, popularity: float, mode: str, rng: random.Random | None = None) -> list[Track]:
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
            picked[amb] = _troi_pick([r["recording_mbid"] for r in rows if r.get("recording_mbid")], lo, hi,
                                     self.per_artist, rng or random.Random())
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


def _troi_pick(mbids: list[str], lo: int, hi: int, count: int, rng: random.Random) -> list[str]:
    """troi's plist.random_item (slice by popularity percentile, then sample) with an injectable RNG,
    so --rng-seed reproduces it; the percentile maths is troi's own."""
    pool = plist(mbids)
    if not pool:
        return []
    window = list(pool)[pool._get_index(lo):pool._get_index(hi)]  # plist overrides __getitem__; slice a list
    return rng.sample(window, min(count, len(window)))


class TroiTagRadio(TagRadio, GenreSource):
    """LB-radio tag search (troi's tag element endpoint), through our Http layer. Serves both the
    sparse-seed tag fallback and genre seeds."""

    name = "lb-tags"

    def __init__(self, http: Http, token: str | None = None, window: int = 34, recordings_per_artist: int = 3):
        self.http, self.headers = http, _headers(token)
        self.meta = LBMetadata(http, token)
        self.window, self.recordings_per_artist = window, recordings_per_artist

    def _search(self, tags: list[str], op: str, popularity: float, limit: int) -> list[tuple[str, float]]:
        """(recording mbid, popularity 0..1) — LB's 'percent' is a popularity percentile, 0 = most played."""
        lo, hi = troi_window(popularity, self.window)
        d = self.http.get(f"{API}/lb-radio/tags", headers=self.headers, params={
            "tag": [t.lower() for t in tags], "operator": op, "pop_begin": lo, "pop_end": hi, "count": limit})
        if not isinstance(d, list):
            return []
        return [(r["recording_mbid"], 1 - float(r.get("percent") or 50) / 100) for r in d if r.get("recording_mbid")]

    def artists_for_tags(self, tags: list[str], popularity: float, limit: int) -> list[Artist]:
        return self.genre_artists(tags, "or", popularity, limit)

    def genre_tracks(self, genres: list[str], op: str, popularity: float, limit: int) -> list[Track]:
        hits = self._search(genres, op, popularity, limit)
        meta = self.meta.recordings([m for m, _ in hits])
        out = []
        for m, pop in hits:
            t = meta.get(m)
            if t:
                t.popularity, t.sources = pop, {"lb-tags"}
                out.append(t)
        return out

    def genre_artists(self, genres: list[str], op: str, popularity: float, limit: int) -> list[Artist]:
        seen: dict[str, Artist] = {}
        # tagged recordings come several per artist, so ask for enough to find `limit` distinct artists
        for t in self.genre_tracks(genres, op, popularity, limit * self.recordings_per_artist):
            seen.setdefault(t.artist.key, t.artist)
        return list(seen.values())[:limit]


def _trim_top(data):
    """Keep only what ranking needs; the raw payload (tags, colours, 2k rows) is ~1MB per artist."""
    if not isinstance(data, list):
        return data
    keep = ("recording_mbid", "recording_name", "total_user_count")
    return [{k: r.get(k) for k in keep} for r in data[:400]]
