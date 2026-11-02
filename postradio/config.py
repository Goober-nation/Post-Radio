"""All tunables in one place. `config.example.yaml` mirrors these dataclasses; a YAML file only
needs the keys it changes. Unknown keys are an error (catches typos).

Folders. Post-Radio never uses its own install location or the working directory for files:
- a library caller passes `home=` (or `cache_dir` / `data_dir` / `state_dir`) to `Recommender`, or gets the
  user's own folders (`~/.cache/postradio`, `~/.local/share/postradio`);
- the CLI and `postradio serve` also honour `$POSTRADIO_HOME` and `$POSTRADIO_CONFIG` (see `find_config`).
Under a home: `cache/` (API cache, disposable), `data/` (store + genre files, re-downloadable), `state/`
(saved playlists, imported loves: user data). Relative paths in a config file resolve against that file's
own folder."""
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path

import yaml

from .brand import CONFIG_ENV, HOME_ENV, NAME
from .errors import ConfigError

ROOT = Path(__file__).resolve().parent.parent  # the source checkout; only tests and dev scripts use it


# ---------------------------------------------------------------- user-facing
@dataclass
class Defaults:
    explore: float = 0.5  # 0 = seed + closest neighbours, 1 = ~3 hops out
    popularity: float | None = None  # 0 = deep cuts by small artists, 1 = hits; null = follow explore
    n: int = 50  # playlist length
    per_artist: int = 2  # max tracks per artist
    unheard: str | None = None  # null | tracks (drop played tracks) | artists (also drop artists you know)


@dataclass
class Storage:
    save_dir: str | None = None  # saved playlists (JSON); null = <state>/playlists
    feedback_file: str | None = None  # imported loves/hates log; null = <save_dir>/feedback/opinions.jsonl
    result_ttl_s: int = 3600  # results kept in memory (keep=True, the server, --save) this long...
    result_cache_size: int = 50  # ...up to this many
    http_cache: str | None = None  # API response cache (SQLite); null = <cache>/http.sqlite
    http_ttl_s: int = 7 * 86400
    data_dir: str | None = None  # optional data (mb_artists.sqlite, genre files); null = <home>/data


@dataclass
class Sources:
    use_lb_token: bool = True  # adds LB popularity as a 2nd track source when $LISTENBRAINZ_TOKEN is set
    history: str = "lb"  # lb | file | yandex | none
    lb_user: str | None = None  # null = $LISTENBRAINZ_USERNAME
    history_file: str | None = None  # .jsonl/.csv plays (artist, title, [timestamp, artist_mbid, recording_mbid])
    yandex_playlist: str | None = None  # public Yandex Music playlist link (e.g. your likes): taste + history
    yandex_tracks: int | None = None  # keep only the newest N playlist tracks; null = all


@dataclass
class FeedbackCfg:
    enabled: bool = True  # loves/hates/ratings influence the recs
    sources: list[str] = field(default_factory=lambda: ["json", "yandex", "navidrome", "lb"])  # first wins
    navidrome_url: str | None = None  # null = $NAVIDROME_URL; creds from $NAVIDROME_USERNAME/$NAVIDROME_PASSWORD
    lb_user: str | None = None  # null = sources.lb_user / $LISTENBRAINZ_USERNAME


@dataclass
class Server:
    host: str = "127.0.0.1"
    port: int = 8765
    job_workers: int = 2  # background jobs (POST /jobs/...) running at once; more wait queued


