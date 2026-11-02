"""Flow mode, the pure part: a taste profile from dated likes, a short path of genre stops over the genre graph,
each stop filled with familiar and new artists, ordered so neighbouring tracks share genres. No network; the
engine (Radio.flow) fetches artists and tracks and calls these.

explore (0..1) decides how far the path may leave the user's own genres and how many of each stop's tracks come
from artists the user doesn't know. Ported from Post-Radio-Webpage's app/flow.py (genre-graph branch)."""
import heapq
import math
import random
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .genres import Adjacency, GenreScore
from .models import Track


@dataclass(frozen=True)
class PlannerConfig:
    destination_from: float = 0.3  # below this explore, the path only walks the user's own genres
    destination_width: float = 0.2  # how tightly the destination's closeness must match what explore asks
    destination_reach: float = 0.6  # wanted closeness of the destination = 1 - reach × explore
    own_share_max: float = 0.15  # a destination is a genre the user has less than this share of
    candidates: int = 4  # each step picks among this many strongest neighbours
    hub_damping: float = 0.7  # at high explore, very close genres pull less (they're "more of the same")


@dataclass
class Pick:
    """One candidate track at a stop. `genres` are the artist's, strongest first."""
    track: Track
    genres: list[str]
    familiar: bool  # the user already likes or plays this artist
    score: float = 1.0
    stop: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def artist(self) -> str:
        return self.track.artist.name

    @property
    def title(self) -> str:
        return self.track.title


# ---------------------------------------------------------------- taste profile
def recency(at: int | None, newest: int | None, half_life_days: float) -> float:
    if at is None or newest is None or half_life_days <= 0:
        return 1.0
    return 0.5 ** (max(0, newest - at) / 86400 / half_life_days)


def artist_weights(likes: Iterable[tuple[str, int | None]], half_life_days: float) -> dict[str, float]:
    """(artist name, liked at) -> {artist: summed recency weight}. Newer likes count more; the newest is 1."""
    likes = list(likes)
    newest = max((at for _, at in likes if at is not None), default=None)
    out: Counter = Counter()
    for artist, at in likes:
        out[artist] += recency(at, newest, half_life_days)
    return dict(out)


def taste_profile(weights: dict[str, float], genres_by_source: list[tuple[dict[str, list[str]], float]],
                  genre_weight: dict[str, float] | None = None, ancestors: dict[str, frozenset[str]] | None = None,
                  parent_discount: float = 1.0) -> dict[str, float]:
    """{genre: share} summing to 1. Each source maps artist -> genres (strongest first, counting 1/rank) and is
    normalised on its own, then blended by its share, so a fine source (MusicBrainz) refines a coarse one
    (Yandex album genres) instead of being drowned by it.

    genre_weight: per-genre multiplier (rarity: a genre most artists carry counts less than a specific one).
    ancestors + parent_discount: when an artist's list holds a genre and one of its subgenres, the broader one
    counts parent_discount as much for that artist ("hip hop" next to "alternative hip hop")."""
    out: Counter = Counter()
    total_share = sum(s for _, s in genres_by_source) or 1.0
    for genres_of, share in genres_by_source:
        c: Counter = Counter()
        for artist, w in weights.items():
            gs = genres_of.get(artist) or []
            for i, g in enumerate(gs):
                v = w / (i + 1)
                if ancestors and parent_discount != 1 and any(g in ancestors.get(o, ()) for o in gs if o != g):
                    v *= parent_discount
                if genre_weight:
                    v *= genre_weight.get(g, 1.0)
                c[g] += v
        total = sum(c.values())
        for g, v in c.items():
            out[g] += share / total_share * v / total
    norm = sum(out.values()) or 1.0
    return {g: round(v / norm, 4) for g, v in out.most_common() if v > 0}


