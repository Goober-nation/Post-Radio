"""Exploration radio: build an artist graph around a seed, then rank tracks by hop + popularity fit.

explore in [0, 1]:
  0   -> stay on the seed and its closest neighbours, popular tracks, familiar artists allowed
  1   -> aim ~3 hops out, low-popularity "deep cuts", skip artists/tracks already in your history
"""
import math
import random
import re
from collections import defaultdict
from dataclasses import dataclass, field

from .sources import Deezer, ListenBrainz, MusicBrainz, Track, norm

JUNK_TITLE = re.compile(r"^\[|\b(live|demo|remix|edit|instrumental|acoustic version|karaoke|session|rehearsal"
                        r"|bootleg)\b|\(\d{4}|\d{4}[‐-]\d\d", re.I)
MAX_SOURCE_SHARE = 0.5  # no single-source track type may fill more than half the list
EDGE_WEIGHT = {"lb": 1.0, "deezer": 0.85, "mb": 0.6, "troi": 0.8}


@dataclass
class Node:
    name: str
    hop: int
    affinity: float  # best path product of edge weights from the seed
    mbid: str | None = None
    deezer_id: int | None = None
    via: set = field(default_factory=set)  # sources that produced an edge into this node
    lb_pop: float | None = None  # artist-level, log-scaled LB listener count, 0..1
    dz_pop: float | None = None  # same from Deezer fan count, fallback when LB has nothing
    parent: str | None = None  # name of the node this one was first reached from (for "why")

    @property
    def popularity(self) -> float | None:
        return self.lb_pop if self.lb_pop is not None else self.dz_pop


@dataclass
class Params:
    explore: float = 0.5
    n: int = 50
    per_artist: int = 2
    seed: int | None = None

    @property
    def max_hops(self) -> int:
        return 1 + round(2 * self.explore)

    @property
    def target_hop(self) -> float:
        return 3 * self.explore

    @property
    def target_pop(self) -> float:
        return 1 - 0.85 * self.explore

    @property
    def target_artist_pop(self) -> float:
        return 0.95 - 0.6 * self.explore

    @property
    def troi_mode(self) -> str:
        return "easy" if self.explore < 0.34 else "medium" if self.explore < 0.67 else "hard"


def hop_fit(hop: int, p: Params) -> float:
    return math.exp(-((hop - p.target_hop) ** 2) / 1.2)


def pop_fit(pop: float, p: Params) -> float:
    return math.exp(-((pop - p.target_pop) ** 2) / 0.12)


def dz_pop(fans: int) -> float:
    # calibrated so Deezer fans and LB listeners land on roughly the same scale (Radiohead ~0.95)
    return min(1.0, math.log10(1 + fans) / 6.9)


def artist_pop_fit(pop: float | None, p: Params) -> float:
    if pop is None:
        return 0.7
    # only penalise being *more* popular than the target; obscure is never punished at low explore
    over = max(0.0, pop - p.target_artist_pop)
    return math.exp(-(over ** 2) / 0.03)


class Graph:
    def __init__(self):
        self.nodes: dict[str, Node] = {}

    def add(self, name, hop, affinity, src, mbid=None, deezer_id=None, fans=None, parent=None) -> Node:
        key = norm(name)
        n = self.nodes.get(key)
        if n is None:
            n = self.nodes[key] = Node(name, hop, affinity, mbid, deezer_id, parent=parent)
        else:
            if hop < n.hop or (hop == n.hop and affinity > n.affinity):
                n.hop, n.affinity, n.parent = hop, max(affinity, n.affinity), parent or n.parent
            # independent sources agreeing on an edge is strong evidence
            if src not in n.via:
                n.affinity = min(1.0, n.affinity * 1.15)
            n.mbid = n.mbid or mbid
            n.deezer_id = n.deezer_id or deezer_id
        n.via.add(src)
        if fans is not None and n.dz_pop is None:
            n.dz_pop = dz_pop(fans)
        return n


