"""Provider interfaces. The engine only talks to these; each source lives in its own adapter module."""
import random
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from typing import TypeVar

from ..models import Artist, Edge, Feedback, History, Track

T = TypeVar("T")


def seg(value) -> str:
    """URL-encode one path segment (user names, MBIDs): nothing user-supplied can reshape the path."""
    from urllib.parse import quote

    return quote(str(value), safe="")
ROW_ERRORS = (KeyError, TypeError, ValueError, AttributeError, IndexError)


def parse_rows(rows: Iterable | None, convert: Callable[[dict], T | None]) -> list[T]:
    """Convert API rows one by one, skipping any that are malformed (missing/null fields, wrong types).
    Real-world payloads are messy; one bad row must never cost the rest of the response."""
    out = []
    for row in rows if isinstance(rows, list | tuple) else []:
        try:
            item = convert(row)
        except ROW_ERRORS:
            continue
        if item is not None:
            out.append(item)
    return out


class ArtistResolver(ABC):
    """Turns user input into normalised seeds."""

    @abstractmethod
    def find_artist(self, name: str) -> Artist | None: ...

    @abstractmethod
    def artist_by_mbid(self, mbid: str) -> Artist | None: ...

    @abstractmethod
    def find_recording(self, artist: str, title: str) -> Track | None: ...

    @abstractmethod
    def recording_by_mbid(self, mbid: str) -> Track | None: ...


class SimilarArtists(ABC):
    name: str
    max_hop: int = 99  # e.g. band-member relations only make sense right next to the seed

    @abstractmethod
    def neighbours(self, artist: Artist, limit: int) -> list[Edge]: ...


class TopTracks(ABC):
    """An artist's catalogue ordered by popularity. Implementations should fill artist.popularity."""

    name: str

    @abstractmethod
    def top_tracks(self, artist: Artist, limit: int) -> list[Track]: ...


class RadioSource(ABC):
    """Seeded radio: may return tracks by artists outside the similarity graph."""

    name: str

    @abstractmethod
    def radio(self, artist: Artist, popularity: float, mode: str, rng: random.Random | None = None) -> list[Track]:
        """rng: use it for any random choice so a run's --rng-seed reproduces the playlist."""


class Tagger(ABC):
    @abstractmethod
    def tags(self, artist: Artist, n: int = 4) -> list[str]:
        """Up to n genre/tag names for the artist, strongest first."""


class TagRadio(ABC):
    @abstractmethod
    def artists_for_tags(self, tags: list[str], popularity: float, limit: int) -> list[Artist]: ...


class GenreSource(ABC):
    """Genre/tag seeds. op is 'and' (all genres) or 'or' (any)."""

    name: str

    @abstractmethod
    def genre_artists(self, genres: list[str], op: str, popularity: float, limit: int) -> list[Artist]: ...

    def genre_tracks(self, genres: list[str], op: str, popularity: float, limit: int) -> list[Track]:
        return []

    def supported_genres(self) -> list[str] | None:
        """The fixed genre list this source understands, or None for free-form tags."""
        return None

    def unsupported(self, genres: list[str]) -> list[str]:
        """Requested genres this source can't serve (it contributes nothing for them)."""
        return []


class HistoryProvider(ABC):
    @abstractmethod
    def load(self) -> History: ...

    def version(self) -> object:
        """Same contract as FeedbackProvider.version()."""
        return None


class NoHistory(HistoryProvider):
    def load(self) -> History:
        return History()


class FeedbackProvider(ABC):
    """Loves/hates/ratings from one place (LB, Navidrome, ...), normalised to Feedback scores."""

    name: str

    @abstractmethod
    def load(self) -> list[Feedback]: ...

    def liked(self) -> list[tuple[Feedback, int | None]]:
        """Positive opinions with when they were given (unix seconds, None if unknown). Flow mode builds the
        taste profile from these, newest weighted most. Sources that know dates override this."""
        return [(f, None) for f in self.load() if f.score > 0]

    def version(self) -> object:
        """Changes whenever load() may return something different; consumers reload on change.
        None = never changes (e.g. a static list)."""
        return None


class TrackSimilarity(ABC):
    @abstractmethod
    def similar(self, track: Track, limit: int) -> list[tuple[Track, float]]:
        """(similar track, weight 0..1), strongest first. track needs a recording MBID."""


def time_bucket(seconds: int) -> int:
    """A version() for remote sources: changes every `seconds` (their cache max-age)."""
    import time

    return int(time.time() // max(1, seconds))
