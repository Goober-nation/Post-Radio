"""Deezer: tokenless similarity, catalogue popularity and artist radio."""
from collections.abc import Callable

from ..http import Http
from ..models import Artist, Edge, Track, log_popularity, norm
from . import GenreSource, RadioSource, SimilarArtists, TopTracks

API = "https://api.deezer.com"
FANS_FULL_SCALE = 6.9  # log10 fans of a superstar (Radiohead ~4M -> ~0.95)
RANK_FULL_SCALE = 900_000  # Deezer track 'rank' of a global hit


class Deezer(SimilarArtists, TopTracks, RadioSource, GenreSource):
    """`known_titles` lets Deezer tell same-named artists apart: given an artist with an MBID, it returns
    normalised titles that artist is known to have recorded. Only consulted when the name is ambiguous."""

    name = "deezer"

    def __init__(self, http: Http, known_titles: Callable[[Artist], set[str]] | None = None,
                 fans_full_scale: float = FANS_FULL_SCALE, rank_full_scale: int = RANK_FULL_SCALE):
        self.http = http
        self.known_titles = known_titles
        self.fans_full_scale, self.rank_full_scale = fans_full_scale, rank_full_scale

    # ---- identity ----
    def _candidates(self, name: str) -> list[Artist]:
        d = self.http.get(f"{API}/search/artist", params={"q": name, "limit": 10})
        rows = [a for a in (d or {}).get("data") or [] if norm(a["name"]) == norm(name)]
        rows.sort(key=lambda a: -(a.get("nb_fan") or 0))
        return [self._artist(a) for a in rows]

    def resolve(self, artist: Artist) -> int | None:
        """Find (and cache on the artist) its Deezer id; 0 means looked up and not found/ambiguous."""
        if artist.deezer_id is not None:
            return artist.deezer_id or None
        cands = self._candidates(artist.name)
        match = None
        if len(cands) == 1:
            match = cands[0]  # a single exact-name hit: verifying it would cost calls and prove little
        elif cands and artist.mbid and self.known_titles:
            titles = self.known_titles(artist)  # same-named artists (two 'Bilal's): pick by shared titles
            match = next((c for c in cands if len(self._titles(c.deezer_id) & titles) >= 2), None) if titles else None
        elif cands:
            match = cands[0]  # no way to tell them apart: most-followed
        artist.deezer_id = match.deezer_id if match else 0
        if match:
            artist.absorb(match)
        return artist.deezer_id or None

    def _titles(self, did: int) -> set[str]:
        """Track and album titles: albums overlap far better with MB release groups for big catalogues."""
        d = self.http.get(f"{API}/artist/{did}/top", params={"limit": 100})
        out = set()
        for t in (d or {}).get("data") or []:
            out.add(norm(t["title_short"] or t["title"]))
            out.add(norm((t.get("album") or {}).get("title") or ""))
        out.discard("")
        return out

    # ---- interfaces ----
    def neighbours(self, artist: Artist, limit: int) -> list[Edge]:
        did = self.resolve(artist)
        if not did:
            return []
        d = self.http.get(f"{API}/artist/{did}/related", params={"limit": limit})
        rows = (d or {}).get("data") or []
        return [Edge(self._artist(a), 1 - 0.5 * i / max(1, len(rows)), self.name) for i, a in enumerate(rows)]

    def top_tracks(self, artist: Artist, limit: int = 100) -> list[Track]:
        did = artist.deezer_id if artist.deezer_id else self.resolve(artist)
        if not did:
            return []
        d = self.http.get(f"{API}/artist/{did}/top", params={"limit": limit})
        rows = (d or {}).get("data") or []
        # Deezer /top includes features; keep the artist's own tracks, rank by position
        own = [t for t in rows if t["artist"]["id"] == did] or rows
        n = len(own)
        return [Track(t["title_short"] or t["title"], artist, deezer_id=t["id"],
                      popularity=1 - i / max(1, n - 1), sources={"deezer-top"}) for i, t in enumerate(own)]

    def radio(self, artist: Artist, popularity: float, mode: str) -> list[Track]:
        did = self.resolve(artist)
        if not did:
            return []
        d = self.http.get(f"{API}/artist/{did}/radio", params={"limit": 50})
        out = []
        for t in (d or {}).get("data") or []:
            a = Artist(t["artist"]["name"], deezer_id=t["artist"]["id"])
            pop = min(1.0, (t.get("rank") or 0) / self.rank_full_scale)  # no catalogue context: global estimate
            out.append(Track(t["title_short"] or t["title"], a, deezer_id=t["id"], popularity=pop,
                             sources={"deezer-radio"}))
        return out

    def genre_artists(self, genres: list[str], op: str, popularity: float, limit: int) -> list[Artist]:
        """Deezer only has ~25 broad genres (Rock, Rap/Hip Hop, ...); niche genres simply match nothing.
        'and' keeps artists listed under every requested genre."""
        d = self.http.get(f"{API}/genre")
        ids: dict[str, int] = {}
        for gn in (d or {}).get("data") or []:
            if gn["id"]:  # 0 = "All"
                for alias in [gn["name"], *gn["name"].split("/")]:  # "Rap/Hip Hop" -> rap, hiphop
                    ids.setdefault(norm(alias), gn["id"])
        per_genre = []
        for gname in genres:
            gid = ids.get(norm(gname))
            if gid:
                rows = (self.http.get(f"{API}/genre/{gid}/artists") or {}).get("data") or []
                per_genre.append([self._artist(a) for a in rows])
        if not per_genre or (op == "and" and len(per_genre) < len(genres)):
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
