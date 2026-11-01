"""Typed results: what Recommender returns to a library caller, and (via to_dict) what the HTTP API sends.

One track shape for every playlist kind. Fields that only one kind fills are None for the others:
radio fills `hop` and `path`, flow fills `stop`. The JSON form is to_dict(); from_dict() reads it back
(saved playlists, HTTP clients). Keys are stable: new fields may be added, existing ones keep their meaning."""
from dataclasses import asdict, dataclass, field, fields
from typing import Any

SCHEMA = 1  # bumped only if an existing field changes meaning or is removed

# What flow / profile made of the listener's taste (Profile.input, FlowPreview.input, Playlist.stats["input"]):
# likes received, distinct artists, artists considered (the most-liked, capped), matched to MusicBrainz,
# with genres from any source, and whether the local artist store was used. Thin input = weak results.
INPUT = ("likes", "artists", "considered", "matched", "with_genres", "store")


def _pick(cls, d: dict) -> dict:
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


@dataclass
class PlaylistTrack:
    artist: str
    title: str
    artist_mbid: str | None = None
    recording_mbid: str | None = None
    deezer_id: int | None = None
    popularity: float | None = None  # 0..1 within the artist's catalogue
    artist_popularity: float | None = None  # 0..1 the artist's own fame, when a source reported it
    score: float | None = None  # ranking score (relative within one playlist)
    sources: list[str] = field(default_factory=list)  # streams that offered it: deezer-top, lb, troi, ...
    familiar: bool = False  # the listener already knows (plays or likes) this artist
    genres: list[str] = field(default_factory=list)  # flow: the artist's genres, strongest first
    hop: int | None = None  # radio: graph distance from the seed
    path: list[str] | None = None  # radio: artists from the seed to this one
    stop: str | None = None  # flow: the genre stop it belongs to
    taste: dict[str, float] | None = None  # radio: {"artist", "track", "similar_boost"} taste factors
    via: list[str] | None = None  # why the artist is in: radio: the sources linking it (lb, deezer, genre...);
    #                               flow: "likes" or "genre:<source>"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PlaylistTrack":
        return cls(**_pick(cls, d))


@dataclass
class Seed:
    artist: str | None = None
    artist_mbid: str | None = None
    track: str | None = None
    popularity: float | None = None
    genres: list[str] | None = None
    genre_op: str | None = None


@dataclass
class Playlist:
    """kind: "radio" (seeded) or "flow" (from the listener's likes)."""
    kind: str
    tracks: list[PlaylistTrack]
    explore: float
    popularity: float | None  # None = followed explore
    rng_seed: int | None  # pass back as rng_seed to repeat the run
    seed: Seed | None = None  # radio: the (first) seed
    seeds: list[Seed] = field(default_factory=list)  # radio: every seed, when several were blended
    genre_path: list[str] | None = None  # flow: the genre stops, in order
    profile: list[dict[str, Any]] | None = None  # flow: [{"genre", "share"}] the listener's genres
    familiarity: float | None = None  # flow: mean own-genre share along the path (1 = all yours)
    flow_score: float | None = None  # flow: mean genre closeness of neighbouring tracks
    stats: dict[str, Any] = field(default_factory=dict)  # candidates, graph_size, mode, mix, unheard, input ...
    result_id: str | None = None  # set only when kept (keep=True)
    created_at: str | None = None
    default_name: str | None = None
    schema: int = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Playlist":
        d = _pick(cls, d)
        d["tracks"] = [PlaylistTrack.from_dict(t) for t in d.get("tracks") or []]
        d["seed"] = Seed(**_pick(Seed, d["seed"])) if d.get("seed") else None
        d["seeds"] = [Seed(**_pick(Seed, x)) for x in d.get("seeds") or []]
        return cls(**d)


@dataclass
class Week:
    week: str
    title: str
    playlist_mbid: str
    url: str
    tracks: list[PlaylistTrack]


@dataclass
class DeepCuts:
    """ListenBrainz's own Weekly Exploration / Jams, one entry per week."""
    kind: str
    user: str
    weeks: list[Week]
    result_id: str | None = None
    created_at: str | None = None
    default_name: str | None = None
    schema: int = SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphPreview:
    """radio dry=True: the seed and its artist graph by hop, no tracks."""
    rng_seed: int | None
    seed: Seed
    seeds: list[Seed]
    explore: float
    popularity: float | None
    mode: str
    max_hops: int
    target_hop: float
    popularity_target: float
    graph_size: int
    hops: dict[int, dict[str, Any]]  # hop -> {"artists": count, "top": [{"artist", "affinity", "via", "path"}]}
    dry: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Profile:
    """The listener's genres (flow's starting point) and the nearest genres beyond them."""
    profile: list[dict[str, Any]]  # [{"genre", "share"}]
    nearby: list[dict[str, Any]]  # [{"genre", "score", "hop"}]
    input: dict[str, Any] | None = None  # see INPUT

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FlowPreview:
    """flow dry=True: the profile and the genre path, no tracks."""
    rng_seed: int | None
    genre_path: list[str]
    explore: float
    popularity: float | None
    stops: int
    tracks_per_stop: list[int]
    familiarity: float
    graph_learned: bool
    profile: list[dict[str, Any]]
    nearby: list[dict[str, Any]]
    input: dict[str, Any] | None = None  # see INPUT
    dry: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Progress:
    """One progress event of a running call (progress= on generate / flow / profile, and HTTP jobs).

    stage: radio: seed, history, graph, catalogues, radio, scoring, done.
           flow:  likes, profile, path, pools, catalogues, fill, done.
    done/total: counts within the stage when known (artists fetched, stops filled); None otherwise."""
    stage: str
    done: int | None = None
    total: int | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
