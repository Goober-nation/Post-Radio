"""MusicBrainz: seed resolution, member/side-project relations, tags. Strictly 1 req/s (see http.py)."""
import re

from ..http import Http
from ..models import Artist, Edge, Track, norm
from . import ArtistResolver, SimilarArtists, Tagger, parse_rows, seg

API = "https://musicbrainz.org/ws/2"
REL_TYPES = {"member of band", "collaboration", "is person", "supporting musician", "founder", "subgroup"}
GOOD_TAG = re.compile(r"^[a-z0-9][a-z0-9 &'/\-]{1,30}$")  # drops junk like "_interview is member only?"


class MusicBrainz(ArtistResolver, SimilarArtists, Tagger):
    name = "mb"
    max_hop = 1

    def __init__(self, http: Http):
        self.http = http

    def find_artist(self, name: str) -> Artist | None:
        d = self.http.get(f"{API}/artist", params={"query": name, "fmt": "json", "limit": 5})
        arts = parse_rows((d or {}).get("artists"), lambda a: a if a["id"] and a["name"] else None)
        exact = [a for a in arts if norm(a["name"]) == norm(name)]
        a = (exact or arts or [None])[0]
        return Artist(a["name"], mbid=a["id"]) if a else None

    def artist_by_mbid(self, mbid: str) -> Artist | None:
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
        """Artist tags; if those are empty or junk, fall back to tags on the artist's releases."""
        if not artist.mbid:
            return []
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
