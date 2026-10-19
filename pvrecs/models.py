"""Normalised data model. Providers translate their API payloads into these; nothing past the
provider layer ever sees a raw JSON dict."""
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

_PAREN = re.compile(r"\s*[\(\[].*")


def norm(s: str) -> str:
    return "".join(ch for ch in s.casefold() if ch.isalnum())


def log_popularity(count: int | float, full_scale: float) -> float:
    """Map an audience size to 0..1 on a log scale; `full_scale` = log10 of a superstar's count."""
    return min(1.0, math.log10(1 + max(0, count)) / full_scale)


@dataclass
class Artist:
    name: str
    mbid: str | None = None
    deezer_id: int | None = None
    popularity: float | None = None  # global audience size, 0 = nobody, 1 = superstar

    @property
    def key(self) -> str:
        return norm(self.name)

    def absorb(self, other: "Artist") -> None:
        """Fill gaps from another view of the same artist."""
        self.mbid = self.mbid or other.mbid
        self.deezer_id = self.deezer_id or other.deezer_id
        if self.popularity is None:
            self.popularity = other.popularity


@dataclass
class Track:
    title: str
    artist: Artist
    recording_mbid: str | None = None
    deezer_id: int | None = None
    popularity: float = 0.5  # within the artist's catalogue: 1 = their biggest song, 0 = deepest cut
    sources: set[str] = field(default_factory=set)

    @property
    def key(self) -> str:
        return track_key(self.artist.name, self.title)


def track_key(artist: str, title: str) -> str:
    return norm(artist) + "|" + norm(_PAREN.sub("", title))


@dataclass(frozen=True)
class Edge:
    target: Artist
    weight: float  # 0..1, provider-normalised similarity strength
    source: str


@dataclass(frozen=True)
class Play:
    artist: str
    title: str
    timestamp: int | None = None
    artist_mbid: str | None = None
    recording_mbid: str | None = None


class History:
    """What the listener already knows. Built from raw plays or pre-aggregated counts, so any
    scrobble store (ListenBrainz, a file export, Maloja...) can supply it."""

    def __init__(self, artist_plays: dict[str, int] | None = None, heard: set[str] | None = None):
        self._artists = artist_plays or {}  # keys: artist MBID or "n:" + normalised name
        self._heard = heard or set()  # track_key()s

    @classmethod
    def from_plays(cls, plays: Iterable[Play]) -> "History":
        artists: dict[str, int] = {}
        heard: set[str] = set()
        for p in plays:
            for k in filter(None, (p.artist_mbid, "n:" + norm(p.artist))):
                artists[k] = artists.get(k, 0) + 1
            heard.add(track_key(p.artist, p.title))
        return cls(artists, heard)

    @classmethod
    def from_counts(cls, artists: Iterable[tuple[str, str | None, int]], tracks: Iterable[tuple[str, str]]):
        """artists: (name, mbid, plays); tracks: (artist name, title)."""
        a: dict[str, int] = {}
        for name, mbid, n in artists:
            for k in filter(None, (mbid, "n:" + norm(name))):
                a[k] = max(a.get(k, 0), n)
        return cls(a, {track_key(ar, t) for ar, t in tracks})

    def plays(self, artist: Artist) -> int:
        return self._artists.get(artist.mbid or "", 0) or self._artists.get("n:" + artist.key, 0)

    def heard(self, track: Track) -> bool:
        return track.key in self._heard

    def __bool__(self) -> bool:
        return bool(self._artists or self._heard)

    def summary(self) -> str:
        return f"{len({k for k in self._artists if k.startswith('n:')})} artists, {len(self._heard)} tracks"
