"""MusicBrainz: seed resolution, member/side-project relations, tags, genre artists. Strictly 1 req/s (see
http.py). With a local store (musicbrainz_store.py, from the JSON dump) names, aliases and genres resolve
locally first and the API is the fallback."""
import re

from ..http import Http
from ..models import Artist, Edge, Track, norm
from . import ArtistResolver, GenreSource, SimilarArtists, Tagger, parse_rows, seg
from .musicbrainz_store import ArtistStore, GenreIndex

API = "https://musicbrainz.org/ws/2"
REL_TYPES = {"member of band", "collaboration", "is person", "supporting musician", "founder", "subgroup"}
GOOD_TAG = re.compile(r"^[a-z0-9][a-z0-9 &'/\-]{1,30}$")  # drops junk like "_interview is member only?"


class MusicBrainz(ArtistResolver, SimilarArtists, Tagger, GenreSource):
    name = "mb"
    max_hop = 1

    def __init__(self, http: Http, store: ArtistStore | None = None, index: GenreIndex | None = None):
        """store: the local artist store (names, aliases, genres, genre -> artists); index: the small
        genre -> artists file for users without the store. Both optional."""
        self.http, self.store, self.index = http, store, index
        self._known_genres: set[str] | None = None

    def find_artist(self, name: str) -> Artist | None:
        """Store first: aliases resolve there (Kanye West -> Ye) and the best-tagged namesake wins."""
        if self.store and (hits := self.store.resolve(name, limit=1)):
            return Artist(hits[0]["name"], mbid=hits[0]["mbid"])
        d = self.http.get(f"{API}/artist", params={"query": name, "fmt": "json", "limit": 5})
        arts = parse_rows((d or {}).get("artists"), lambda a: a if a["id"] and a["name"] else None)
        exact = [a for a in arts if norm(a["name"]) == norm(name)]
        a = (exact or arts or [None])[0]
        return Artist(a["name"], mbid=a["id"]) if a else None

    def artist_by_mbid(self, mbid: str) -> Artist | None:
        if self.store and (a := self.store.artist(mbid)) and a["name"]:
            return Artist(a["name"], mbid=mbid)
        d = self.http.get(f"{API}/artist/{seg(mbid)}", params={"fmt": "json"})
        return Artist(d["name"], mbid=mbid) if d and d.get("name") else None

    def find_recording(self, artist: str, title: str) -> Track | None:
        q = f'recording:"{_lucene(title)}" AND artist:"{_lucene(artist)}"'
        d = self.http.get(f"{API}/recording", params={"query": q, "fmt": "json", "limit": 5})
        recs = parse_rows((d or {}).get("recordings"), self._track)
        return recs[0] if recs else None

    def recording_by_mbid(self, mbid: str) -> Track | None:
        d = self.http.get(f"{API}/recording/{seg(mbid)}", params={"inc": "artist-credits", "fmt": "json"})
        got = parse_rows([d], self._track) if isinstance(d, dict) else []
        return got[0] if got else None

    def neighbours(self, artist: Artist, limit: int) -> list[Edge]:
        if not artist.mbid:
            return []
        d = self.http.get(f"{API}/artist/{seg(artist.mbid)}", params={"inc": "artist-rels", "fmt": "json"})
        out = parse_rows((d or {}).get("relations"), lambda rel: Edge(
            Artist(rel["artist"]["name"], mbid=rel["artist"]["id"]), 1.0, self.name)
            if rel.get("type") in REL_TYPES and rel["artist"]["name"] and rel["artist"]["id"] else None)
        return out[:limit]

    def tags(self, artist: Artist, n: int = 4) -> list[str]:
        """Store genres first; else artist tags, and if those are empty or junk, tags on the artist's releases."""
        if not artist.mbid:
            return []
        if self.store and (got := self.store.genres(artist.mbid)):
            return got[:n]
        d = self.http.get(f"{API}/artist/{seg(artist.mbid)}", params={"inc": "tags genres", "fmt": "json"})
        counts = self._count((d or {}).get("tags") or [])
        if not counts:
            d = self.http.get(f"{API}/release-group", params={"artist": artist.mbid, "inc": "tags", "limit": 50,
                                                              "fmt": "json"})
            for rg in parse_rows((d or {}).get("release-groups"), lambda rg: rg if isinstance(rg, dict) else None):
                for t, c in self._count(rg.get("tags") or []).items():
                    counts[t] = counts.get(t, 0) + c
        return [t for t, _ in sorted(counts.items(), key=lambda x: -x[1])][:n]

    def recording_titles(self, artist: Artist) -> set[str]:
        """Normalised recording + release-group titles: used to verify identity on other services."""
        if not artist.mbid:
            return set()
        recs = self.http.get(f"{API}/recording", params={"artist": artist.mbid, "limit": 100, "fmt": "json"})
        rgs = self.http.get(f"{API}/release-group", params={"artist": artist.mbid, "limit": 100, "fmt": "json"})
        titles = set(parse_rows((recs or {}).get("recordings"), lambda r: norm(r["title"])))
        titles |= set(parse_rows((rgs or {}).get("release-groups"), lambda r: norm(r["title"])))
        titles.discard("")
        return titles

    # ---------------------------------------------------------------- GenreSource (store or index only)
    def _genre_table(self):
        return self.store or self.index

    def unsupported(self, genres: list[str]) -> list[str]:
        table = self._genre_table()
        if not table:
            return list(genres)
        known = self._known()
        return [g for g in genres if g.casefold() not in known]

    def _known(self) -> set[str]:
        if self._known_genres is None:
            table = self._genre_table()
            self._known_genres = table.known_genres() if table else set()
        return self._known_genres

    def genre_artists(self, genres: list[str], op: str, popularity: float, limit: int) -> list[Artist]:
        """Artists carrying the genres most strongly (MusicBrainz genre votes). `popularity` is not used: votes
        measure how sure taggers are, not audience size. With op 'and', artists carrying every genre."""
        table = self._genre_table()
        if not table:
            return []
        per_genre = [table.genre_artists(g.casefold(), limit if op == "or" else max(limit, 300)) for g in genres]
        if op == "and":
            keep = set.intersection(*({m for m, _ in rows} for rows in per_genre)) if per_genre else set()
            per_genre = [[r for r in per_genre[0] if r[0] in keep]] if per_genre else []
        seen: dict[str, Artist] = {}
        for i in range(max((len(r) for r in per_genre), default=0)):  # interleave genres, strongest first
            for rows in per_genre:
                if i < len(rows) and rows[i][1]:
                    seen.setdefault(rows[i][0], Artist(rows[i][1], mbid=rows[i][0]))
        return list(seen.values())[:limit]

    # ---------------------------------------------------------------- data build
    def genre_names(self) -> dict[str, str]:
        """Every MusicBrainz genre: {genre MBID: name}. Paged, 100 per request."""
        names: dict[str, str] = {}
        offset = 0
        while True:
            d = self.http.get(f"{API}/genre/all", params={"limit": 100, "offset": offset, "fmt": "json"})
            rows = parse_rows((d or {}).get("genres"), lambda g: (g["id"], g["name"]) if g["id"] and g["name"]
                              else None)
            names.update(rows)
            offset += 100
            total = (d or {}).get("genre-count")
            if not rows or not isinstance(total, int) or offset >= total:
                return names

    @staticmethod
    def _count(tags: list[dict]) -> dict[str, int]:
        good = parse_rows(tags, lambda t: (t["name"], int(t["count"]))
                          if int(t.get("count") or 0) > 0 and GOOD_TAG.match(t["name"]) else None)
        return dict(good)

    @staticmethod
    def _track(r: dict) -> Track | None:
        a = r["artist-credit"][0]["artist"]
        if not (r["title"] and a["name"] and a["id"] and r["id"]):
            return None
        return Track(r["title"], Artist(a["name"], mbid=a["id"]), recording_mbid=r["id"])


_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


def _lucene(text: str) -> str:
    """Escape Lucene query syntax so titles like 'He Said "Hi"' or 'AC/DC' search literally."""
    return _LUCENE_SPECIAL.sub(r"\\\1", text)
