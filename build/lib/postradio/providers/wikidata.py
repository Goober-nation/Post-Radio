"""Wikidata: the genre graph (data build only, never during a radio run). Wikidata data is CC0.

Nodes are the genres Wikidata links to a MusicBrainz genre (P8052). Edges are `subgenre_of` (P279, subclass
of) and `influenced_by` (P737/P941). Wikidata-only steps in between ("American hip hop", "music of Africa")
are collapsed, so an edge goes to the nearest MusicBrainz-linked ancestor. `roots` are the top genres a node
descends from (no MusicBrainz-linked parent) and `depth` is the shortest hop count to one. Wikidata's levels
are uneven, so treat edges as "related", not as a strict hierarchy."""
import time
from collections import defaultdict

from ..errors import SourceUnavailable
from ..http import Http
from . import parse_rows

SPARQL = "https://query.wikidata.org/sparql"
SOURCE = "Wikidata P279 (subclass of) + P737/P941 (CC0); MusicBrainz names via P8052"

# every MB-linked genre plus all its ancestors, so Wikidata-only steps can be collapsed. No label service here:
# with it the query hits Wikidata's 60 s limit and the answer is cut off mid-JSON (seen 2026-11-01).
ANCESTORS = """SELECT DISTINCT ?a ?mb ?p WHERE {
    ?g wdt:P8052 [] . ?g wdt:P279* ?a . OPTIONAL { ?a wdt:P279 ?p } OPTIONAL { ?a wdt:P8052 ?mb } }"""
# English labels, only for the MB-linked items (the only ones that become nodes)
LABELS = """SELECT ?a ?aLabel WHERE { ?a wdt:P8052 [] . ?a rdfs:label ?aLabel . FILTER(LANG(?aLabel) = "en") }"""
INFLUENCES = """SELECT DISTINCT ?g ?r WHERE { ?g wdt:P8052 [] . ?r wdt:P8052 [] .
    VALUES ?prop { wdt:P737 wdt:P941 } ?g ?prop ?r . }"""


class Wikidata:
    name = "wikidata"

    def __init__(self, http: Http):
        self.http = http

    def _sparql(self, query: str) -> list[dict]:
        d = self.http.get(SPARQL, params={"query": query}, headers={"Accept": "application/sparql-results+json"},
                          timeout=180)
        rows = ((d or {}).get("results") or {}).get("bindings")
        if not isinstance(rows, list):
            raise SourceUnavailable("Wikidata SPARQL returned no results (timeout or rate limit?)")
        return rows

    def genre_graph(self, mb_genres: dict[str, str]) -> dict:
        """mb_genres = {MusicBrainz genre MBID: name} (MusicBrainz.genre_names()). Returns the
        genre_graph.json document: {source, fetched, nodes: {name: {qid, label, roots, depth}}, edges}."""
        return build_graph(self._sparql(ANCESTORS), self._sparql(INFLUENCES), mb_genres, self._sparql(LABELS))


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[1]


def _val(row: dict, key: str) -> str | None:
    v = row.get(key)
    return v.get("value") if isinstance(v, dict) else None


def build_graph(rows: list[dict], influences: list[dict], mb_genres: dict[str, str],
                labels: list[dict] = ()) -> dict:
    """SPARQL bindings -> graph document. A stale P8052 that MusicBrainz no longer lists is dropped. A node
    without an English label is labelled with its QID."""
    label = dict(parse_rows(list(labels), lambda r: (_qid(_val(r, "a")), _val(r, "aLabel")) if _val(r, "aLabel")
                            else None))
    known: set[str] = set()  # every item reached, MB-linked or not
    mb: dict[str, str] = {}
    parents: dict[str, set[str]] = defaultdict(set)

    for a, mbid, p in parse_rows(rows, lambda r: (_qid(_val(r, "a")), _val(r, "mb"), _val(r, "p"))):
        known.add(a)
        if mbid in mb_genres:
            mb[a] = mb_genres[mbid]
        if p:
            parents[a].add(_qid(p))

    def mb_parents(q: str) -> set[str]:
        """Nearest MB-linked ancestors, walking through Wikidata-only ones."""
        found, seen, todo = set(), set(), list(parents[q])
        while todo:
            p = todo.pop()
            if p in seen or p not in known:
                continue
            seen.add(p)
            if p in mb:
                found.add(p)
            else:
                todo.extend(parents[p])
        return found - {q}

    edges = {(mb[q], mb[p], "subgenre_of") for q in mb for p in mb_parents(q)}
    infl = parse_rows(influences, lambda r: (_qid(_val(r, "g")), _qid(_val(r, "r"))))
    edges |= {(mb[s], mb[d], "influenced_by") for s, d in infl if s in mb and d in mb}
    edges = {e for e in edges if e[0] != e[1]}  # two Wikidata items can share one MB genre

    nodes = {name: {"qid": q, "label": label.get(q, q), "roots": [], "depth": None} for q, name in mb.items()}
    children: dict[str, set[str]] = defaultdict(set)
    for src, dst, kind in edges:
        if kind == "subgenre_of":
            children[dst].add(src)
    has_parent = {src for src, _, kind in edges if kind == "subgenre_of"}
    for root in sorted(set(nodes) - has_parent):
        nodes[root]["roots"].append(root)
        nodes[root]["depth"] = 0
        frontier, seen = [root], {root}
        while frontier:  # every descendant of this root
            frontier = [c for p in frontier for c in children[p] if c not in seen]
            seen.update(frontier)
        for c in seen - {root}:
            nodes[c]["roots"].append(root)
    _depths(nodes, children)
    return {"source": SOURCE, "fetched": time.strftime("%Y-%m-%d"), "nodes": dict(sorted(nodes.items())),
            "edges": [{"src": s, "dst": d, "kind": k} for s, d, k in sorted(edges)]}


def _depths(nodes: dict, children: dict[str, set[str]]) -> None:
    """Shortest hop count from any root (multi-source BFS)."""
    frontier = [n for n, v in nodes.items() if v["depth"] == 0]
    depth = 0
    while frontier:
        depth += 1
        nxt = []
        for p in frontier:
            for c in children[p]:
                if nodes[c]["depth"] is None:
                    nodes[c]["depth"] = depth
                    nxt.append(c)
        frontier = nxt
