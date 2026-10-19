"""In-memory providers implementing the real interfaces."""
from pvrecs.models import Artist, Edge, History, Track
from pvrecs.providers import ArtistResolver, HistoryProvider, SimilarArtists, Tagger, TagRadio, TopTracks

# seed S -> A (close) -> B -> C (far); audience shrinks with distance
NAMES = {"S": "Seed", "A": "Alpha", "B": "Beta", "C": "Gamma"}
POP = {"S": 0.95, "A": 0.85, "B": 0.5, "C": 0.3}
SIM = {"S": ["A"], "A": ["B"], "B": ["C"], "C": []}


def artist(mbid):
    return Artist(NAMES[mbid], mbid=mbid)


class Resolver(ArtistResolver):
    def find_artist(self, name):
        return next((artist(m) for m, n in NAMES.items() if n == name), None)

    def artist_by_mbid(self, mbid):
        return artist(mbid) if mbid in NAMES else None

    def find_recording(self, a, title):
        return None

    def recording_by_mbid(self, mbid):
        return None


class Similar(SimilarArtists):
    name = "lb"

    def __init__(self, sim=None):
        self.sim = sim or SIM

    def neighbours(self, a, limit):
        return [Edge(artist(m), 1.0, self.name) for m in self.sim.get(a.mbid, [])][:limit]


class Tracks(TopTracks):
    name = "lb"

    def top_tracks(self, a, limit):
        a.popularity = POP.get(a.mbid, 0.4)
        return [Track(f"{a.name} song {i}", a, popularity=1 - i / 19, sources={"lb"}) for i in range(20)]


class StaticHistory(HistoryProvider):
    def __init__(self, history):
        self.h = history

    def load(self):
        return self.h


class Tags(Tagger):
    def tags(self, a):
        return ["shoegaze"] if a.mbid == "S" else []


class TagRad(TagRadio):
    def artists_for_tags(self, tags, popularity, limit):
        return [artist("B")] if tags == ["shoegaze"] else []


EMPTY = History()
