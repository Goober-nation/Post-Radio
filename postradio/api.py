"""Public library API: what Post-Vinyl (or the HTTP server) calls.

    rec = Recommender()                       # reads config.yaml / $POSTRADIO_CONFIG
    res = rec.generate(artist="Radiohead", explore=0.7)
    rec.save(res["result_id"])                 # user liked it -> JSON in storage.save_dir
    rec.deep_cuts()                            # LB Weekly Exploration, both weeks

Results live in an in-process TTL cache until saved; nothing touches disk unless save() is called.
"""
import difflib
import json
import logging
import random
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from . import config as config_mod
from . import data as data_mod
from .engine import Params, Providers, Radio
from .errors import ConfigError, InvalidInput, NotFound
from .genres import GenreGraph
from .http import Http
from .models import Like, Play, Track
from .providers import FeedbackProvider, HistoryProvider, NoHistory
from .providers.deezer import Deezer
from .providers.feedback import LBFeedback, NavidromeFeedback
from .providers.feedback_store import JsonFeedbackStore
from .providers.history import FileHistory, ListenBrainzHistory
from .providers.lb_playlists import KINDS, LBCreatedFor
from .providers.listenbrainz import LBSimilar, LBSimilarRecordings, LBTopTracks, TroiRadio, TroiTagRadio
from .providers.listener import LikesFeedback, MergedHistory, PlaysHistory
from .providers.musicbrainz import MusicBrainz
from .providers.musicbrainz_store import ArtistStore, GenreIndex
from .providers.yandex import YandexLikes, YandexSimilar, genre_table
from .results import DeepCuts, FlowPreview, GraphPreview, Playlist, PlaylistTrack, Profile, Progress, Seed, Week

_CONFIGURED = object()  # "use this Recommender's configured value" (None means "none")
SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class ResultCache:
    """Small thread-safe TTL + LRU cache for unsaved results."""

    def __init__(self, ttl_s: int, max_items: int):
        self.ttl, self.max = ttl_s, max_items
        self._d: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._d[key] = (time.monotonic(), value)
            self._d.move_to_end(key)
            while len(self._d) > self.max:
                self._d.popitem(last=False)

    def get(self, key: str) -> dict | None:
        with self._lock:
            item = self._d.get(key)
            if not item:
                return None
            if time.monotonic() - item[0] > self.ttl:
                del self._d[key]
                return None
            return item[1]


def new_seed() -> int:
    """A run seed small enough to type back in (--rng-seed)."""
    return random.randrange(1_000_000)


def slug(s: str) -> str:
    return SAFE_NAME.sub("-", s.strip().lower()).strip("-.")[:60] or "playlist"


@dataclass
class Listener:
    """One listener's taste for one call, so a single long-lived Recommender can serve many users. Given to
    generate / flow / profile, it replaces the configured taste and history for that call only.

    Data (a host app's own records, no credentials needed): `likes` (loves/stars, dislikes with a negative
    score; dated likes weigh more in flow) and `plays` (listening history).
    Sources Post-Radio fetches itself: `yandex_playlist` (a public playlist link: likes + history),
    `lb_user` (ListenBrainz loves/hates + all-time listening stats), plus any provider objects.
    `lb_token`: the listener's own ListenBrainz token for this call (LB catalogue as a track source, faster
    artist identity checks). Never stored or logged."""
    likes: list[Like] = field(default_factory=list)
    plays: list[Play] = field(default_factory=list)
    yandex_playlist: str | None = None
    yandex_tracks: int | None = None  # newest N playlist tracks; None = all
    lb_user: str | None = None
    lb_token: str | None = None
    feedback: list[FeedbackProvider] = field(default_factory=list)
    history: list[HistoryProvider] = field(default_factory=list)


def _track(t: Track) -> PlaylistTrack:
    return PlaylistTrack(t.artist.name, t.title, artist_mbid=t.artist.mbid, recording_mbid=t.recording_mbid,
                         deezer_id=t.deezer_id)


