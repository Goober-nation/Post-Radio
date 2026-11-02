"""Flow mode, the pure part: a taste profile from dated likes, a short path of genre stops over the genre graph,
each stop filled with familiar and new artists, ordered so neighbouring tracks share genres. No network; the
engine (Radio.flow) fetches artists and tracks and calls these.

explore (0..1) sets, through straight lines between configured end values: how many stops the path has, how
strongly taste steers each step (taste_weight), and how many of each stop's tracks come from artists the user
doesn't know. Ported from Post-Radio-Webpage's app/flow.py (genre-graph branch)."""
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
    candidates: int = 4  # each step picks among this many strongest neighbours
    floor: float = 0.05  # pull of a genre outside the listener's spread, so the walk can still cross it


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
def interp(lo_hi: tuple[float, float], explore: float) -> float:
    """The value at `explore` on the straight line from lo_hi[0] (explore 0) to lo_hi[1] (explore 1)."""
    lo, hi = lo_hi
    return lo + (hi - lo) * explore


def within(adj: Adjacency, start: str, radius: int) -> dict[str, int]:
    """Genres at most `radius` links from start -> their distance (BFS)."""
    dist, frontier = {start: 0}, [start]
    for d in range(1, radius + 1):
        nxt = []
        for g in frontier:
            for n in adj.get(g, {}):
                if n not in dist:
                    dist[n] = d
                    nxt.append(n)
        frontier = nxt
    return dist


def plan_path(spread: list[GenreScore], adj: Adjacency, stops: int = 5, taste_weight: float = 1.0,
              rng: random.Random | None = None, start: str | None = None, radius: int = 0,
              cfg: PlannerConfig | None = None, warnings: list[str] | None = None) -> list[str]:
    """Genre stops as a walk over the genre graph. It starts at `start`, or at one of the listener's three
    strongest genres. Each step goes to a linked, unvisited genre, picked (weighted, among the `candidates`
    best) by link weight × pull ** taste_weight, where pull is the genre's closeness to the listener's taste
    (spread score relative to the best, `floor` for genres outside the spread). taste_weight 0 follows link
    strength only; higher keeps the walk on the listener's genres. With radius > 0 every stop stays within that
    many links of the start. A walk that gets stuck routes through the graph to the best reachable unvisited
    genre (the stops in between count); if nothing is reachable the path ends early and a warning is added."""
    rng, cfg = rng or random.Random(), cfg or PlannerConfig()
    if not spread or stops < 1:
        return []
    top_r = max(s.score for s in spread) or 1.0
    r = {s.genre: s.score / top_r for s in spread}
    own = sorted((s for s in spread if s.hop == 0), key=lambda s: (-s.score, s.genre))[:3]

    def pull(g: str) -> float:
        return max(r.get(g, 0.0), cfg.floor) ** taste_weight

    if start is not None:
        path = [start]
    elif own:
        path = [rng.choices([s.genre for s in own], weights=[s.score for s in own])[0]]
    else:
        path = [spread[0].genre]
    allowed = within(adj, path[0], radius) if radius > 0 else None
    ok = (lambda g: g in allowed) if allowed is not None else (lambda g: True)
    while len(path) < stops:
        nb = {n: w * pull(n) for n, w in adj.get(path[-1], {}).items() if n not in path and ok(n) and w > 0}
        if nb:
            best = sorted(nb.items(), key=lambda kv: (-kv[1], kv[0]))[:cfg.candidates]
            path.append(rng.choices([g for g, _ in best], weights=[w for _, w in best])[0])
            continue
        reach = within(adj, path[-1], len(adj))  # every genre reachable from here
        targets = sorted((g for g in reach if g not in path and ok(g)), key=lambda g: (-pull(g), reach[g], g))
        route = route_between(adj, path[-1], targets[0]) if targets else None
        if not route:
            if warnings is not None:
                warnings.append(f"path ended after {len(path)} of {stops} stops: no unvisited genre reachable "
                                f"from {path[-1]}")
            break
        if warnings is not None:
            warnings.append(f"stuck at {path[-1]}: routed through the graph to {targets[0]}")
        path += [g for g in route[1:] if g not in path and ok(g)][: stops - len(path)]
    return path


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
