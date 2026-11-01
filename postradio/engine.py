"""Exploration radio: build an artist graph around a seed, then rank tracks by hop + popularity fit.

explore in [0, 1]: how far out in the graph (0 = seed and closest neighbours, 1 = ~3 hops).
popularity in [0, 1] (optional): 1 = hits by big artists, 0 = deep cuts by small ones.
  Unset, it follows explore and stays near the seed's own audience size.
Data in and out is models.* types only (no raw API payloads); every source sits behind a providers.*
interface, and every tunable comes from config.Advanced.
"""
import math
import random
import re
import threading
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import flow as flow_mod
from .brand import POOL_PREFIX
from .config import Advanced
from .errors import InvalidInput, NotFound
from .genres import GenreGraph
from .models import Artist, Edge, History, Taste, Track, is_mbid
from .providers import (
    ArtistResolver,
    FeedbackProvider,
    GenreSource,
    HistoryProvider,
    NoHistory,
    RadioSource,
    SimilarArtists,
    Tagger,
    TagRadio,
    TopTracks,
    TrackSimilarity,
)

_worker = threading.local()  # in_pool=True only on threads of a Radio's own fetch pool


def _mark_pool_thread() -> None:
    _worker.in_pool = True


@dataclass
class Params:
    explore: float = 0.5
    n: int = 50
    per_artist: int = 2
    seed: int | None = None
    popularity: float | None = None  # None = derived from explore
    unheard: str | None = None  # None | "tracks" (drop played tracks) | "artists" (also drop known artists)
    mix: dict[str, float] = field(default_factory=dict)  # stream -> 0..1 share of what it offers (missing = 1)
    stops: int = 5  # flow mode: genre stops along the path
    adv: Advanced = field(default_factory=Advanced)

    @property
    def max_hops(self) -> int:
        return 1 + round(self.adv.max_extra_hops * self.explore)

    @property
    def target_hop(self) -> float:
        return self.adv.hop_span * self.explore

    @property
    def pop(self) -> float:
        return self.popularity if self.popularity is not None else 1 - self.adv.pop_slope * self.explore

    @property
    def artist_pop_ceiling(self) -> float:
        a = self.adv
        if self.popularity is None:
            return a.ceiling_base - a.ceiling_slope * self.explore
        return a.slider_ceiling_base + a.slider_ceiling_slope * self.popularity

    @property
    def radio_mode(self) -> str:
        easy, medium = self.adv.radio_mode_thresholds
        return "easy" if self.explore < easy else "medium" if self.explore < medium else "hard"


def hop_fit(hop: int, p: Params) -> float:
    return math.exp(-((hop - p.target_hop) ** 2) / p.adv.hop_width)


def pop_fit(pop: float, p: Params) -> float:
    return math.exp(-((pop - p.pop) ** 2) / p.adv.pop_width)


def artist_pop_fit(pop: float | None, p: Params, seed_pop: float | None = None) -> float:
    if pop is None:
        return p.adv.unknown_artist_pop_fit
    ceiling = p.artist_pop_ceiling
    if p.popularity is None and seed_pop is not None:
        # without an explicit slider, stay roughly in the seed's league: an obscure seed shouldn't
        # drift to stadium acts just because they're 2 hops away
        ceiling = min(ceiling, seed_pop + p.adv.seed_pop_margin)
    # only penalise being *more* popular than the target; obscure is never punished at low explore
    return math.exp(-(max(0.0, pop - ceiling) ** 2) / p.adv.artist_pop_width)


@dataclass
class Node:
    artist: Artist
    hop: int
    affinity: float  # best-path product of edge weights from the seed
    via: set[str] = field(default_factory=set)
    parent: str | None = None  # key of the node this was reached from


class Graph:
    def __init__(self, agreement_bonus: float = Advanced.agreement_bonus):
        self.nodes: dict[str, Node] = {}
        self.agreement_bonus = agreement_bonus

    def add(self, artist: Artist, hop: int, affinity: float, source: str, parent: Node | None = None) -> Node:
        n = self.nodes.get(artist.key)
        if n is None:
            n = self.nodes[artist.key] = Node(artist, hop, affinity, parent=parent and parent.artist.key)
        else:
            n.artist.absorb(artist)
            if hop < n.hop or (hop == n.hop and affinity > n.affinity):
                n.hop, n.affinity = hop, max(affinity, n.affinity)
                n.parent = parent.artist.key if parent else n.parent
            if source not in n.via:
                n.affinity = min(1.0, n.affinity * self.agreement_bonus)
        n.via.add(source)
        return n

    def node_for(self, artist: Artist, hop: int, affinity: float, source: str, parent: Node | None) -> Node:
        """Existing node for this artist, or a new leaf (radio tracks reach outside the graph)."""
        n = self.nodes.get(artist.key)
        if n:
            n.artist.absorb(artist)
            return n
        return self.add(artist, hop, affinity, source, parent)

    def path(self, n: Node) -> list[str]:
        out = [n.artist.name]
        while n.parent and len(out) < 6:
            n = self.nodes[n.parent]
            out.append(n.artist.name)
        return out[::-1]


