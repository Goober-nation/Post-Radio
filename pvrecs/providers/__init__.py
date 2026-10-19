"""Provider interfaces. The engine only talks to these; each source lives in its own adapter module."""
from abc import ABC, abstractmethod

from ..models import Artist, Edge, Feedback, History, Track


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
    def radio(self, artist: Artist, popularity: float, mode: str) -> list[Track]: ...


class Tagger(ABC):
    @abstractmethod
    def tags(self, artist: Artist, n: int = 4) -> list[str]:
        """Up to n genre/tag names for the artist, strongest first."""


class TagRadio(ABC):
    @abstractmethod
    def artists_for_tags(self, tags: list[str], popularity: float, limit: int) -> list[Artist]: ...


class HistoryProvider(ABC):
    @abstractmethod
    def load(self) -> History: ...


class NoHistory(HistoryProvider):
    def load(self) -> History:
        return History()


class FeedbackProvider(ABC):
    """Loves/hates/ratings from one place (LB, Navidrome, ...), normalised to Feedback scores."""

    name: str

    @abstractmethod
    def load(self) -> list[Feedback]: ...


class TrackSimilarity(ABC):
    @abstractmethod
    def similar(self, track: Track, limit: int) -> list[tuple[Track, float]]:
        """(similar track, weight 0..1), strongest first. track needs a recording MBID."""
