"""Genre layer: a graph of MusicBrainz genres and how strongly they relate. Pure functions, no network.

Two kinds of edges:
- structural: Wikidata subgenre/influence links (and, later, the Yandex genre tree) say *which* genres are
  related, not how strongly; each kind gets a fixed weight (`advanced.genre_kind_weights`).
- learned: genre-pair weights from data (`genre_weights.json`, built by the data build): co-tag NPMI over
  artists' MusicBrainz genres, and similar-artist pairs projected onto genres. When present, the data carries
  the ranking and structural edges only fill genres the data never reaches, scaled down.

The Wikidata graph (`postradio/data/genre_graph.json`, CC0) ships with the package; the learned weights are
CC BY-NC-SA and live in `storage.data_dir` when installed. Everything works without them."""
import json
import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import NamedTuple

PACKAGED_GRAPH = Path(__file__).resolve().parent / "data" / "genre_graph.json"
WEIGHTS_FILE = "genre_weights.json"  # in storage.data_dir

Pair = tuple[str, str]
Adjacency = dict[str, dict[str, float]]


class GenreScore(NamedTuple):
    genre: str
    score: float
    hop: int  # 0 = one of the profile's own genres


def pair(a: str, b: str) -> Pair:
    """Pairs are stored sorted, so (a, b) and (b, a) are one pair."""
    return (a, b) if a <= b else (b, a)


# ---------------------------------------------------------------- learned weights (used by the data build)
def popularity(artist_tags: dict[str, set[str]]) -> dict[str, int]:
    """Prior: how many artists carry each genre."""
    return dict(Counter(g for tags in artist_tags.values() for g in tags))


def cotag(artist_tags: dict[str, set[str]], min_shared: int = 2) -> dict[Pair, float]:
    """NPMI over artists, negatives dropped: 1 = the two genres only ever appear together, 0 = no more often
    than chance. Pairs shared by fewer than min_shared artists are noise and skipped."""
    n = len(artist_tags)
    single, joint = popularity(artist_tags), Counter()
    for tags in artist_tags.values():
        joint.update(pair(a, b) for a, b in combinations(sorted(tags), 2))
    out = {}
    for (a, b), c in joint.items():
        if c < min_shared:
            continue
        p_ab = c / n
        if p_ab >= 1:  # every artist has both: maximal association
            out[(a, b)] = 1.0
            continue
        npmi = math.log(p_ab / (single[a] / n * single[b] / n)) / -math.log(p_ab)
        if npmi > 0:
            out[(a, b)] = round(min(npmi, 1.0), 4)
    return out


def similar(pairs: Iterable[tuple[str, str, float]], artist_tags: dict[str, set[str]],
            min_pairs: int = 2) -> dict[Pair, float]:
    """Project similar-artist pairs (artist, artist, strength 0..1) onto genres: every genre pair across the
    two artists gets strength / (|tags_a| × |tags_b|), so an artist with ten tags doesn't count ten times.
    Totals are divided by sqrt(mass_a × mass_b) (cosine over genre mass), then scaled to max 1. Same-genre
    links are skipped (they say nothing about how two genres relate)."""
    acc, mass, count = Counter(), Counter(), Counter()
    for x, y, s in pairs:
        tx, ty = artist_tags.get(x) or set(), artist_tags.get(y) or set()
        if not tx or not ty:
            continue
        share = s / (len(tx) * len(ty))
        for a in tx:
            for b in ty:
                mass[a] += share
                mass[b] += share
                if a != b:
                    acc[pair(a, b)] += share
                    count[pair(a, b)] += 1
    raw = {p: v / math.sqrt(mass[p[0]] * mass[p[1]]) for p, v in acc.items() if count[p] >= min_pairs}
    top = max(raw.values(), default=1.0) or 1.0
    return {p: round(v / top, 4) for p, v in raw.items()}


def combine(layers: list[dict[Pair, float]], fallback: dict[Pair, float],
            fallback_scale: float = 0.2) -> dict[Pair, float]:
    """Data carries the ranking: a pair's weight is its strongest data layer. A structural edge is kept, scaled
    down to fallback_scale, only when one of its genres has no data edge at all, so it fills gaps but never
    outranks or overrides what the data says about covered genres."""
    out: dict[Pair, float] = {}
    for layer in layers:
        for p, w in layer.items():
            out[p] = max(out.get(p, 0.0), w)
    covered = {g for p in out for g in p}
    for p, w in fallback.items():
        if p not in out and not (p[0] in covered and p[1] in covered):
            out[p] = round(w * fallback_scale, 4)
    return out


def coverage(layer: dict[Pair, float]) -> int:
    return len({g for p in layer for g in p})


# ---------------------------------------------------------------- structural edges
def yandex_edges(table: dict) -> set[tuple[str, str, str]]:
    """Yandex genre tree {yandex id: {parent, mb: [MB genres]}} -> (MB genre of child, MB genre of its Yandex
    parent, "yandex_parent") edges, where they differ."""
    return {(c, p, "yandex_parent") for v in table.values() if v.get("parent") in table
            for c in v.get("mb") or [] for p in table[v["parent"]].get("mb") or [] if c != p}