# ---------------------------------------------------------------- advanced (algorithm constants)
@dataclass
class Advanced:
    # stream caps, 0..1 (1 = all of it, 0 = off), applied to each whole stream (all artists / calls together):
    # deezer-top 0.2 = keep 20% of all Deezer catalogue candidates, those nearest the popularity target.
    # track-sim keeps its most-similar-first order instead. Each cap only touches its own stream. Per run: --mix.
    mix: dict[str, float] = field(default_factory=lambda: {
        "deezer-top": 1.0,  # Deezer artist top tracks (catalogue)
        "lb": 1.0,  # ListenBrainz popularity catalogue (needs a token)
        "deezer-radio": 1.0,  # Deezer artist radio around the seed + closest neighbours
        "troi": 1.0,  # LB Radio (troi) seed + similar-artist picks
        "track-sim": 1.0,  # similar recordings injected by a track seed
        "lb-tags": 1.0})  # tagged recordings injected by a genre seed
    # explore -> graph shape
    hop_span: float = 3.0  # target hop = hop_span * explore
    max_extra_hops: int = 2  # max hops = 1 + round(max_extra_hops * explore)
    radio_mode_thresholds: tuple[float, float] = (0.34, 0.67)  # troi easy | medium | hard
    # explore/popularity -> targets
    pop_slope: float = 0.85  # track popularity target = 1 - pop_slope * explore (no slider)
    ceiling_base: float = 0.95  # artist popularity ceiling = base - slope * explore (no slider)
    ceiling_slope: float = 0.6
    slider_ceiling_base: float = 0.35  # with slider: ceiling = base + slope * popularity
    slider_ceiling_slope: float = 0.6
    seed_pop_margin: float = 0.2  # no slider: ceiling never exceeds seed popularity + margin
    # fit curve widths (gaussian denominators)
    hop_width: float = 1.2
    pop_width: float = 0.12
    artist_pop_width: float = 0.03
    unknown_artist_pop_fit: float = 0.7
    # graph
    source_weights: dict[str, float] = field(default_factory=lambda: {
        "lb": 1.0, "deezer": 0.85, "mb": 0.6, "tag": 0.5, "radio": 0.5, "genre": 0.9, "yandex": 0.85})
    agreement_bonus: float = 1.15
    blend_bonus: float = 1.3  # several seeds: affinity multiplier per further seed reaching an artist
    graph_cap: int = 160
    fanout: dict[int, int] = field(default_factory=lambda: {1: 14, 2: 8, 3: 6})
    frontier_base: int = 16  # frontier size at hop h = max(frontier_min, frontier_base // h)
    frontier_min: int = 4
    sparse_graph: int = 8  # fewer hop-1 artists -> tag fallback
    tag_neighbours: int = 20
    tag_radio_recordings: int = 60
    genre_seed_artists: int = 30  # genre seeds: hop-1 artists per genre source
    genre_seed_tracks: int = 100  # genre seeds: tagged recordings injected as candidates
    genre_recordings_per_artist: int = 3  # tag search fetches limit * this recordings to find `limit` artists
    # candidates
    shortlist_min: int = 40
    candidate_artists_min: int = 18
    radio_neighbours: int = 3  # radio is pulled for the seed + this many hop-1 artists
    top_tracks_limit: int = 100
    junk_title_pattern: str = (r"^\[|\b(live|demo|remix|edit|instrumental|acoustic version|karaoke|session"
                               r"|rehearsal|bootleg|version|take \d+|dub|mix|unplugged|débranché|phase corrected"
                               r"|tribute|originally performed|in the style of|re-recording|re-recorded"
                               r"|from the (series|film|movie|motion picture|game))\b"
                               r"|\(\d{4}|\d{4}[‐-]\d\d")
    # ranking
    familiarity_strength: float = 0.9  # known artist weight *= 1 - strength * explore * familiarity
    familiarity_saturation: int = 200  # plays at which an artist counts as fully familiar
    heard_skip_explore: float = 0.3  # at/above this explore, already-played tracks are dropped
    heard_penalty: float = 0.6  # below it they're multiplied by this
    multi_source_bonus: float = 0.2
    jitter: float = 0.3
    shuffle_band: int = 10
    max_source_share: float = 0.5
    # taste (loves / hates / ratings)
    love_factor: float = 1.5  # weight *= love_factor ** score   for score in (0, 1]
    hate_factor: float = 0.3  # weight *= hate_factor ** -score  for score in [-1, 0)
    feedback_track_cap: int = 50  # strongest track opinions that feed the track-similarity layer
    similar_recordings_limit: int = 50
    seed_track_similar_limit: int = 50  # track seeds inject this many similar recordings
    seed_track_boost: float = 1.0  # injected seed-similar tracks: score *= 1 + boost * similarity
    seed_track_affinity: float = 0.8  # graph affinity for artists reached only via the seed track
    feedback_max_age_s: int = 900  # re-read loves/ratings after this long
    history_refresh_s: int = 86400  # long-running processes reload listening history this often
    feedback_store_depth: int = 5000  # imported-JSON log: only the newest N opinions are read
    # provider scales
    deezer_fans_full_scale: float = 6.9  # log10 fans of a superstar
    deezer_rank_full_scale: int = 900_000
    lb_listeners_full_scale: float = 5.5  # log10 LB listeners of a superstar
    lb_min_listener_share: float = 0.002  # drop recordings below this share of the artist's top track
    troi_window_width: int = 34
    # genre graph (postradio/genres.py)
    genre_kind_weights: dict[str, float] = field(default_factory=lambda: {
        "subgenre_of": 1.0,  # Wikidata P279
        "influenced_by": 0.5,  # Wikidata P737/P941
        "yandex_parent": 0.3})  # Yandex genre tree
    genre_fallback_scale: float = 0.2  # with learned weights, structural edges fill uncovered genres at this scale
    genre_spread_hops: int = 2  # a taste profile spreads this many hops over the genre graph...
    genre_spread_decay: float = 0.5  # ...losing this factor per hop (times the edge weight)
    # flow mode (postradio/flow.py)
    flow_half_life_days: float = 365.0  # taste profile: a like this old counts half as much as the newest; 0 = off
    flow_profile_artists: int = 150  # artists whose genres are looked up for the profile (most-liked first)
    flow_profile_api: bool = False  # with the store installed, also ask the MusicBrainz API (1-4 s each) for artists
    #   the store doesn't know or has no genres for; off = store only (Yandex genres still cover them)
    flow_source_shares: dict[str, float] = field(default_factory=lambda: {
        "mb": 0.7,  # MusicBrainz artist genres (store first, then the API)
        "yandex": 0.3})  # Yandex album genres of the liked tracks
    flow_genre_rarity: float = 1.0  # profile: weight genres by rarity among artists (store); 0 = off
    flow_parent_discount: float = 0.3  # profile: a genre next to one of its subgenres counts this much; 1 = off
    # explore sets these on a straight line from (value at explore 0, value at explore 1); explicit call args win
    flow_stops: tuple[float, float] = (2, 5)  # genre stops on the path (rounded)
    flow_taste_weight: tuple[float, float] = (2.0, 0.7)  # step score = link weight x closeness ** this
    flow_min_link_weight: tuple[float, float] = (0.07, 0.07)  # genre links weaker than this are not walked
    flow_min_genre_weight: float = 0.23  # genres below this weight are left out of the walk (see docs)
    flow_radius: int = 0  # >0: every stop within this many links of the start; 0 = off
    flow_new_artists: int = 30  # new-artist candidates fetched per stop (per genre source)
    flow_familiar_artists: int = 12  # liked artists considered per stop
    flow_tracks_per_artist: int = 5  # an artist's best-fitting tracks kept as candidates
    flow_artist_gap: int = 4  # the same artist is kept at least this many tracks apart where possible
    # rate limits (per host; "default" covers the rest)
    min_interval_s: dict[str, float] = field(default_factory=lambda: {
        "musicbrainz.org": 1.1,  # hard limit 1 req/s
        "query.wikidata.org": 1.0,  # data build only; heavy queries, be polite
        "api.deezer.com": 0.12,  # documented ~50 req / 5 s
        "api.listenbrainz.org": 0.34,  # 30 req / 10 s window, also tracked via X-RateLimit headers
        "labs.api.listenbrainz.org": 0.1,
        "default": 0.05})
    max_in_flight: dict[str, int] = field(default_factory=lambda: {
        "musicbrainz.org": 1, "query.wikidata.org": 1, "api.deezer.com": 6, "api.listenbrainz.org": 4,
        "labs.api.listenbrainz.org": 4, "default": 4})
    max_attempts: dict[str, int] = field(default_factory=lambda: {"musicbrainz.org": 5, "default": 4})
    ratelimit_floor: int = 2  # LB: when X-RateLimit-Remaining drops to this, wait for the window to reset
    fetch_workers: int = 8  # threads for independent fetches (catalogues, neighbours, similar recordings)
    backoff_cap_s: float = 30.0  # MB worst case: 2+4+8+16 s of waiting across 5 attempts
    http_cache_keep_s: int = 30 * 86400  # cache rows older than this are deleted (stale fallback window)