# ---------------------------------------------------------------- path
def plan_path(spread: list[GenreScore], adj: Adjacency, explore: float, stops: int = 5,
              rng: random.Random | None = None, roots: dict[str, tuple[str, ...]] | None = None,
              cfg: PlannerConfig | None = None, start: str | None = None) -> list[str]:
    """Genre stops, each one graph edge from the last (or, at a dead end, a jump), starting at one of the user's
    strongest genres. Two measures per genre, both 0..1: f = the user's own share (hop 0, relative to their top
    genre; 0 for genres they don't have) and r = spread score (how close it is to their taste at all). A step's
    pull is edge weight × ((1-explore)·f + explore·r·(1-0.7·explore·r)·(1-f)): explore 0 tours only the user's own
    genres; higher explore favours genres they have little of. From explore `destination_from` the path first
    routes to a destination outside the start genre's family (see destination()) and then keeps away from that
    family: fully at explore 1, partly in between. No revisits. `start` fixes the first stop (any genre in the
    graph, the listener's or not); the walk then follows the same rules from there."""
    rng, cfg = rng or random.Random(), cfg or PlannerConfig()
    if not spread or stops < 1:
        return []
    top_r = max(s.score for s in spread) or 1.0
    top_f = max((s.score for s in spread if s.hop == 0), default=top_r) or 1.0
    f = {s.genre: s.score / top_f if s.hop == 0 else 0.0 for s in spread}
    r = {s.genre: s.score / top_r for s in spread}
    own = sorted((g for g in f if f[g] > 0), key=lambda g: (-f[g], g))

    def pull(g: str) -> float:
        rg = r.get(g, 0.0)
        return (1 - explore) * f.get(g, 0.0) + explore * rg * (1 - cfg.hub_damping * explore * rg) * (1 - f.get(g, 0.0))

    if start is not None:
        path = [start]
    else:
        path = [rng.choices(own[:3], weights=[f[g] for g in own[:3]])[0]] if own else [spread[0].genre]
    home = set((roots or {}).get(path[0], ())) | {path[0]}
    away = False  # once at the destination, the start genre's family is damped by (1 - explore)
    if explore >= cfg.destination_from and stops > 1:
        dest = destination(path[0], f, r, explore, rng, roots, cfg)
        route = route_between(adj, path[0], dest) if dest else None
        if route:
            path, away = fit(route, stops), True

    def damp(g: str) -> float:
        return 1 - explore if away and roots and home & set(roots.get(g, (g,))) else 1.0

    while len(path) < stops:
        nb = {n: w * pull(n) * damp(n) for n, w in adj.get(path[-1], {}).items()
              if n not in path and pull(n) * damp(n) > 0}
        if nb:
            best = sorted(nb.items(), key=lambda kv: (-kv[1], kv[0]))[:cfg.candidates]
            path.append(rng.choices([g for g, _ in best], weights=[w for _, w in best])[0])
        else:
            rest = sorted((g for g in r if g not in path and pull(g) * damp(g) > 0),
                          key=lambda g: (-pull(g) * damp(g), g))
            if not rest:
                break
            path.append(rest[0])
    return path


def destination(start: str, f: dict[str, float], r: dict[str, float], explore: float, rng: random.Random,
                roots: dict[str, tuple[str, ...]] | None, cfg: PlannerConfig | None = None) -> str | None:
    """Where a more adventurous path heads: a genre the user has little of, outside the start genre's family (no
    shared Wikidata root) when roots are known, and about as close to their taste as explore asks: closeness near
    1 - reach × explore (0.7 at explore 0.5, 0.4 at 1 by default). Closeness is relative to the closest eligible
    genre, not to the user's top genre: with a broad profile every outside genre has a low raw score, and an
    absolute target would then pick the same nearest genre at every explore. One of the best three, at random."""
    cfg = cfg or PlannerConfig()
    want = 1 - cfg.destination_reach * explore
    fam = set((roots or {}).get(start, ())) | {start}

    def ok(g: str) -> bool:
        return g != start and f.get(g, 0) < cfg.own_share_max and not (roots and fam & set(roots.get(g, (g,))))

    eligible = [g for g in r if ok(g)]
    top = max((r[g] for g in eligible), default=0.0) or 1.0
    scored = sorted(((r[g] / top * (1 - f.get(g, 0)) * math.exp(-((r[g] / top - want) / cfg.destination_width) ** 2),
                      g) for g in eligible), key=lambda x: (-x[0], x[1]))[:3]
    scored = [(w, g) for w, g in scored if w > 0]
    return rng.choices([g for _, g in scored], weights=[w for w, _ in scored])[0] if scored else None


def route_between(adj: Adjacency, a: str, b: str) -> list[str] | None:
    """Strongest route a -> b: Dijkstra with step cost 1.05 - edge weight (few hops, strong links)."""
    dist, prev, heap = {a: 0.0}, {}, [(0.0, a)]
    while heap:
        d, g = heapq.heappop(heap)
        if g == b:
            out = [b]
            while out[-1] != a:
                out.append(prev[out[-1]])
            return out[::-1]
        if d > dist.get(g, math.inf):
            continue
        for n, w in adj.get(g, {}).items():
            nd = d + 1.05 - w
            if nd < dist.get(n, math.inf):
                dist[n], prev[n] = nd, g
                heapq.heappush(heap, (nd, n))
    return None


def fit(route: list[str], stops: int) -> list[str]:
    """A route longer than the playlist has stops keeps its ends and evenly spaced stops between."""
    if len(route) <= stops:
        return route
    if stops == 1:
        return route[:1]
    idx = sorted({round(i * (len(route) - 1) / (stops - 1)) for i in range(stops)})
    return [route[i] for i in idx]


