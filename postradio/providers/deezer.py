"""Deezer: tokenless similarity, catalogue popularity and artist radio."""
import threading
import time
from collections.abc import Callable

from ..http import Http
from ..models import Artist, Edge, Track, log_popularity, norm
from . import GenreSource, RadioSource, SimilarArtists, TopTracks, parse_rows

API = "https://api.deezer.com"
FANS_FULL_SCALE = 6.9  # log10 fans of a superstar (Radiohead ~4M -> ~0.95)
RANK_FULL_SCALE = 900_000  # Deezer track 'rank' of a global hit


class Deezer(SimilarArtists, TopTracks, RadioSource, GenreSource):
    """`known_titles` lets Deezer tell same-named artists apart: given an artist with an MBID, it returns
    normalised titles that artist is known to have recorded. Only consulted when the name is ambiguous."""

    name = "deezer"

    def __init__(self, http: Http, known_titles: Callable[[Artist], set[str]] | None = None,
                 fans_full_scale: float = FANS_FULL_SCALE, rank_full_scale: int = RANK_FULL_SCALE,
                 cheap_titles: Callable[[Artist], set[str]] | None = None, memo_ttl_s: float = 7 * 86400):
        self.http = http
        self.known_titles = known_titles  # may be slow (MusicBrainz, 1 req/s): only for real ambiguity
        self.cheap_titles = cheap_titles  # free if available (e.g. LB with a token): also checks single hits
        self._resolved: dict[tuple[str, str | None], tuple[float, Artist | None]] = {}
        self.memo_ttl_s = memo_ttl_s  # same as the HTTP cache: a failed match gets retried once data may change
        self._locks: dict[tuple[str, str | None], threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.fans_full_scale, self.rank_full_scale = fans_full_scale, rank_full_scale

    # ---- identity ----
    def _candidates(self, name: str) -> list[Artist]:
        d = self.http.get(f"{API}/search/artist", params={"q": name, "limit": 10})
        rows = parse_rows((d or {}).get("data"), lambda a: a if norm(a["name"]) == norm(name) else None)
        rows.sort(key=lambda a: -(a.get("nb_fan") or 0))
        return parse_rows(rows, self._artist)

    def resolve(self, artist: Artist) -> int | None:
        """Find (and cache on the artist) its Deezer id; 0 means looked up and not found/ambiguous.
        Memoised per (name, MBID) and locked, so parallel jobs never look the same artist up twice."""
        if artist.deezer_id is not None:
            return artist.deezer_id or None
        key = (artist.key, artist.mbid)
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            hit = self._resolved.get(key)
            if hit is None or time.monotonic() - hit[0] > self.memo_ttl_s:
                hit = self._resolved[key] = (time.monotonic(), self._match(artist))
            match = hit[1]
        artist.deezer_id = match.deezer_id if match else 0
        if match:
            artist.absorb(match)
        return artist.deezer_id or None

    def _match(self, artist: Artist) -> Artist | None:
        cands = self._candidates(artist.name)
        if not cands:
            return None
        cheap = self.cheap_titles(artist) if (artist.mbid and self.cheap_titles) else set()
        if len(cands) == 1:
            # one exact-name hit: if free titles agree, accept; with no free titles, accept rather than pay
            # MB calls; if free titles *disagree*, LB and Deezer catalogues can simply differ, so let the
            # MB titles (track + release-group) settle it before throwing the match away
            only = cands[0]
            if not cheap or self._titles(only.deezer_id) & cheap:
                return only
            slow = self.known_titles(artist) if self.known_titles else set()
            if not slow:
                return only  # MB knows nothing either: no evidence against the match, same as having no titles
            return only if self._titles(only.deezer_id) & slow else None
        titles = cheap or (self.known_titles(artist) if artist.mbid and self.known_titles else set())
        if not titles:
            return cands[0]  # no way to tell them apart: most-followed
        # same-named artists (two 'Bilal's): pick by shared titles
        return next((c for c in cands if len(self._titles(c.deezer_id) & titles) >= 2), None)

    def _titles(self, did: int) -> set[str]:
        """Track and album titles: albums overlap far better with MB release groups for big catalogues."""
        d = self.http.get(f"{API}/artist/{did}/top", params={"limit": 100})
        out = set()
        for t in parse_rows((d or {}).get("data"), lambda t: t if isinstance(t, dict) else None):
            out.add(norm(t.get("title_short") or t.get("title") or ""))
            out.add(norm((t.get("album") or {}).get("title") or ""))
        out.discard("")
        return out

    # ---- interfaces ----
    def neighbours(self, artist: Artist, limit: int) -> list[Edge]:
        did = self.resolve(artist)
        if not did:
            return []
        d = self.http.get(f"{API}/artist/{did}/related", params={"limit": limit})
        rows = parse_rows((d or {}).get("data"), self._artist)
        return [Edge(a, 1 - 0.5 * i / max(1, len(rows)), self.name) for i, a in enumerate(rows)]

    def top_tracks(self, artist: Artist, limit: int = 100) -> list[Track]:
        did = artist.deezer_id if artist.deezer_id else self.resolve(artist)
        if not did:
            return []
        d = self.http.get(f"{API}/artist/{did}/top", params={"limit": limit})
        rows = parse_rows((d or {}).get("data"), lambda t: t if t.get("id") is not None and _title(t) else None)
        # Deezer /top includes features; keep the artist's own tracks, rank by position
        own = [t for t in rows if (t.get("artist") or {}).get("id") == did] or rows
        n = len(own)
        return [Track(_title(t), artist, deezer_id=t["id"], popularity=1 - i / max(1, n - 1), sources={"deezer-top"})
                for i, t in enumerate(own)]

    def radio(self, artist: Artist, popularity: float, mode: str, rng=None) -> list[Track]:
        did = self.resolve(artist)
        if not did:
            return []
        d = self.http.get(f"{API}/artist/{did}/radio", params={"limit": 50})
        def convert(t):
            a = Artist(t["artist"]["name"], deezer_id=t["artist"]["id"])
            pop = min(1.0, (t.get("rank") or 0) / self.rank_full_scale)  # no catalogue context: global estimate
            return Track(_title(t), a, deezer_id=t["id"], popularity=pop, sources={"deezer-radio"})

        return parse_rows((d or {}).get("data"), convert)

    def _genre_ids(self) -> tuple[dict[str, int], list[str]]:
        """alias -> genre id, plus display names. 'Rap/Hip Hop' is reachable as rap, hiphop or raphiphop."""
        d = self.http.get(f"{API}/genre")
        ids: dict[str, int] = {}
        names = []
        for gn in parse_rows((d or {}).get("data"), lambda g: g if g["id"] and g["name"] else None):  # 0 = "All"
            if gn["id"]:
                names.append(gn["name"])
                for alias in [gn["name"], *gn["name"].split("/")]:
                    ids.setdefault(norm(alias), gn["id"])
        return ids, names

    def supported_genres(self) -> list[str] | None:
        return self._genre_ids()[1]

    def unsupported(self, genres: list[str]) -> list[str]:
        ids = self._genre_ids()[0]
        return [g for g in genres if norm(g) not in ids]

    def genre_artists(self, genres: list[str], op: str, popularity: float, limit: int) -> list[Artist]:
        """Deezer has ~25 broad genres; with op 'and', one unknown genre means no Deezer artists."""
        ids = self._genre_ids()[0]
        if op == "and" and self.unsupported(genres):
            return []
        per_genre = []
        for gname in genres:
            if gid := ids.get(norm(gname)):
                rows = (self.http.get(f"{API}/genre/{gid}/artists") or {}).get("data") or []
                per_genre.append(parse_rows(rows, self._artist))
        if not per_genre:
            return []
        if op == "and":
            keys = set.intersection(*({a.key for a in arts} for arts in per_genre))
            per_genre = [[a for a in per_genre[0] if a.key in keys]]
        seen: dict[str, Artist] = {}
        for arts in per_genre:
            for a in arts:
                seen.setdefault(a.key, a)
        return list(seen.values())[:limit]

    def _artist(self, a: dict) -> Artist:
        fans = a.get("nb_fan")
        return Artist(a["name"], deezer_id=a["id"],
                      popularity=log_popularity(fans, self.fans_full_scale) if fans is not None else None)


def _title(t: dict) -> str:
    title = t.get("title_short") or t.get("title")
    if not title:
        raise KeyError("title")
    return title