@dataclass
class Config:
    """`base_dir` is not a YAML key: load() sets it to the config file's folder, so relative paths in the
    file resolve there."""
    defaults: Defaults = field(default_factory=Defaults)
    storage: Storage = field(default_factory=Storage)
    sources: Sources = field(default_factory=Sources)
    feedback: FeedbackCfg = field(default_factory=FeedbackCfg)
    server: Server = field(default_factory=Server)
    advanced: Advanced = field(default_factory=Advanced)
    base_dir: str | None = None

    def stream_mix(self) -> dict[str, float]:
        return dict(self.advanced.mix)

    def path(self, p: str, base: Path) -> Path:
        """A configured path: absolute, ~-expanded, or relative to the config file's folder (else `base`)."""
        q = Path(os.path.expanduser(p))
        return q if q.is_absolute() else Path(self.base_dir or base) / q


@dataclass(frozen=True)
class Dirs:
    cache: Path  # disposable
    data: Path  # re-downloadable (postradio data fetch)
    state: Path  # user data: saved playlists, imported loves


def user_dirs() -> Dirs:
    """The user's own folders (XDG on Linux; the same layout on macOS), used when no home is given."""
    home = Path.home()
    cache = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache") / NAME
    share = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share") / NAME
    return Dirs(cache, share / "data", share)


def resolve_dirs(home: str | Path | None = None, cache_dir: str | Path | None = None,
                 data_dir: str | Path | None = None, state_dir: str | Path | None = None) -> Dirs:
    """Explicit folders win; then `home` (cache/, data/, state/ under it); then the user's own folders."""
    base = (Dirs(Path(home).expanduser() / "cache", Path(home).expanduser() / "data",
                 Path(home).expanduser() / "state") if home else user_dirs())

    def pick(x, d):
        return Path(x).expanduser() if x else d

    return Dirs(pick(cache_dir, base.cache), pick(data_dir, base.data), pick(state_dir, base.state))