@dataclass
class Providers:
    resolver: ArtistResolver
    similar: list[SimilarArtists]
    tracks: list[TopTracks]
    radios: list[RadioSource] = field(default_factory=list)
    tagger: Tagger | None = None
    tag_radio: TagRadio | None = None
    history: HistoryProvider = field(default_factory=NoHistory)
    feedback: list[FeedbackProvider] = field(default_factory=list)  # priority order
    track_similarity: TrackSimilarity | None = None
    genres: list[GenreSource] = field(default_factory=list)
    # flow mode: genre sources for the taste profile, by name (keys of advanced.flow_source_shares)
    profile_taggers: dict[str, Tagger] = field(default_factory=dict)
    profile_resolver: ArtistResolver | None = None  # None = resolver


@dataclass
class Run:
    """Everything that belongs to one generate() call. Passed explicitly, never stored on Radio, so
    concurrent requests on one Radio can't see each other's seed, genres, boosts or snapshots."""
    genres: list[str] = field(default_factory=list)
    genre_op: str = "or"
    seed_pop: float | None = None
    track_boost: dict[str, float] = field(default_factory=dict)
    history: History | None = None  # one snapshot per run (no per-node version() checks)
    taste: Taste | None = None

    def boost(self, t: Track) -> float:
        return self.track_boost.get(t.recording_mbid or "", 0.0) or self.track_boost.get(t.key, 1.0)