def neighbours(edges: Iterable[tuple[str, str, str]], kind_weights: dict[str, float]) -> Adjacency:
    """Undirected, keeping the strongest kind when two genres are linked more than once. Unknown kinds are
    skipped."""
    adj: Adjacency = defaultdict(dict)
    for src, dst, kind in edges:
        w = kind_weights.get(kind)
        if not w:
            continue
        for a, b in ((src, dst), (dst, src)):
            adj[a][b] = max(adj[a].get(b, 0.0), w)
    return adj


def from_pairs(weights: dict[Pair, float]) -> Adjacency:
    adj: Adjacency = defaultdict(dict)
    for (a, b), w in weights.items():
        adj[a][b] = adj[b][a] = w
    return adj


def structural(edges: Iterable[tuple[str, str, str]], kind_weights: dict[str, float]) -> dict[Pair, float]:
    return {pair(a, b): w for a, nb in neighbours(edges, kind_weights).items() for b, w in nb.items() if a < b}


def spread(profile: dict[str, float], adj: Adjacency, hops: int = 2, decay: float = 0.5) -> list[GenreScore]:
    """Taste profile {genre: share} -> scores over the graph, strongest first. Each hop passes on
    share × edge weight × decay; a genre keeps its best path, not the sum, so a hub with many links doesn't win
    just for being a hub. hop 0 = the profile's own genres."""
    best: dict[str, tuple[float, int]] = {g: (s, 0) for g, s in profile.items() if s > 0}
    frontier = dict(best)
    for hop in range(1, hops + 1):
        nxt: dict[str, tuple[float, int]] = {}
        for g, (score, _) in frontier.items():
            for n, w in adj.get(g, {}).items():
                s = score * w * decay
                if s > best.get(n, (0.0, 0))[0] and s > nxt.get(n, (0.0, 0))[0]:
                    nxt[n] = (s, hop)
        best |= nxt
        frontier = nxt
    ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], kv[0]))
    return [GenreScore(g, round(s, 4), h) for g, (s, h) in ranked]


# ---------------------------------------------------------------- loading
@dataclass(frozen=True)
class GenreNode:
    qid: str  # Wikidata item
    label: str
    roots: tuple[str, ...]  # top genres this one descends from (no MusicBrainz-linked parent)
    depth: int | None  # shortest hop count to a root


@dataclass
class GenreGraph:
    nodes: dict[str, GenreNode]
    adj: Adjacency
    learned: bool  # True when genre_weights.json carried the ranking

    def related(self, genre: str) -> dict[str, float]:
        return self.adj.get(genre, {})

    def spread(self, profile: dict[str, float], hops: int = 2, decay: float = 0.5) -> list[GenreScore]:
        return spread(profile, self.adj, hops, decay)

    @classmethod
    def load(cls, kind_weights: dict[str, float], fallback_scale: float, data_dir: Path | None = None,
             graph_path: Path = PACKAGED_GRAPH, yandex: dict | None = None) -> "GenreGraph":
        """The packaged Wikidata graph, plus `yandex` tree edges when given. With `data_dir/genre_weights.json`
        present the learned weights rank and structure fills in at `fallback_scale`; otherwise the structural
        graph is used as is. Cached per file version, so repeated loads are free."""
        weights = data_dir / WEIGHTS_FILE if data_dir else None
        w_stamp = _stamp(weights) if weights else None
        y_key = json.dumps(yandex, sort_keys=True) if yandex else None
        return _load(graph_path, _stamp(graph_path), weights, w_stamp, y_key,
                     tuple(sorted(kind_weights.items())), fallback_scale)


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


@lru_cache(maxsize=8)
def _load(graph_path: Path, _g_stamp, weights: Path | None, w_stamp, yandex_json: str | None,
          kinds: tuple[tuple[str, float], ...], fallback_scale: float) -> GenreGraph:
    data = json.loads(graph_path.read_text())
    nodes = {name: GenreNode(n.get("qid", ""), n.get("label", name), tuple(n.get("roots") or ()), n.get("depth"))
             for name, n in data["nodes"].items()}
    edges = [(e["src"], e["dst"], e["kind"]) for e in data["edges"]]
    if yandex_json:
        edges += [e for e in yandex_edges(json.loads(yandex_json)) if e[0] in nodes and e[1] in nodes]
    kind_weights = dict(kinds)
    if not w_stamp:
        return GenreGraph(nodes, neighbours(edges, kind_weights), learned=False)
    w = json.loads(weights.read_text())
    layers = [{pair(*k.split("|", 1)): v for k, v in (w.get(name) or {}).items()} for name in ("cotag", "similar")]
    return GenreGraph(nodes, from_pairs(combine(layers, structural(edges, kind_weights), fallback_scale)),
                      learned=True)
