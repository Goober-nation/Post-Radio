"""Per-call listener data as providers: a host app passes likes and plays as plain data (no credentials), and
several history sources merge into one. Used by api.Listener."""
from collections.abc import Iterable

from ..models import Feedback, History, Like, Play
from . import FeedbackProvider, HistoryProvider


class LikesFeedback(FeedbackProvider):
    """Likes (and dislikes) given as data."""

    name = "likes"

    def __init__(self, likes: Iterable[Like]):
        self.likes = list(likes)

    def load(self) -> list[Feedback]:
        return [Feedback(x.artist, x.title, max(-1.0, min(1.0, x.score)), self.name, x.artist_mbid, x.recording_mbid)
                for x in self.likes if x.artist]

    def liked(self) -> list[tuple[Feedback, int | None]]:
        return [(f, x.at) for f, x in zip(self.load(), [x for x in self.likes if x.artist], strict=True)
                if f.score > 0]


class PlaysHistory(HistoryProvider):
    """Listening history given as data."""

    def __init__(self, plays: Iterable[Play]):
        self.history = History.from_plays(plays)

    def load(self) -> History:
        return self.history


class MergedHistory(HistoryProvider):
    """Several history sources as one; a failing one is skipped, like any provider."""

    def __init__(self, sources: list[HistoryProvider], log=lambda _m: None):
        self.sources, self.log = sources, log

    def version(self) -> object:
        return tuple(s.version() for s in self.sources)

    def load(self) -> History:
        out = []
        for s in self.sources:
            try:
                out.append(s.load())
            except Exception as e:  # noqa: BLE001 - a source's failure costs only its own history
                self.log(f"history from {type(s).__name__} unavailable: {e}")
        return History.merged(out)