class Radio:
    def __init__(self, lb: ListenBrainz, mb: MusicBrainz, dz: Deezer, log=print):
        self.lb, self.mb, self.dz, self.log = lb, mb, dz, log
        self._history = None

    def history(self):
        if self._history is None:
            self._history = self.lb.history()
            self.log(f"history: {len(self._history[0])} artists, {len(self._history[1])} tracks")
        return self._history

    # ---------- seed resolution ----------
    def resolve(self, artist=None, track=None, artist_mbid=None, recording_mbid=None):
        """Returns (seed_artist_name, artist_mbid, seed_track_or_None)."""
        if recording_mbid:
            meta = self.lb.recording_metadata([recording_mbid]).get(recording_mbid) or {}
            ac = (meta.get("artist") or {}).get("artists") or [{}]
            title = (meta.get("recording") or {}).get("name")
            name, amb = ac[0].get("name"), ac[0].get("artist_mbid")
            return name, amb, Track(name, title, amb, recording_mbid)
        if track:
            a, _, t = track.partition(" - ")
            if not t:
                raise ValueError("--track must look like 'Artist - Title'")
            r = self.mb.find_recording(a.strip(), t.strip())
            if not r:
                raise ValueError(f"track not found on MusicBrainz: {track}")
            return r["artist"], r["artist_mbid"], Track(r["artist"], r["title"], r["artist_mbid"], r["recording_mbid"])
        if artist_mbid:
            return self.mb.artist_name(artist_mbid) or artist_mbid, artist_mbid, None
        if artist:
            found = self.mb.find_artist(artist)
            if not found:
                raise ValueError(f"artist not found on MusicBrainz: {artist}")
            return found[1], found[0], None
        raise ValueError("need a seed")

    # ---------- graph ----------
    def build_graph(self, name, mbid, p: Params, cap: int = 160) -> Graph:
        g = Graph()
        seed = g.add(name, 0, 1.0, "seed", mbid=mbid)
        self._dz_lookup(seed)
        frontier = [norm(name)]
        fanout = {1: 14, 2: 8, 3: 6}
        budget = {h: cap * (h / sum(range(1, p.max_hops + 1))) for h in range(1, p.max_hops + 1)}
        for hop in range(1, p.max_hops + 1):
            nxt = []
            hop_start = len(g.nodes)
            for key in frontier:
                if len(g.nodes) - hop_start >= budget[hop]:
                    break
                src = g.nodes[key]
                k = fanout[hop]
                if src.mbid:
                    for m, nm, s in self.lb.similar_artists(src.mbid, limit=k):
                        w = src.affinity * EDGE_WEIGHT["lb"] * (0.5 + 0.5 * s)
                        nxt.append(g.add(nm, hop, w, "lb", mbid=m, parent=src.name))
                    if hop == 1:  # MB relations only matter right next to the seed (members, side projects)
                        for m, nm in self.mb.related_artists(src.mbid)[:6]:
                            nxt.append(g.add(nm, hop, src.affinity * EDGE_WEIGHT["mb"], "mb", mbid=m, parent=src.name))
                if src.deezer_id is None and hop > 1:
                    self._dz_lookup(src)
                if src.deezer_id:
                    rel = self.dz.related(src.deezer_id, limit=k)
                    for i, (did, nm, fans) in enumerate(rel):
                        w = 1 - 0.5 * i / max(1, len(rel))
                        nxt.append(g.add(nm, hop, src.affinity * EDGE_WEIGHT["deezer"] * w, "deezer",
                                         deezer_id=did, fans=fans, parent=src.name))
            # expand the strongest new nodes next
            seen, frontier = set(), []
            for n in sorted(nxt, key=lambda n: -n.affinity):
                kk = norm(n.name)
                if n.hop == hop and kk not in seen:
                    seen.add(kk)
                    frontier.append(kk)
            frontier = frontier[: max(4, 16 // hop)]
        self.log(f"graph: {len(g.nodes)} artists, hops<= {p.max_hops}")
        return g

    def artist_weight(self, n: Node, p: Params, known: dict[str, int]) -> float:
        w = n.affinity * hop_fit(n.hop, p) * artist_pop_fit(n.popularity, p)
        listens = known.get(n.mbid or "", 0) or known.get("n:" + norm(n.name), 0)
        if listens:
            fam = min(1.0, math.log1p(listens) / math.log(200))
            w *= 1 - 0.9 * p.explore * fam
        return w

    # ---------- candidates ----------
    def candidates(self, g: Graph, p: Params, seed_name: str, seed_mbid: str, known) -> list[tuple[Track, Node]]:
        # pass 1: graph-only weight picks a shortlist; fetching their LB top tracks also tells us how
        # popular each artist is, which pass 2 folds in (this is what pushes high explore off the mainstream)
        shortlist = sorted(g.nodes.values(), key=lambda n: -self.artist_weight(n, p, known))[:max(40, p.n)]
        lb_tracks = {}
        for n in shortlist:
            if not n.mbid and n.via <= {"deezer", "deezer-radio"}:
                found = self.mb.find_artist(n.name)
                n.mbid = found[0] if found and norm(found[1]) == norm(n.name) else None
            if n.mbid:
                lb_tracks[n.name] = self._lb_tracks(n)
        nodes = sorted(shortlist, key=lambda n: -self.artist_weight(n, p, known))[:max(18, p.n // 2)]
        out: list[tuple[Track, Node]] = []
        for n in nodes:
            out += [(t, n) for t in lb_tracks.get(n.name, [])]
            if n.deezer_id is None:
                self._dz_lookup(n)
            if n.deezer_id:
                out += [(t, n) for t in self.dz.top(n.deezer_id, limit=40)]
        # Deezer radio around the seed and the closest neighbours: brings in artists outside the graph
        radio_from = [g.nodes[norm(seed_name)]] + [n for n in nodes if n.hop == 1][:3]
        for n in radio_from:
            if n.deezer_id:
                for t in self.dz.radio(n.deezer_id):
                    rn = self._node_for(g, t.artist, n.hop + 1, n.affinity * 0.7, "deezer-radio", parent=n.name)
                    if rn.popularity is None:
                        self._dz_lookup(rn)
                    out.append((t, rn))
        # troi LB-radio: seed + similar artists in the mode matching the exploration level
        lo, hi = self._troi_window(p)
        radio = self.lb.radio_artist(seed_mbid, p.troi_mode, lo, hi, 8)
        mbids = [r.mbid for recs in radio.values() for r in recs]
        meta = self.lb.recording_metadata(mbids) if mbids else {}
        for amb, recs in radio.items():
            for r in recs:
                m = meta.get(r.mbid) or {}
                title = (m.get("recording") or {}).get("name")
                if not title:
                    continue
                name = r.artist_credit.name
                hop = 0 if amb == seed_mbid else 1
                node = self._node_for(g, name, hop, 0.8, "troi", mbid=amb)
                t = Track(name, title, amb, r.mbid, popularity=1 - (lo + hi) / 200, sources={"troi"})
                out.append((t, node))
        return out

    def _dz_lookup(self, n: Node):
        found = self._dz_match(n)
        n.deezer_id = found[0] if found else 0  # 0 = looked up, not on Deezer / ambiguous
        if found and n.dz_pop is None:
            n.dz_pop = dz_pop(found[1])

    def _dz_match(self, n: Node) -> tuple[int, int] | None:
        """Pick the Deezer artist that is really this node. Names collide (US soul 'Bilal' vs Algerian
        rai 'Bilal'), so when we know the MBID, require shared song titles with its LB top recordings."""
        cands = self.dz.find_artists(n.name)
        if not cands or not n.mbid:
            return cands[0] if cands else None
        lb_titles = {norm(r["recording_name"]) for r in self.lb.top_recordings(n.mbid)[:200]}
        if not lb_titles:
            return cands[0] if len(cands) == 1 else None
        for did, fans in cands:
            dz_titles = {norm(t.title) for t in self.dz.top(did, limit=40)}
            if len(dz_titles & lb_titles) >= 2:
                return did, fans
        return None

    def _troi_window(self, p: Params) -> tuple[int, int]:
        hi = round(100 - 70 * p.explore)
        return max(0, hi - 35), hi

    def _node_for(self, g: Graph, name, hop, affinity, src, mbid=None, parent=None) -> Node:
        return g.nodes.get(norm(name)) or g.add(name, hop, affinity, src, mbid=mbid, parent=parent)

    @staticmethod
    def path(g: Graph, n: Node) -> list[str]:
        out = [n.name]
        while n.parent and len(out) < 6:
            n = g.nodes[norm(n.parent)]
            out.append(n.name)
        return out[::-1]

    def _lb_tracks(self, n: Node) -> list[Track]:
        rows = self.lb.top_recordings(n.mbid)
        if not rows:
            return []
        n.lb_pop = min(1.0, math.log10(1 + (rows[0]["total_user_count"] or 0)) / 5.5)
        floor = max(5, (rows[0]["total_user_count"] or 0) * 0.002)
        rows = [r for r in rows if (r["total_user_count"] or 0) >= floor and not JUNK_TITLE.search(r["recording_name"])]
        seen, uniq = set(), []
        for r in rows:  # LB lists the same song once per recording; keep the most-played
            k = norm(r["recording_name"])
            if k not in seen:
                seen.add(k)
                uniq.append(r)
        rows = uniq[:120]
        out = []
        for i, r in enumerate(rows):
            out.append(Track(r["artist_name"], r["recording_name"], n.mbid, r["recording_mbid"],
                             popularity=1 - i / max(1, len(rows) - 1), sources={"lb"}))
        return out

    # ---------- ranking ----------
    def generate(self, p: Params, **seed) -> dict:
        rng = random.Random(p.seed)
        name, mbid, seed_track = self.resolve(**seed)
        self.log(f"seed: {name} ({mbid})" + (f" / track {seed_track.title}" if seed_track else ""))
        known, heard = self.history()
        g = self.build_graph(name, mbid, p)
        cands = self.candidates(g, p, name, mbid, known)

        merged: dict[str, tuple[Track, Node]] = {}
        for t, n in cands:
            if JUNK_TITLE.search(t.title):
                continue
            key = norm(t.artist) + "|" + norm(re.sub(r"\s*[\(\[].*", "", t.title))
            if key in merged:
                old = merged[key][0]
                old.sources |= t.sources
                old.popularity = max(old.popularity, t.popularity)
                old.recording_mbid = old.recording_mbid or t.recording_mbid
                old.deezer_id = old.deezer_id or t.deezer_id
            else:
                merged[key] = (t, n)

        if seed_track:
            merged.pop(norm(seed_track.artist) + "|" + norm(seed_track.title), None)

        scored = []
        for key, (t, n) in merged.items():
            if key in heard:
                if p.explore >= 0.3:
                    continue
                pen = 0.6
            else:
                pen = 1.0
            s = self.artist_weight(n, p, known) * pop_fit(t.popularity, p) * pen
            s *= 1 + 0.2 * (len(t.sources) - 1)
            s *= 0.85 + 0.3 * rng.random()  # keep repeated runs from being identical
            scored.append((s, t, n))
        scored.sort(key=lambda x: -x[0])

        per_artist: dict[str, int] = defaultdict(int)
        per_source: dict[str, int] = defaultdict(int)
        max_source = math.ceil(p.n * MAX_SOURCE_SHARE)
        picked = []
        for s, t, n in scored:
            a = norm(n.name)  # per graph node: Deezer /top includes tracks credited to collaborators
            only = next(iter(t.sources)) if len(t.sources) == 1 else None
            if per_artist[a] >= p.per_artist or (only and per_source[only] >= max_source):
                continue
            per_artist[a] += 1
            if only:
                per_source[only] += 1
            picked.append({
                "artist": t.artist, "title": t.title, "score": round(s, 4), "hop": n.hop,
                "popularity": round(t.popularity, 2), "sources": sorted(t.sources),
                "recording_mbid": t.recording_mbid, "deezer_id": t.deezer_id,
                "path": self.path(g, n), "via": sorted(n.via),
                "known_artist": bool(known.get(n.mbid or "") or known.get("n:" + norm(t.artist))),
            })
            if len(picked) >= p.n:
                break
        # gentle shuffle within bands so the playlist doesn't read as a strict ranking
        bands = [picked[i:i + 10] for i in range(0, len(picked), 10)]
        for b in bands:
            rng.shuffle(b)
        return {
            "seed": {"artist": name, "artist_mbid": mbid,
                     "track": seed_track.title if seed_track else None},
            "explore": p.explore, "mode": p.troi_mode, "graph_size": len(g.nodes),
            "candidates": len(merged), "tracks": [t for b in bands for t in b],
        }
