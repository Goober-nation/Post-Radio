"""Normalised data model. Providers translate their API payloads into these; nothing past the
provider layer ever sees a raw JSON dict."""
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

_PAREN = re.compile(r"\s*[\(\[].*")


_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def is_mbid(value: str) -> bool:
    return bool(_UUID.match(value or ""))


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
    virtual: bool = False  # a non-artist seed node (e.g. a genre); never sent to artist APIs

    @property
    def key(self) -> str:
        # names with no letters/digits ("!!!", "+/-") would all normalise to ""; fall back to the MBID or
        # the raw casefolded name so different artists never merge into one graph node
        base = norm(self.name) or (f"mbid:{self.mbid}" if self.mbid else "raw:" + self.name.strip().casefold())
        return ("~" if self.virtual else "") + base

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
    a = norm(artist) or "raw:" + artist.strip().casefold()
    t = norm(_PAREN.sub("", title)) or "raw:" + title.strip().casefold()
    return a + "|" + t


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


@dataclass(frozen=True)
class Feedback:
    """One opinion from one source. score in [-1, 1]: love/star/5 stars = 1, hate/1 star = -1, 3 stars = 0.
    title=None means the opinion is about the artist as a whole (starred/rated artist)."""
    artist: str
    title: str | None
    score: float
    source: str
    artist_mbid: str | None = None
    recording_mbid: str | None = None


def rating_score(stars: int) -> float:
    """1..5 stars -> -1..1, 3 is neutral."""
    return (max(1, min(5, stars)) - 3) / 2


class Taste:
    """Aggregated likes/dislikes. Per track, the first source in `priority` with an opinion wins
    (Navidrome over LB by default); artist scores sum their tracks' scores, squashed to [-1, 1]."""

    def __init__(self, items: Iterable[Feedback] = (), priority: list[str] | None = None):
        rank = {s: i for i, s in enumerate(priority or [])}
        best: dict[str, Feedback] = {}
        artist_direct: dict[str, Feedback] = {}
        for f in items:
            key = track_key(f.artist, f.title) if f.title else "a:" + norm(f.artist)
            target = best if f.title else artist_direct
            old = target.get(key)
            if old is None or rank.get(f.source, 99) < rank.get(old.source, 99):
                target[key] = f
        self.tracks = best  # track_key -> winning Feedback
        self._by_recording = {f.recording_mbid: f for f in best.values() if f.recording_mbid}
        totals: dict[str, float] = {}
        for f in list(best.values()) + list(artist_direct.values()):
            weight = 2.0 if f.title is None else 1.0  # an explicit artist opinion counts double
            for k in filter(None, (f.artist_mbid, "n:" + norm(f.artist))):
                totals[k] = totals.get(k, 0.0) + weight * f.score
        self._artists = {k: math.tanh(v / 2) for k, v in totals.items()}

    def artist(self, artist: Artist) -> float:
        return self._artists.get(artist.mbid or "", 0.0) or self._artists.get("n:" + artist.key, 0.0)

    def track(self, track: Track) -> float:
        f = self._by_recording.get(track.recording_mbid or "") or self.tracks.get(track.key)
        return f.score if f else 0.0

    def strongest(self, n: int) -> list[Feedback]:
        """Track opinions with a recording MBID, strongest first (feeds the track-similarity layer)."""
        items = [f for f in self.tracks.values() if f.recording_mbid and f.score]
        return sorted(items, key=lambda f: -abs(f.score))[:n]

    def __bool__(self) -> bool:
        return bool(self.tracks or self._artists)

    def summary(self) -> str:
        loves = sum(1 for f in self.tracks.values() if f.score > 0)
        hates = sum(1 for f in self.tracks.values() if f.score < 0)
        return f"{loves} liked / {hates} disliked tracks, {len(self._artists)} artist keys"