class Radio:
    def __init__(self, providers: Providers, adv: Advanced | None = None, log: Callable[[str], None] = print):
        self.p, self.log = providers, log
        self.a = adv or Advanced()
        self.junk = re.compile(self.a.junk_title_pattern, re.IGNORECASE)
        self._history: History | None = None
        self._taste: Taste | None = None
        self._taste_version: tuple = ()
        self._executor: ThreadPoolExecutor | None = None
        self._pool_lock = threading.Lock()
        self._history_version: object = None
        self._load_lock = threading.Lock()  # one thread reloads history/taste; others wait, then reuse it
        self._warned_streams: set[str] = set()
        self.last_run: Run | None = None  # for inspection/debugging only; never read by the engine

    def history(self) -> History:
        with self._load_lock:
            version = self.p.history.version()
            if self._history is None or version != self._history_version:
                try:
                    loaded = self.p.history.load()
                except (OSError, ValueError, RuntimeError, KeyError, TypeError) as e:
                    # a broken history source degrades to "no personalisation", never a failed run
                    self.log(f"history unavailable: {e}")
                    loaded = History()
                self._history, self._history_version = loaded, version
                if self._history:
                    self.log(f"history: {self._history.summary()}")
            return self._history

    def _safe(self, fn, *args, default=None, what: str = ""):
        """Call a provider; on any error log it and return `default` (an empty result). One broken source
        or malformed payload costs that source's contribution, never the whole run."""
        try:
            return fn(*args)
        except Exception as e:  # noqa: BLE001 - provider boundary: anything a third-party API throws
            owner = getattr(getattr(fn, "__self__", None), "name", None) or type(getattr(fn, "__self__", fn)).__name__
            self.log(f"{what or owner}: skipped after {type(e).__name__}: {e}")
            return [] if default is None else default

    def _map(self, fn, items: list) -> list:
        """Run independent fetches concurrently (order kept), each isolated by _safe. Per-host limits live
        in Http, so this only overlaps network latency; it never raises the request rate past the APIs' limits."""
        def job(x):
            return self._safe(fn, x, what=getattr(fn, "__name__", "fetch"))

        if len(items) <= 1 or getattr(_worker, "in_pool", False):
            # already inside a pooled job: run inline, since resubmitting could deadlock a saturated pool
            return [job(x) for x in items]
        return list(self._pool().map(job, items))

    def _pool(self) -> ThreadPoolExecutor:
        """One pool per Radio, shared by graph building and the background taste load, so at most
        fetch_workers fetches run at once. _map called from inside a pooled job runs inline (no deadlock)."""
        with self._pool_lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=self.a.fetch_workers, thread_name_prefix=POOL_PREFIX,
                                                    initializer=_mark_pool_thread)
            return self._executor

    def close(self) -> None:
        """Stop the worker threads. Safe to call twice; a later fetch would start a fresh pool."""
        with self._pool_lock:
            ex, self._executor = self._executor, None
        if ex:
            ex.shutdown(wait=True)

    def __enter__(self) -> "Radio":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def taste(self) -> Taste:
        """Merged opinions, reloaded only when some feedback provider's version() changes."""
        with self._load_lock:
            return self._taste_locked()

    def _taste_locked(self) -> Taste:
        version = tuple(prov.version() for prov in self.p.feedback)
        if self._taste is None or version != self._taste_version:
            self._taste_version = version
            items = []
            for prov in self.p.feedback:
                try:
                    got = prov.load()
                except (RuntimeError, OSError, ValueError, KeyError, TypeError) as e:  # e.g. Navidrome down
                    self.log(f"feedback from {prov.name} unavailable: {e}")
                    continue
                self.log(f"feedback: {len(got)} opinions from {prov.name}")
                items += got
            self._taste = Taste(items, priority=[prov.name for prov in self.p.feedback])
            if self._taste:
                self.log(f"taste: {self._taste.summary()}")
        return self._taste

    def taste_factor(self, score: float) -> float:
        if score > 0:
            return self.a.love_factor ** score
        if score < 0:
            return self.a.hate_factor ** -score
        return 1.0

    def _track_layer(self, taste: Taste) -> dict[str, float]:
        """Loved/hated recordings spread to their LB-similar recordings, scaled by similarity."""
        boosts: dict[str, float] = {}
        sim = self.p.track_similarity
        if not sim:
            return boosts
        opinions = taste.strongest(self.a.feedback_track_cap)
        sims = self._map(lambda f: sim.similar(
            Track(f.title or "", Artist(f.artist, mbid=f.artist_mbid), recording_mbid=f.recording_mbid),
            self.a.similar_recordings_limit), opinions)
        for f, similar in zip(opinions, sims, strict=True):
            for t, w in similar:
                factor = self.taste_factor(f.score * w)
                for k in filter(None, (t.recording_mbid, t.key)):
                    boosts[k] = boosts.get(k, 1.0) * factor
        lo, hi = self.a.hate_factor, self.a.love_factor
        return {k: min(hi, max(lo, v)) for k, v in boosts.items()}

    # ---------- seed ----------
    def resolve(self, artist=None, track=None, artist_mbid=None, recording_mbid=None, genres=None,
                genre_op="or") -> tuple[Artist, Track | None]:
        r = self.p.resolver
        for mbid in (artist_mbid, recording_mbid):
            if mbid is not None and not is_mbid(mbid):
                raise InvalidInput(f"not an MBID (expected a UUID): {mbid!r}")
        if genres:
            if genre_op not in ("and", "or"):
                raise InvalidInput("genre_op must be 'and' or 'or'")
            label = f" {genre_op} ".join(genres)
            return Artist(label, virtual=True), None
        if recording_mbid or track:
            if recording_mbid:
                t = r.recording_by_mbid(recording_mbid)
            else:
                a, _, title = track.partition(" - ")
                if not title:
                    raise InvalidInput("track must look like 'Artist - Title'")
                t = r.find_recording(a.strip(), title.strip())
            if not t:
                raise NotFound(f"track not found: {recording_mbid or track}")
            return t.artist, t
        a = r.artist_by_mbid(artist_mbid) if artist_mbid else r.find_artist(artist) if artist else None
        if not a:
            raise NotFound(f"artist not found: {artist_mbid or artist}")
        return a, None

    # ---------- graph ----------
    def build_graph(self, seed: Artist, p: Params, run: Run | None = None) -> Graph:
        run = run or Run()
        a, cap = self.a, self.a.graph_cap
        g = Graph(a.agreement_bonus)
        g.add(seed, 0, 1.0, "seed")
        frontier = [g.nodes[seed.key]]
        budget = {h: cap * h / sum(range(1, p.max_hops + 1)) for h in range(1, p.max_hops + 1)}
        for hop in range(1, p.max_hops + 1):
            new: list[Node] = []
            start = len(g.nodes)
            k = a.fanout.get(hop, a.fanout[max(a.fanout)])
            jobs = [(src, prov) for src in frontier if not src.artist.virtual
                    for prov in self.p.similar if hop <= prov.max_hop]
            fetched = self._map(lambda job, k=k: job[1].neighbours(job[0].artist, k), jobs)  # network, in parallel
            edges_by_src: dict[str, list[Edge]] = defaultdict(list)
            for (src, _), edges in zip(jobs, fetched, strict=True):
                edges_by_src[src.artist.key] += edges
            for src in frontier:  # graph mutation stays single-threaded and in frontier order
                if len(g.nodes) - start >= budget[hop]:
                    break
                if src.artist.virtual:
                    new += self._genre_neighbours(g, src, p, run)
                    continue
                new += [self._link(g, src, e, hop) for e in edges_by_src[src.artist.key]]
            if hop == 1 and len(g.nodes) - 1 < a.sparse_graph and not seed.virtual:
                new += self._tag_neighbours(g, frontier[0], p)
            ranked = sorted({n.artist.key: n for n in new if n.hop == hop}.values(), key=lambda n: -n.affinity)
            frontier = ranked[: max(a.frontier_min, a.frontier_base // hop)]
        self.log(f"graph: {len(g.nodes)} artists, hops <= {p.max_hops}")
        return g

    def _genre_neighbours(self, g: Graph, seed: Node, p: Params, run: Run) -> list[Node]:
        genres, op = run.genres, run.genre_op
        out = []
        for prov in self.p.genres:
            missing = self._safe(prov.unsupported, genres)
            if missing:
                known = self._safe(prov.supported_genres, default=[])
                effect = "skipped (and)" if op == "and" else f"no artists for {missing}"
                self.log(f"genre: {prov.name} doesn't know {missing}: {effect}"
                         + (f"; it supports: {', '.join(known)}" if known else ""))
            found = self._safe(prov.genre_artists, genres, op, p.pop, self.a.genre_seed_artists)
            for i, ar in enumerate(found):
                w = self.a.source_weights.get("genre", 0.9) * (1 - 0.5 * i / max(1, len(found)))
                out.append(g.add(ar, 1, w, "genre", seed))
        self.log(f"genre seed: {len(out)} artists for {seed.artist.name}")
        return out

    def _link(self, g: Graph, src: Node, e: Edge, hop: int) -> Node:
        w = self.a.source_weights.get(e.source, 0.7)
        return g.add(e.target, hop, src.affinity * w * e.weight, e.source, src)

    def _tag_neighbours(self, g: Graph, seed: Node, p: Params) -> list[Node]:
        """Sparse seed: borrow neighbours from artists sharing its tags (tag radio)."""
        if not (self.p.tagger and self.p.tag_radio):
            return []
        tags = self._safe(self.p.tagger.tags, seed.artist)
        if not tags:
            return []
        found = self._safe(self.p.tag_radio.artists_for_tags, tags, p.pop, self.a.tag_radio_recordings)
        artists = [a for a in found if a.key != seed.artist.key]
        self.log(f"sparse seed: {len(artists)} tag neighbours from {tags}")
        w = self.a.source_weights["tag"]
        return [g.add(a, 1, w, "tag", seed) for a in artists[: self.a.tag_neighbours]]

    def artist_weight(self, n: Node, p: Params, run: Run | None = None) -> float:
        run = run or Run()
        taste = run.taste if run.taste is not None else self.taste()
        history = run.history if run.history is not None else self.history()
        w = n.affinity * hop_fit(n.hop, p) * artist_pop_fit(n.artist.popularity, p, run.seed_pop)
        w *= self.taste_factor(taste.artist(n.artist))
        listens = history.plays(n.artist)
        if listens:
            fam = min(1.0, math.log1p(listens) / math.log(self.a.familiarity_saturation))
            w *= 1 - self.a.familiarity_strength * p.explore * fam
        return w

    # ---------- candidates ----------
    def _top_tracks(self, artist: Artist) -> list[Track]:
        if artist.virtual:
            return []
        out = []
        for prov in self.p.tracks:
            tracks = self._safe(prov.top_tracks, artist, self.a.top_tracks_limit)
            out += [t for t in tracks if not self.junk.search(t.title)]
        return out

    def candidates(self, g: Graph, seed: Node, p: Params, seed_track: Track | None = None,
                   rng: random.Random | None = None, run: Run | None = None) -> list[tuple[Track, Node]]:
        rng = rng or random.Random()
        run = run or Run()
        # pass 1: graph-only weight picks a shortlist; fetching their catalogues also fills in artist
        # popularity, which pass 2 folds in (this is what keeps deep exploration off the mainstream)
        by_weight = sorted(g.nodes.values(), key=lambda n: -self.artist_weight(n, p, run))
        shortlist = by_weight[:max(self.a.shortlist_min, p.n)]
        fetched = self._map(lambda n: self._top_tracks(n.artist), shortlist)  # network, in parallel
        catalogue = {n.artist.key: tracks for n, tracks in zip(shortlist, fetched, strict=True)}
        nodes = sorted(shortlist, key=lambda n: -self.artist_weight(n, p, run))
        nodes = nodes[:max(self.a.candidate_artists_min, p.n // 2)]
        # every candidate is filed under an explicit stream label; caps apply per whole stream at the end
        streams: dict[str, list[tuple[Track, Node]]] = defaultdict(list)
        seed_bonus: dict[int, float] = {}
        for n in nodes:
            for t in catalogue[n.artist.key]:
                streams[next(iter(t.sources), "")].append((t, n))  # catalogue tracks carry one source each
        if seed_track and self.p.track_similarity:
            for t, w in self._safe(self.p.track_similarity.similar, seed_track, self.a.seed_track_similar_limit):
                hop = 0 if t.artist.key == seed.artist.key else 1
                tn = g.node_for(t.artist, hop, self.a.seed_track_affinity * w, "track-sim", seed)
                t.artist = tn.artist
                t.popularity = p.pop  # unknown; don't let the popularity fit decide these
                t.sources = {"track-sim"}  # one stream, one label: no unearned multi-source bonus
                seed_bonus[id(t)] = 1 + self.a.seed_track_boost * w  # applied only if the track survives the cap
                streams["track-sim"].append((t, tn))
        if seed.artist.virtual:
            for prov in self.p.genres:
                for t in self._safe(prov.genre_tracks, run.genres, run.genre_op, p.pop, self.a.genre_seed_tracks):
                    tn = g.node_for(t.artist, 1, self.a.source_weights.get("genre", 0.9), "genre", seed)
                    t.artist = tn.artist
                    streams["lb-tags"].append((t, tn))
        # radio around the seed and its closest neighbours brings in artists outside the graph
        radio_seeds = [] if seed.artist.virtual else [seed]
        jobs = [(n, prov) for n in radio_seeds + [n for n in nodes if n.hop == 1][: self.a.radio_neighbours]
                for prov in self.p.radios]
        # one RNG per stream, derived from the run seed: reproducible regardless of thread timing
        seeds = [rng.random() for _ in jobs]
        fetched_radio = self._map(lambda ij: ij[1][1].radio(ij[1][0].artist, p.pop, p.radio_mode,
                                                            random.Random(seeds[ij[0]])), list(enumerate(jobs)))
        for (n, _), tracks in zip(jobs, fetched_radio, strict=True):
            for t in tracks:
                hop = 0 if t.artist.key == seed.artist.key else n.hop + 1
                rn = g.node_for(t.artist, hop, n.affinity * self.a.source_weights["radio"], "radio", n)
                t.artist = rn.artist
                streams[next(iter(t.sources), "")].append((t, rn))
        out: list[tuple[Track, Node]] = []
        for name, entries in streams.items():
            kept = self._cut(entries, name, p)
            if name == "track-sim":  # a capped-away similar recording must leave no trace, even via other streams
                for t, _ in kept:
                    for k in filter(None, (t.recording_mbid, t.key)):
                        run.track_boost[k] = run.track_boost.get(k, 1.0) * seed_bonus[id(t)]
            out += kept
        return out

    # streams whose order already means something are cut by order, not by popularity distance
    ORDERED_STREAMS = {"track-sim"}  # LB returns similar recordings most-similar first

    def _cut(self, entries: list[tuple[Track, Node]], stream: str, p: Params) -> list[tuple[Track, Node]]:
        """Apply the stream's cap to the whole stream: keep round(len * cap) candidates. Most streams keep
        those nearest the popularity target (so a low -p still reaches deep cuts); ordered streams keep the
        first ones. A stream with no configured cap is kept whole, with a one-time warning."""
        if stream not in p.mix:
            if stream not in self._warned_streams:
                self._warned_streams.add(stream)
                self.log(f"mix: no cap configured for stream '{stream}', keeping all of it")
            return entries
        m = p.mix[stream]
        if m >= 1.0:
            return entries
        cap = round(len(entries) * max(0.0, m))
        if stream in self.ORDERED_STREAMS:
            return entries[:cap]
        return sorted(entries, key=lambda e: abs(e[0].popularity - p.pop))[:cap]

    # ---------- ranking ----------
    def generate(self, p: Params, dry: bool = False, **seed_args) -> dict:
        """dry=True: resolve the seed and build the artist graph only (no catalogues, no track picks)."""
        rng = random.Random(p.seed)
        run = Run(genres=list(seed_args.get("genres") or []), genre_op=seed_args.get("genre_op") or "or")
        seed_artist, seed_track = self.resolve(**seed_args)
        self.log(f"seed: {seed_artist.name} ({seed_artist.mbid})" + (f" / {seed_track.title}" if seed_track else ""))

        def personal():  # history + taste don't affect graph building, so they load alongside it
            taste = self.taste()
            return self.history(), taste, self._safe(self._track_layer, taste, default={}) if taste else {}

        if dry:
            g = self.build_graph(seed_artist, p, run)
            return self._preview(g, seed_artist, seed_track, p, run)

        with ThreadPoolExecutor(max_workers=1) as bg:
            pending = bg.submit(personal)
            g = self.build_graph(seed_artist, p, run)
            seed = g.nodes[seed_artist.key]
            if not seed.artist.virtual:
                self._top_tracks(seed.artist)  # fills seed popularity before anything is weighted
            run.history, run.taste, run.track_boost = pending.result()  # joined before anything is weighted
        history, taste = run.history, run.taste
        run.seed_pop = seed.artist.popularity
        self.last_run = run
        cands = self.candidates(g, seed, p, seed_track, random.Random(rng.random()), run)

        merged: dict[str, tuple[Track, Node]] = {}
        for t, n in cands:
            if self.junk.search(t.title):
                continue
            if t.key in merged:
                old = merged[t.key][0]
                old.sources |= t.sources
                old.popularity = max(old.popularity, t.popularity)
                old.recording_mbid = old.recording_mbid or t.recording_mbid
                old.deezer_id = old.deezer_id or t.deezer_id
            else:
                merged[t.key] = (t, n)
        if seed_track:
            merged.pop(seed_track.key, None)

        scored = []
        for t, n in merged.values():
            if n.artist.virtual:
                continue
            if p.unheard and history.heard(t):
                continue
            if p.unheard == "artists" and history.plays(n.artist):
                continue
            pen = 1.0
            if history.heard(t):
                if p.explore >= self.a.heard_skip_explore:
                    continue
                pen = self.a.heard_penalty
            s = self.artist_weight(n, p, run) * pop_fit(t.popularity, p) * pen
            s *= self.taste_factor(taste.track(t)) * run.boost(t)
            s *= 1 + self.a.multi_source_bonus * (len(t.sources) - 1)
            s *= 1 - self.a.jitter / 2 + self.a.jitter * rng.random()  # keep repeated runs from being identical
            scored.append((s, t, n))
        scored.sort(key=lambda x: -x[0])

        picked = self._select(scored, p)
        band = self.a.shuffle_band
        bands = [picked[i:i + band] for i in range(0, len(picked), band)]
        for b in bands:  # gentle shuffle so the list doesn't read as a strict ranking
            rng.shuffle(b)
        return {
            "seed": {"artist": None if seed_artist.virtual else seed_artist.name, "artist_mbid": seed_artist.mbid,
                     "track": seed_track.title if seed_track else None, "popularity": run.seed_pop},
            "genres": run.genres or None, "genre_op": run.genre_op if run.genres else None,
            "explore": p.explore, "popularity": p.popularity, "unheard": p.unheard, "mode": p.radio_mode,
            "mix": {k: v for k, v in p.mix.items() if v < 1} or None,
            "graph_size": len(g.nodes),
            "candidates": len(merged),
            "tracks": [self._row(s, t, n, g, history, taste, run.boost(t)) for b in bands for (s, t, n) in b],
        }

    def _preview(self, g: Graph, seed_artist: Artist, seed_track: Track | None, p: Params, run: Run,
                 per_hop: int = 12) -> dict:
        """Dry radio: the artist graph by hop, strongest first, with how each artist was reached."""
        by_hop: dict[int, list[Node]] = defaultdict(list)
        for n in g.nodes.values():
            if n.hop > 0:
                by_hop[n.hop].append(n)
        return {
            "dry": True, "rng_seed": p.seed,
            "seed": {"artist": None if seed_artist.virtual else seed_artist.name, "artist_mbid": seed_artist.mbid,
                     "track": seed_track.title if seed_track else None},
            "genres": run.genres or None, "explore": p.explore, "popularity": p.popularity, "mode": p.radio_mode,
            "max_hops": p.max_hops, "target_hop": round(p.target_hop, 2), "popularity_target": round(p.pop, 2),
            "graph_size": len(g.nodes),
            "hops": {h: {"artists": len(ns), "top": [{"artist": n.artist.name, "affinity": round(n.affinity, 3),
                                                     "via": sorted(n.via), "path": g.path(n)}
                                                    for n in sorted(ns, key=lambda n: -n.affinity)[:per_hop]]}
                     for h, ns in sorted(by_hop.items())},
        }

    def _select(self, scored, p: Params):
        """Greedy by score with a per-artist cap. A per-source cap keeps one source from dominating,
        but only as a preference: if it leaves the list short, a second pass fills it ignoring that cap."""
        per_artist: dict[str, int] = defaultdict(int)
        per_source: dict[str, int] = defaultdict(int)
        max_source = math.ceil(p.n * self.a.max_source_share)
        picked, taken = [], set()
        for enforce_source_cap in (True, False):
            for i, (s, t, n) in enumerate(scored):
                if len(picked) >= p.n:
                    break
                only = next(iter(t.sources)) if len(t.sources) == 1 else None
                if i in taken or per_artist[n.artist.key] >= p.per_artist:
                    continue
                if enforce_source_cap and only and per_source[only] >= max_source:
                    continue
                taken.add(i)
                per_artist[n.artist.key] += 1
                if only:
                    per_source[only] += 1
                picked.append((s, t, n))
        picked.sort(key=lambda x: -x[0])
        return picked

    @staticmethod
    def _row(s: float, t: Track, n: Node, g: Graph, history: History, taste: Taste, boost: float) -> dict:
        return {
            "artist": t.artist.name, "title": t.title, "score": round(s, 4), "hop": n.hop,
            "popularity": round(t.popularity, 2), "artist_popularity": n.artist.popularity,
            "sources": sorted(t.sources), "recording_mbid": t.recording_mbid, "deezer_id": t.deezer_id,
            "path": g.path(n), "via": sorted(n.via), "known_artist": bool(history.plays(n.artist)),
            "taste": {"artist": round(taste.artist(n.artist), 2), "track": taste.track(t),
                      "similar_boost": round(boost, 2)},
        }

    # ---------- flow mode ----------
    def profile(self, graph: GenreGraph):
        """The user's genre profile from their dated likes: (top artists, resolved artists, per-source artist
        genres, {genre: share}, spread over the graph). Raises InvalidInput when there's nothing to go on."""
        a = self.a
        likes = [x for prov in self.p.feedback for x in self._safe(prov.liked, default=[])]
        if not likes:
            raise InvalidInput("flow needs likes: a Yandex playlist, Navidrome stars, LB loves or imported loves")
        weights = flow_mod.artist_weights(((f.artist, at) for f, at in likes), a.flow_half_life_days)
        top = sorted(weights, key=lambda k: (-weights[k], k))[: a.flow_profile_artists]
        weights = {k: weights[k] for k in top}
        resolver = self.p.profile_resolver or self.p.resolver
        calls_before = getattr(getattr(resolver, "http", None), "calls", 0)
        resolved = dict(zip(top, self._map(resolver.find_artist, top), strict=True))
        sources = []
        for name, tagger in self.p.profile_taggers.items():
            share = a.flow_source_shares.get(name, 0.0)
            if share <= 0:
                continue
            arts = [resolved.get(x) or Artist(x) for x in top]
            tags = self._map(lambda ar, t=tagger: t.tags(ar, 5), arts)
            got = {x: g for x, g in zip(top, tags, strict=True) if g}
            self.log(f"profile: {name} genres for {len(got)} of {len(top)} artists")
            sources.append((got, share))
        calls = getattr(getattr(resolver, "http", None), "calls", 0) - calls_before
        self.log(f"profile: {sum(1 for v in resolved.values() if v)} of {len(top)} artists resolved, "
                 f"{calls} network calls")
        profile = flow_mod.taste_profile(weights, sources)
        if not profile:
            raise InvalidInput("flow: no genres found for the liked artists")
        self.log("profile: " + ", ".join(f"{g} {s:.2f}" for g, s in list(profile.items())[:8]))
        spread = graph.spread(profile, a.genre_spread_hops, a.genre_spread_decay)
        return top, resolved, sources, profile, spread

    def flow(self, p: Params, graph: GenreGraph, dry: bool = False) -> dict:
        """A playlist with no seed: the user's taste profile (from their likes) spread over the genre graph, a
        path of p.stops genre stops (explore decides how far it leaves their genres), each stop filled with
        familiar and new artists' tracks, scored like generate() (popularity fit, taste, jitter)."""
        a, rng = self.a, random.Random(p.seed)
        cfg = flow_mod.PlannerConfig(destination_from=a.flow_destination_from,
                                     destination_width=a.flow_destination_width,
                                     destination_reach=a.flow_destination_reach)
        history, taste = self.history(), self.taste()
        top, resolved, sources, profile, spread = self.profile(graph)
        roots = {g: n.roots for g, n in graph.nodes.items()}
        path = flow_mod.plan_path(spread, graph.adj, p.explore, p.stops, random.Random(rng.random()), roots, cfg)
        self.log("path: " + " -> ".join(path))
        counts = flow_mod.allocate(p.n, len(path))
        if dry:  # preview: profile and path only, no artist pools or catalogues
            return {"dry": True, "rng_seed": p.seed, "path": path, "explore": p.explore, "popularity": p.popularity,
                    "stops": p.stops, "tracks_per_stop": counts, "familiarity": flow_mod.familiarity(path, spread),
                    "graph_learned": graph.learned,
                    "profile": [{"genre": g, "share": s} for g, s in list(profile.items())[:20]],
                    "nearby": [{"genre": s.genre, "score": s.score, "hop": s.hop} for s in spread if s.hop > 0][:20]}

        # familiar artists per stop: liked artists whose genres include the stop, most-liked first
        artist_genres: dict[str, list[str]] = {}
        for genres_of, _ in sources:
            for x, gs in genres_of.items():
                artist_genres.setdefault(x, [])
                artist_genres[x] += [g for g in gs if g not in artist_genres[x]]
        pools_artists: dict[str, list[tuple[Artist, list[str], bool]]] = {}
        for g, k in zip(path, counts, strict=True):
            # about 3 candidates per wanted track of each kind keeps catalogue fetches proportional to n
            new_k = round(k * p.explore)
            fam_cap = min(a.flow_familiar_artists, 3 * max(1, k - new_k))
            new_cap = min(a.flow_new_artists, 3 * max(1, new_k))
            fam = [(resolved.get(x) or Artist(x), artist_genres.get(x, []), True) for x in top
                   if g in artist_genres.get(x, [])][:fam_cap]
            new: dict[str, tuple[Artist, list[str], bool]] = {}
            for prov in self.p.genres:
                if self._safe(prov.unsupported, [g]):
                    continue
                for ar in self._safe(prov.genre_artists, [g], "or", p.pop, new_cap * 2):
                    known = history.plays(ar) or taste.artist(ar) > 0
                    new.setdefault(ar.key, (ar, [g], bool(known)))
            fam_keys = {f[0].key for f in fam}
            pools_artists[g] = fam + [v for v in new.values() if v[0].key not in fam_keys][:new_cap]

        everyone = {ar.key: ar for arts in pools_artists.values() for ar, _, _ in arts}
        catalogues = dict(zip(everyone, self._map(self._top_tracks, list(everyone.values())), strict=True))
        pools: dict[str, list[flow_mod.Pick]] = {}
        for g, arts in pools_artists.items():
            picks = []
            for ar, gs, familiar in arts:
                scored = []
                for t in catalogues.get(ar.key) or []:
                    if history.heard(t) or taste.track(t):  # liked tracks don't come back; hated ones never
                        continue
                    s = pop_fit(t.popularity, p) * artist_pop_fit(t.artist.popularity or ar.popularity, p)
                    s *= self.taste_factor(taste.artist(ar)) * self.taste_factor(taste.track(t))
                    s *= 1 - a.jitter / 2 + a.jitter * rng.random()
                    scored.append(flow_mod.Pick(t, gs or [g], familiar, s))
                picks += sorted(scored, key=lambda c: -c.score)[: a.flow_tracks_per_artist]
            pools[g] = picks
        stops = flow_mod.fill(path, pools, counts, p.explore, p.per_artist)
        tracks = flow_mod.order(stops, path, a.flow_artist_gap)
        self.log(f"flow: {len(tracks)} tracks over {len(path)} stops")
        return {
            "path": path, "explore": p.explore, "popularity": p.popularity, "stops": p.stops,
            "profile": [{"genre": g, "share": s} for g, s in list(profile.items())[:20]],
            "familiarity": flow_mod.familiarity(path, spread),
            "flow_score": flow_mod.flow_score(tracks, graph.adj),
            "graph_learned": graph.learned,
            "candidates": sum(len(v) for v in pools.values()),
            "tracks": [{"artist": c.artist, "title": c.title, "stop": c.stop, "genres": c.genres[:3],
                        "familiar": c.familiar, "score": round(c.score, 4),
                        "popularity": round(c.track.popularity, 2), "sources": sorted(c.track.sources),
                        "recording_mbid": c.track.recording_mbid, "deezer_id": c.track.deezer_id} for c in tracks],
        }