def familiarity(path: list[str], spread: list[GenreScore]) -> float:
    """Mean share of the user's own taste over the path (1 = all their top genre, 0 = none of their genres)."""
    own = {s.genre: s.score for s in spread if s.hop == 0}
    top = max(own.values(), default=1.0) or 1.0
    return round(sum(own.get(g, 0) / top for g in path) / len(path), 3) if path else 0.0


def allocate(n: int, stops: int) -> list[int]:
    """n tracks over the stops, as even as possible, extras to the first stops."""
    return [n // stops + (i < n % stops) for i in range(stops)] if stops > 0 else []


# ---------------------------------------------------------------- fill and order
_FEAT = re.compile(r"\s*[(\[].*?[)\]]|\s+(feat|ft)\.?\s.*$", re.IGNORECASE)


def title_key(title: str) -> str:
    """"Forever (Explicit Version)" and "Forever feat. X" are the same song for de-duplication."""
    return _FEAT.sub("", title).casefold().strip()


def fill(path: list[str], pools: dict[str, list[Pick]], counts: list[int], explore: float,
         per_artist: int = 2, artist_key: Callable[[Pick], str] = lambda p: p.track.artist.key) -> list[list[Pick]]:
    """Each stop's tracks from its pool: share of new artists = explore (rounded), best score first. Artists with
    the stop's genre in their top two come first; others only fill a stop that would come up short. At most
    per_artist tracks per artist over the playlist and one per song title (the same song credited to two artists
    counts once). A stop short of new (or familiar) tracks takes the other kind rather than coming up short; a
    stop that still comes up short hands its remainder to the stops after it (then before it) that have
    eligible tracks left, so a thin genre doesn't shorten the playlist."""
    used: Counter = Counter()
    titles: set[str] = set()
    sorted_pools = {g: sorted(pools.get(g, []), key=lambda c: (g not in c.genres[:2], -c.score)) for g in path}
    out: list[list[Pick]] = [[] for _ in path]

    def take(i: int, k: int, want_familiar: bool | None) -> int:
        g, got = path[i], 0
        for c in sorted_pools[g]:
            if got >= k:
                break
            if want_familiar is not None and c.familiar != want_familiar:
                continue
            if c in out[i] or used[artist_key(c)] >= per_artist or title_key(c.title) in titles:
                continue
            c.stop = g
            out[i].append(c)
            used[artist_key(c)] += 1
            titles.add(title_key(c.title))
            got += 1
        return got

    short = 0
    for i, k in enumerate(counts):
        new_k = round(k * explore)
        got = take(i, new_k, False)
        got += take(i, k - new_k, True)
        got += take(i, k - got, None)
        short += k - got
    for i in [*range(len(path)), *range(len(path))][:2 * len(path)]:  # two passes: leftovers may free up
        if short <= 0:
            break
        short -= take(i, short, None)
    return out


def order(stops: list[list[Pick]], path: list[str], gap: int = 4,
          artist_key: Callable[[Pick], str] = lambda p: p.track.artist.key) -> list[Pick]:
    """Within a stop: tracks that also carry the previous stop's genre first, the next stop's genre last. Then
    nudges apart two tracks by one artist closer than `gap`, swapping only within the same stop so the path's
    order is never broken."""
    flat: list[Pick] = []
    for i, picks in enumerate(stops):
        prev = path[i - 1] if i else None
        nxt = path[i + 1] if i + 1 < len(path) else None
        flat += sorted(picks, key=lambda c, prev=prev, nxt=nxt: 0 if prev in c.genres else 2 if nxt in c.genres else 1)
    for i in range(len(flat)):
        window = range(max(0, i - gap + 1), i)
        if any(artist_key(flat[j]) == artist_key(flat[i]) for j in window):
            for j in range(i + 1, len(flat)):
                if flat[j].stop != flat[i].stop:
                    break
                if all(artist_key(flat[m]) != artist_key(flat[j]) for m in window):
                    flat[i], flat[j] = flat[j], flat[i]
                    break
    return flat


def flow_score(tracks: list[Pick], adj: Adjacency) -> float:
    """Mean closeness of neighbouring tracks: 1 if they share a genre, else the strongest graph edge between their
    genres, 0 if none. A check on the result, not something the picker optimises."""
    def close(a: Pick, b: Pick) -> float:
        if set(a.genres) & set(b.genres):
            return 1.0
        return max((adj.get(x, {}).get(y, 0.0) for x in a.genres for y in b.genres), default=0.0)

    pairs = list(zip(tracks, tracks[1:], strict=False))
    return round(sum(close(a, b) for a, b in pairs) / len(pairs), 3) if pairs else 0.0