def find_config(env: Mapping[str, str], explicit: str | Path | None = None) -> Path | None:
    """CLI / server only (a library caller passes a Config or a path). Explicit --config > $POSTRADIO_CONFIG >
    $POSTRADIO_HOME/config.yaml > ./config.yaml > the user's config folder. None = built-in defaults."""
    if explicit:
        return Path(explicit).expanduser()
    if env.get(CONFIG_ENV):
        return Path(env[CONFIG_ENV]).expanduser()
    xdg = Path(env.get("XDG_CONFIG_HOME") or Path.home() / ".config") / NAME / "config.yaml"
    for cand in ([Path(env[HOME_ENV]).expanduser() / "config.yaml"] if env.get(HOME_ENV) else []) + [
            Path.cwd() / "config.yaml", xdg]:
        if cand.is_file():
            return cand
    return None


def _merge(obj, data: dict, where: str):
    """where = dotted path of `obj` in the YAML ("" for the root)."""
    def at(k):
        return f"{where}.{k}" if where else k

    if not isinstance(data, dict):
        raise ConfigError(f"config: '{where or 'top level'}' must be a mapping")
    known = {f.name: f for f in fields(obj) if not (where == "" and f.name == "base_dir")}
    updates = {}
    for k, v in data.items():
        if k not in known:
            raise ConfigError(f"config: unknown key '{at(k)}'")
        cur = getattr(obj, k)
        if is_dataclass(cur):
            v = _merge(cur, v, at(k))
        elif isinstance(cur, dict):
            if not isinstance(v, dict):
                raise ConfigError(f"config: '{at(k)}' must be a mapping")
            for kk, vv in v.items():
                sample = next(iter(cur.values()), None)
                if sample is not None:
                    _check_type(sample, vv, f"{at(k)}.{kk}")
            v = {**cur, **v}  # partial override of e.g. source_weights
        elif isinstance(cur, tuple):
            if not isinstance(v, list | tuple):
                raise ConfigError(f"config: '{at(k)}' must be a list")
            v = tuple(v)
        elif cur is not None:
            _check_type(cur, v, at(k))
        updates[k] = v
    return replace(obj, **updates)


def _check_type(default, value, where: str) -> None:
    """The YAML value must have the default's type (ints are fine where floats are expected)."""
    if value is None:
        return  # null always means "use the fallback" for optional settings
    if isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, int):
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif isinstance(default, float):
        ok = isinstance(value, int | float) and not isinstance(value, bool)
    else:
        ok = isinstance(value, type(default))
    if not ok:
        raise ConfigError(f"config: '{where}' must be {type(default).__name__}, got {value!r}")


STREAMS = ("deezer-top", "lb", "deezer-radio", "troi", "track-sim", "lb-tags")


def validate(c: "Config") -> "Config":
    """Ranges and enums that types alone can't express. Raises ValueError naming the key."""
    d = c.defaults
    problems = []
    if not 0 <= d.explore <= 1:
        problems.append("defaults.explore must be in [0, 1]")
    if d.popularity is not None and not 0 <= d.popularity <= 1:
        problems.append("defaults.popularity must be in [0, 1] or null")
    if d.n < 1:
        problems.append("defaults.n must be at least 1")
    if d.per_artist < 1:
        problems.append("defaults.per_artist must be at least 1")
    if d.unheard not in (None, "none", "tracks", "artists"):
        problems.append("defaults.unheard must be null, tracks or artists")
    if c.sources.history not in ("lb", "file", "yandex", "none"):
        problems.append("sources.history must be lb, file, yandex or none")
    if c.sources.history == "yandex" and not c.sources.yandex_playlist:
        problems.append("sources.history = yandex needs sources.yandex_playlist")
    if c.sources.yandex_tracks is not None and c.sources.yandex_tracks < 1:
        problems.append("sources.yandex_tracks must be at least 1 or null")
    for k, v in c.advanced.mix.items():
        if k not in STREAMS:
            problems.append(f"advanced.mix: unknown stream '{k}' (one of {', '.join(STREAMS)})")
        elif not 0 <= v <= 1:
            problems.append(f"advanced.mix.{k} must be in [0, 1]")
    if problems:
        raise ConfigError("config: " + "; ".join(problems))
    return c


def load(path: str | Path | None = None) -> Config:
    """A config file, or the built-in defaults when `path` is None. Nothing is looked up implicitly: the CLI
    finds its file with find_config(). Relative paths inside the file resolve against its folder."""
    if path is None:
        return Config()
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"config: {p} is not valid YAML: {e}") from e
    return replace(validate(_merge(Config(), data, "")), base_dir=str(p.resolve().parent))