def _radio_playlist(res: dict, rng_seed: int) -> Playlist:
    tracks = [PlaylistTrack(r["artist"], r["title"], artist_mbid=r.get("artist_mbid"),
                            recording_mbid=r.get("recording_mbid"), deezer_id=r.get("deezer_id"),
                            popularity=r.get("popularity"), score=r.get("score"), sources=r.get("sources") or [],
                            familiar=bool(r.get("known_artist")), hop=r.get("hop"), path=r.get("path"),
                            taste=r.get("taste"), artist_popularity=r.get("artist_popularity"),
                            via=r.get("via")) for r in res["tracks"]]
    s = res["seed"]
    seed = Seed(s["artist"], s["artist_mbid"], s["track"], s.get("popularity"), res.get("genres"), res.get("genre_op"))
    seeds = [Seed(x["artist"], x["artist_mbid"], x["track"], x.get("popularity")) for x in res.get("seeds") or []]
    stats = {k: res.get(k) for k in ("mode", "mix", "unheard", "graph_size", "candidates", "taste")}
    return Playlist("radio", tracks, res["explore"], res["popularity"], rng_seed, seed=seed, seeds=seeds,
                    stats=stats)


def _flow_playlist(res: dict, rng_seed: int) -> Playlist:
    tracks = [PlaylistTrack(r["artist"], r["title"], artist_mbid=r.get("artist_mbid"),
                            recording_mbid=r.get("recording_mbid"), deezer_id=r.get("deezer_id"),
                            popularity=r.get("popularity"), score=r.get("score"), sources=r.get("sources") or [],
                            familiar=bool(r.get("familiar")), genres=r.get("genres") or [], stop=r.get("stop"),
                            artist_popularity=r.get("artist_popularity"), via=r.get("via"))
              for r in res["tracks"]]
    stats = {"stops": res.get("stops"), "candidates": res.get("candidates"), "graph_learned": res.get("graph_learned"),
             "input": res.get("input")}
    return Playlist("flow", tracks, res["explore"], res["popularity"], rng_seed, genre_path=res["path"],
                    plan=res.get("plan"),
                    profile=res["profile"], familiarity=res["familiarity"], flow_score=res["flow_score"], stats=stats)


class Recommender:
    def __init__(self, cfg: config_mod.Config | None = None, *, home: str | Path | None = None,
                 cache_dir: str | Path | None = None, data_dir: str | Path | None = None,
                 state_dir: str | Path | None = None, token: str | None = None, lb_user: str | None = None,
                 history: HistoryProvider | None = None, feedback: list[FeedbackProvider] | None = None,
                 offline: bool = False, env: Mapping[str, str] | None = None,
                 log: Callable[[str], None] | None = None):
        """cfg: a Config (config.load(path) reads a file); None = built-in defaults.

        Folders: explicit cache_dir / data_dir / state_dir > the config's storage paths > `home` (cache/,
        data/, state/ under it) > the user's own folders. Nothing is read from or written to the install
        location or the working directory.

        env: where to read LISTENBRAINZ_TOKEN, LISTENBRAINZ_USERNAME and NAVIDROME_* from. The library reads
        no environment by default (None = {}); the CLI passes os.environ. `token` and `lb_user` win over it;
        token="" means explicitly tokenless.

        Pass `history` / `feedback` to supply listening history or loves/hates from anywhere (e.g. a
        multi-scrobbler/Maloja adapter); otherwise both come from config."""
        log = log or logging.getLogger("postradio").info  # prose; progress events are separate (progress=)
        self.cfg = cfg or config_mod.Config()
        self.env = dict(env or {})
        s, a, st = self.cfg.sources, self.cfg.advanced, self.cfg.storage
        self.dirs = config_mod.resolve_dirs(home, cache_dir, data_dir, state_dir)
        self._base = Path(home).expanduser() if home else self.dirs.state  # relative paths in a code-built Config
        if data_dir is None and st.data_dir:
            self.dirs = replace(self.dirs, data=self.cfg.path(st.data_dir, self._base))
        self.token = (token if token is not None else self.env.get("LISTENBRAINZ_TOKEN")) or None
        if not s.use_lb_token:
            self.token = None
        self.lb_user = lb_user or s.lb_user or self.env.get("LISTENBRAINZ_USERNAME") or None
        http_path = self.cfg.path(st.http_cache, self._base) if st.http_cache else self.dirs.cache / "http.sqlite"
        self.http = Http(http_path, ttl=st.http_ttl_s, offline=offline,
                         min_interval=a.min_interval_s, max_attempts=a.max_attempts, backoff_cap_s=a.backoff_cap_s,
                         max_in_flight=a.max_in_flight, ratelimit_floor=a.ratelimit_floor, keep_s=a.http_cache_keep_s)
        self.log, self.offline = log, offline
        store_path = (self.cfg.path(st.feedback_file, self._base) if st.feedback_file
                      else self.save_dir / "feedback" / "opinions.jsonl")
        self.feedback_store = JsonFeedbackStore(store_path, self.cfg.advanced.feedback_store_depth)
        s_ = self.cfg.sources
        self.yandex = (YandexLikes(self.http, s_.yandex_playlist, s_.yandex_tracks, log=log)
                       if s_.yandex_playlist else None)
        self.history = history or self._history_from_config()
        self.feedback = feedback if feedback is not None else self._feedback_from_config()
        self.cache = ResultCache(self.cfg.storage.result_ttl_s, self.cfg.storage.result_cache_size)
        self.store = ArtistStore.open(self.data_dir)
        self.genre_index = None if self.store else GenreIndex.open(self.data_dir)
        if self.store:
            log(f"data: MusicBrainz artist store at {self.data_dir}")
        self.radio = Radio(self._providers(), self.cfg.advanced, log=log)
        self.created_for = LBCreatedFor(self.http, self.token)

    def _history_from_config(self) -> HistoryProvider:
        s = self.cfg.sources
        if s.history == "yandex":
            if not self.yandex:
                raise ConfigError("sources.history = yandex needs sources.yandex_playlist")
            return self.yandex.as_history()
        if s.history == "file":
            if not s.history_file:
                raise ConfigError("sources.history = file needs sources.history_file")
            return FileHistory(self.cfg.path(s.history_file, self._base))
        if s.history == "lb" and self.lb_user:
            return ListenBrainzHistory(self.http, self.lb_user, self.token,
                                       refresh_s=self.cfg.advanced.history_refresh_s)
        return NoHistory()

    def _feedback_from_config(self) -> list[FeedbackProvider]:
        f, a = self.cfg.feedback, self.cfg.advanced
        if not f.enabled:
            return []
        out: list[FeedbackProvider] = []
        names = list(f.sources)
        if self.yandex and "yandex" not in names:
            # a configured playlist is the user's likes; a source list written before `yandex` existed shouldn't
            # silently drop it (flow needs likes, not just history)
            names.insert(1 if names[:1] == ["json"] else 0, "yandex")
        for name in names:
            if name == "json":
                out.append(self.feedback_store)
            elif name == "lb":
                user = f.lb_user or self.lb_user
                if user:
                    out.append(LBFeedback(self.http, user, self.token, a.feedback_max_age_s))
            elif name == "yandex":
                if self.yandex:
                    out.append(self.yandex)
            elif name == "navidrome":
                url = f.navidrome_url or self.env.get("NAVIDROME_URL")
                user, pw = self.env.get("NAVIDROME_USERNAME"), self.env.get("NAVIDROME_PASSWORD")
                if url and user and pw:
                    out.append(NavidromeFeedback(self.http, url, user, pw, a.feedback_max_age_s))
                elif url:  # configured but unusable: say why; unconfigured is simply skipped
                    self.log("feedback: navidrome skipped (needs feedback.navidrome_url or $NAVIDROME_URL, "
                             "$NAVIDROME_USERNAME, $NAVIDROME_PASSWORD)")
            else:
                raise ConfigError(f"feedback.sources: unknown source '{name}' (json | yandex | navidrome | lb)")
        return out

    def _providers(self, token: str | None = None, history: HistoryProvider | None = None,
                   feedback: list[FeedbackProvider] | None = None, yandex: "YandexLikes | None | object" = _CONFIGURED
                   ) -> Providers:
        """The engine's sources. Defaults = this Recommender's configured taste, history and token; a Listener
        call passes its own."""
        http, a = self.http, self.cfg.advanced
        token = self.token if token is None else (token or None)
        history = self.history if history is None else history
        feedback = self.feedback if feedback is None else feedback
        yandex = self.yandex if yandex is _CONFIGURED else yandex
        mb = MusicBrainz(http, store=self.store, index=self.genre_index)
        lb_top = LBTopTracks(http, token, a.lb_listeners_full_scale, a.lb_min_listener_share) if token else None

        dz = Deezer(http, known_titles=mb.recording_titles, cheap_titles=lb_top.titles if lb_top else None,
                    fans_full_scale=a.deezer_fans_full_scale, rank_full_scale=a.deezer_rank_full_scale,
                    memo_ttl_s=self.cfg.storage.http_ttl_s)
        tracks = [dz] + ([lb_top] if lb_top else [])  # LB's popularity endpoint is the only one needing a token
        tags = TroiTagRadio(http, token, a.troi_window_width, a.genre_recordings_per_artist)
        similar = [LBSimilar(http), dz, mb] + ([YandexSimilar(http, yandex)] if yandex else [])
        mb_profile = mb.local() if self.store and not a.flow_profile_api else mb
        taggers = {"mb": mb_profile, **({"yandex": yandex} if yandex else {})}
        return Providers(resolver=mb, similar=similar, tracks=tracks, profile_taggers=taggers,
                         profile_resolver=mb_profile,
                         radios=[dz, TroiRadio(http, token, a.troi_window_width)], tagger=mb, tag_radio=tags,
                         history=history, feedback=feedback, track_similarity=LBSimilarRecordings(http),
                         genres=[tags, dz] + ([mb] if self.store or self.genre_index else []))

    def _radio_for(self, listener: "Listener | None", progress: Callable[[Progress], None] | None = None,
                   cancel=None, timeout_s: float | None = None) -> Radio:
        """The shared engine, or a per-call view of it with this listener's taste, history and token and/or
        this call's progress handler, cancel event and deadline."""
        if timeout_s is not None and timeout_s <= 0:
            raise InvalidInput("timeout_s must be positive")
        deadline = time.monotonic() + timeout_s if timeout_s else None
        if listener is None:
            if progress or cancel is not None or deadline:
                return self.radio.view(self.radio.p, progress, cancel, deadline)
            return self.radio
        a = self.cfg.advanced
        yandex = (YandexLikes(self.http, listener.yandex_playlist, listener.yandex_tracks, log=self.log)
                  if listener.yandex_playlist else None)
        token = (listener.lb_token or self.token) if self.cfg.sources.use_lb_token else None
        feedback: list[FeedbackProvider] = []
        if listener.likes:
            feedback.append(LikesFeedback(listener.likes))
        if yandex:
            feedback.append(yandex)
        if listener.lb_user:
            feedback.append(LBFeedback(self.http, listener.lb_user, token, a.feedback_max_age_s))
        feedback += listener.feedback
        histories: list[HistoryProvider] = list(listener.history)
        if listener.plays:
            histories.append(PlaysHistory(listener.plays))
        if yandex:
            histories.append(yandex.as_history())
        if listener.lb_user:
            histories.append(ListenBrainzHistory(self.http, listener.lb_user, token, refresh_s=a.history_refresh_s))
        history = MergedHistory(histories, self.log) if histories else NoHistory()
        return self.radio.view(self._providers(token or "", history, feedback, yandex), progress, cancel, deadline)

    # ------------------------------------------------------------------ radio
    def generate(self, *, artist: str | list[str] | None = None, track: str | list[str] | None = None,
                 artist_mbid: str | list[str] | None = None, recording_mbid: str | list[str] | None = None,
                 genres: list[str] | None = None, genre_op: str = "or", taste: bool = True,
                 explore: float | None = None, popularity: float | None = None, n: int | None = None,
                 per_artist: int | None = None, rng_seed: int | None = None, auto_popularity: bool = False,
                 unheard: str | None = None, mix: dict[str, float] | None = None, dry: bool = False,
                 keep: bool = False, listener: Listener | None = None,
                 progress: Callable[[Progress], None] | None = None, cancel=None,
                 timeout_s: float | None = None) -> Playlist | GraphPreview:
        """Unset knobs fall back to config defaults. auto_popularity=True ignores a configured default
        popularity so it follows explore. Without rng_seed a seed is picked and reported (`rng_seed` in the
        result), so any run can be repeated. dry=True previews the artist graph without picking tracks.
        Seeds: one kind per radio; artist / track / artist_mbid / recording_mbid may each be a list (up to 5),
        blended into one graph. taste=False: likes and dislikes don't change scores (disliked tracks and artists
        are still dropped). listener: this call's taste/history/token instead of the configured ones.
        progress: called with a Progress event at each stage (from worker threads; keep it quick).
        cancel (anything with is_set(), e.g. threading.Event) / timeout_s: stop the run with errors.Cancelled."""
        rng_seed = new_seed() if rng_seed is None else rng_seed
        d = self.cfg.defaults
        pop = popularity if popularity is not None else (None if auto_popularity else d.popularity)
        p = Params(explore=d.explore if explore is None else explore, n=d.n if n is None else n,
                   per_artist=d.per_artist if per_artist is None else per_artist,
                   seed=rng_seed, popularity=pop, unheard=self._unheard(unheard), mix=self._mix(mix),
                   taste=taste, adv=self.cfg.advanced)
        if p.n < 1 or p.per_artist < 1:
            raise InvalidInput("n and per_artist must be at least 1")
        for name, v in (("explore", p.explore), ("popularity", p.popularity)):
            if v is not None and not 0 <= v <= 1:
                raise InvalidInput(f"{name} must be in [0, 1]")
        if sum(bool(x) for x in (artist, track, artist_mbid, recording_mbid, genres)) != 1:
            raise InvalidInput("give exactly one seed: artist, track, artist_mbid, recording_mbid or genres")
        if genres:
            self._check_genres(genres)
        res = self._radio_for(listener, progress, cancel, timeout_s).generate(
            p, dry=dry, artist=artist, track=track, artist_mbid=artist_mbid, recording_mbid=recording_mbid,
            genres=genres, genre_op=genre_op)
        if dry:
            return GraphPreview(rng_seed, Seed(**{k: v for k, v in res["seed"].items() if k != "popularity"},
                                               genres=res["genres"]),
                                [Seed(**x) for x in res["seeds"]],
                                res["explore"], res["popularity"], res["mode"], res["max_hops"], res["target_hop"],
                                res["popularity_target"], res["graph_size"], res["hops"])
        return self._remember(_radio_playlist(res, rng_seed), self._radio_name(res), keep)

    def flow(self, *, explore: float | None = None, popularity: float | None = None, n: int | None = None,
             per_artist: int | None = None, stops: int | None = None, start: str | None = None,
             taste_weight: float | None = None, min_link_weight: float | None = None, radius: int | None = None,
             rng_seed: int | None = None, dry: bool = False,
             keep: bool = False, listener: Listener | None = None,
             progress: Callable[[Progress], None] | None = None, cancel=None,
             timeout_s: float | None = None) -> Playlist | FlowPreview:
        """A playlist from the user's own likes, no seed: a path of genre stops through the genre graph (explore =
        how far from their genres), filled with familiar and new artists. Needs likes (Yandex playlist,
        Navidrome stars, LB loves or imported loves). Without rng_seed a seed is picked and reported, so the
        same path/playlist can be had again; dry=True stops after the profile and the path (no tracks).
        start: the first genre stop (any genre of the graph); None = one of the listener's strongest.
        explore sets stops, taste_weight and min_link_weight (advanced.flow_*: straight lines between their values
        at explore 0 and 1) and the share of new artists; passing a value overrides it. radius (advanced) keeps
        every stop within that many links of the start. The result's `plan` lists every value used and warnings."""
        rng_seed = new_seed() if rng_seed is None else rng_seed
        d = self.cfg.defaults
        p = Params(explore=d.explore if explore is None else explore, n=d.n if n is None else n,
                   per_artist=d.per_artist if per_artist is None else per_artist, seed=rng_seed,
                   popularity=popularity if popularity is not None else d.popularity, stops=stops,
                   taste_weight=taste_weight, min_link_weight=min_link_weight, radius=radius,
                   start=self._graph_genre(start) if start else None, adv=self.cfg.advanced)
        if p.n < 1 or p.per_artist < 1 or (p.stops is not None and not 1 <= p.stops <= 20):
            raise InvalidInput("n and per_artist must be at least 1, stops 1..20")
        for name, v, lo, hi in (("taste_weight", taste_weight, 0, 5), ("min_link_weight", min_link_weight, 0, 1),
                                ("radius", radius, 0, 10)):
            if v is not None and not lo <= v <= hi:
                raise InvalidInput(f"{name} must be in [{lo}, {hi}]")
        for name, v in (("explore", p.explore), ("popularity", p.popularity)):
            if v is not None and not 0 <= v <= 1:
                raise InvalidInput(f"{name} must be in [0, 1]")
        res = self._radio_for(listener, progress, cancel, timeout_s).flow(p, self.genre_graph(), dry=dry)
        if dry:
            return FlowPreview(rng_seed, res["path"], res["explore"], res["popularity"], res["stops"],
                               res["tracks_per_stop"], res["familiarity"], res["graph_learned"], res["profile"],
                               res["nearby"], res.get("input"), res.get("plan"))
        pop = "auto" if p.popularity is None else f"{p.popularity:.2f}"
        name = f"{datetime.now(UTC):%Y-%m-%d}_flow_e{p.explore:.2f}_p{pop}"
        return self._remember(_flow_playlist(res, rng_seed), name, keep)

    def profile(self, listener: Listener | None = None, progress: Callable[[Progress], None] | None = None,
                cancel=None, timeout_s: float | None = None) -> Profile:
        """The taste profile flow mode would use: the user's genres and their nearest neighbours."""
        radio = self._radio_for(listener, progress, cancel, timeout_s)
        _, _, _, profile, spread, used = radio.profile(self.genre_graph())
        return Profile([{"genre": g, "share": s} for g, s in profile.items()],
                       [{"genre": s.genre, "score": s.score, "hop": s.hop} for s in spread if s.hop > 0][:20], used)

    def _graph_genre(self, name: str) -> str:
        """The genre graph's own spelling of `name`, or InvalidInput with the closest names."""
        known = {g.casefold(): g for g in self.genre_graph().nodes}
        if name.casefold() in known:
            return known[name.casefold()]
        close = difflib.get_close_matches(name.casefold(), list(known), n=3, cutoff=0.6)
        hint = f"; did you mean: {', '.join(known[c] for c in close)}?" if close else ""
        raise InvalidInput(f"unknown genre {name!r}{hint}")

    def _check_genres(self, genres: list[str]) -> None:
        """A genre no source knows can only give an empty radio: say so, with the closest known names."""
        known = {g.casefold(): g for g in self.genre_graph().nodes}
        for prov in self.radio.p.genres:
            known |= {g.casefold(): g for g in self.radio._safe(prov.supported_genres, default=[]) or []}
        for g in genres:
            if g.casefold() in known:
                continue
            if all(g in (self.radio._safe(prov.unsupported, [g]) or []) for prov in self.radio.p.genres):
                close = difflib.get_close_matches(g.casefold(), list(known), n=3, cutoff=0.6)
                hint = f"; did you mean: {', '.join(known[c] for c in close)}?" if close else ""
                raise InvalidInput(f"unknown genre {g!r}{hint}")

    def _mix(self, overrides: dict[str, float] | None) -> dict[str, float]:
        """advanced.mix stream caps with per-run overrides. Keys: deezer-top, lb, deezer-radio, troi,
        track-sim, lb-tags (underscores accepted)."""
        base = self.cfg.stream_mix()
        for k, v in base.items():
            if not 0 <= float(v) <= 1:
                raise InvalidInput(f"advanced.mix.{k} must be in [0, 1]")
        for k, v in (overrides or {}).items():
            key = k.replace("_", "-")
            if key not in base:
                raise InvalidInput(f"unknown mix stream '{k}' (one of {', '.join(base)})")
            if not 0 <= float(v) <= 1:
                raise InvalidInput(f"mix {k} must be in [0, 1]")
            base[key] = float(v)
        return base

    def _unheard(self, value: str | None) -> str | None:
        """None = config default; "none" switches it off for this run even if the config sets it."""
        v = self.cfg.defaults.unheard if value is None else value
        if v in (None, "none"):
            return None
        if v not in ("tracks", "artists"):
            raise InvalidInput("unheard must be 'none', 'tracks' or 'artists'")
        return v

    @staticmethod
    def _radio_name(res: dict) -> str:
        seed = res["seed"]["artist"] or ("genre-" + "-".join(res["genres"]))
        seed += f"-{res['seed']['track']}" if res["seed"]["track"] else ""
        pop = "auto" if res["popularity"] is None else f"{res['popularity']:.2f}"
        return f"{datetime.now(UTC):%Y-%m-%d}_{slug(seed)}_e{res['explore']:.2f}_p{pop}"

    # ------------------------------------------------------------------ LB created-for
    def deep_cuts(self, user: str | None = None, kind: str = "exploration", keep: bool = False) -> DeepCuts:
        """LB's own Weekly Exploration ('deep cuts') or Weekly Jams; each week a separate entry."""
        user = user or self.lb_user
        if not user:
            raise InvalidInput("deep_cuts needs an LB user (argument, sources.lb_user or $LISTENBRAINZ_USERNAME)")
        if kind not in KINDS:
            raise InvalidInput(f"kind must be one of {sorted(KINDS)}")
        weeks = self.created_for.playlists(user, KINDS[kind])
        res = DeepCuts(KINDS[kind], user, [Week(w.week, w.title, w.mbid, f"https://listenbrainz.org/playlist/{w.mbid}",
                                                [_track(t) for t in w.tracks]) for w in weeks])
        name = f"{datetime.now(UTC):%Y-%m-%d}_lb-{kind}_{slug(user)}"
        return self._remember(res, name, keep)

    # ------------------------------------------------------------------ feedback store
    def import_feedback(self, data: dict) -> dict:
        """Append a batch of loves/hates (see providers/feedback_store.py for the format); dated, newest wins."""
        return self.feedback_store.import_batch(data)  # InvalidInput on a bad batch; taste reloads on change

    def feedback_batches(self) -> list[dict]:
        return self.feedback_store.batches()

    # ------------------------------------------------------------------ cache & storage
    def _remember(self, res, default_name: str, keep: bool):
        """keep=True holds the result in memory under a result_id (for get() and save()); otherwise nothing
        is stored and result_id is None."""
        res.result_id = uuid.uuid4().hex[:12] if keep else None
        res.created_at, res.default_name = datetime.now(UTC).isoformat(timespec="seconds"), default_name
        if keep:
            self.cache.put(res.result_id, res)
        return res

    def get(self, result_id: str) -> Playlist | DeepCuts | None:
        return self.cache.get(result_id)

    @property
    def save_dir(self) -> Path:
        st = self.cfg.storage
        return self.cfg.path(st.save_dir, self._base) if st.save_dir else self.dirs.state / "playlists"

    @property
    def data_dir(self) -> Path:
        return self.dirs.data

    def genre_graph(self) -> GenreGraph:
        """The genre graph: packaged Wikidata structure plus the Yandex genre tree, ranked by learned weights
        when installed."""
        a = self.cfg.advanced
        return GenreGraph.load(a.genre_kind_weights, a.genre_fallback_scale, data_dir=self.data_dir,
                               yandex=genre_table())

    def save(self, result_id: str, name: str | None = None, overwrite: bool = False) -> Path:
        res = self.get(result_id)
        if res is None:
            raise NotFound(f"result {result_id} expired or unknown (kept {self.cfg.storage.result_ttl_s}s)")
        path = self.save_dir / f"{slug(name) if name else res.default_name}.json"
        if path.exists() and not overwrite:
            raise FileExistsError(path)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({**res.to_dict(), "saved_as": path.stem}, indent=2, ensure_ascii=False))
        tmp.replace(path)
        return path

    def list_saved(self) -> list[dict]:
        out = []
        for p in sorted(self.save_dir.glob("*.json")) if self.save_dir.exists() else []:
            try:
                d = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            n = len(d.get("tracks") or []) or sum(len(w["tracks"]) for w in d.get("weeks") or [])
            out.append({"name": p.stem, "type": d.get("kind") or d.get("type"), "created_at": d.get("created_at"),
                        "tracks": n})
        return out

    def load_saved(self, name: str) -> dict:
        """The saved JSON (Playlist.from_dict(...) turns a radio/flow one back into a Playlist)."""
        p = self.save_dir / f"{slug(name)}.json"
        if not p.exists():
            raise NotFound(f"no saved playlist '{name}'")
        return json.loads(p.read_text())

    # ------------------------------------------------------------------ optional data (storage.data_dir)
    def data_build(self, out_dir: str | Path = "dist", skip: list[str] | None = None, keep_dump: bool = False,
                   seeds: int = 1000, similar: int = 10) -> Path:
        """Build the data files into data_dir and package a release folder under out_dir (see data.py).
        Takes roughly 30 min and ~3 GB of free disk with every step."""
        opts = data_mod.BuildOptions(skip=frozenset(skip or ()), keep_dump=keep_dump, seeds=seeds, similar=similar)
        mb = MusicBrainz(self.http)  # no store: the build replaces it
        return data_mod.build(self.http, mb, self.data_dir, Path(out_dir), opts, log=self.log)

    def data_fetch(self, tag: str | None = None, store: bool = True) -> dict:
        """Install the newest (or `tag`) data release into data_dir, verified against its manifest."""
        if self.store:
            self.store.close()  # the file is replaced; reopened on the next Recommender
            self.store = None
        return data_mod.fetch(self.http, self.data_dir, tag, store=store, log=self.log)

    def data_check(self, tag: str | None = None) -> dict:
        """What `data_fetch` would do, downloading only the release's manifest."""
        return data_mod.check(self.http, self.data_dir, tag)

    def data_status(self) -> dict:
        return data_mod.status(self.data_dir)

    def close(self) -> None:
        """Release worker threads (and the cache connection). Use per-request Recommenders as `with` blocks."""
        self.radio.close()
        self.http.close()
        if self.store:
            self.store.close()

    def __enter__(self) -> "Recommender":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def with_config(self, **section_overrides) -> "Recommender":
        """Copy with e.g. defaults=replace(cfg.defaults, n=20) — handy for per-request tweaks. Close it when
        done (`with rec.with_config(...) as r:`), or its worker threads stay alive."""
        return Recommender(replace(self.cfg, **section_overrides), cache_dir=self.dirs.cache,
                           data_dir=self.dirs.data, state_dir=self.dirs.state, token=self.token or "",
                           lb_user=self.lb_user, history=self.history, feedback=self.feedback,
                           offline=self.offline, env=self.env, log=self.log)
