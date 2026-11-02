#!/usr/bin/env python3
"""Genre Atlas: render Post-Radio's genre graph as one self-contained interactive HTML page.

    pip install numpy scipy        # once, next to Post-Radio
    postradio data fetch           # once: the data release (store, graph, weights)
    python scripts/genre_atlas.py --home ~/.local/share/postradio/.. --out atlas.html     # graph only
    python scripts/genre_atlas.py --home DIR --yandex https://music.yandex.ru/playlists/lk.... \
        --title "Genre Atlas" --out atlas.html                                             # with a listener

--home is the Post-Radio home that holds data/ (the folder `postradio data status` reports, minus /data).
--yandex takes a playlist link (read live; Yandex blocks some regions) or a playlist export JSON (replayed).
$LISTENBRAINZ_TOKEN, when set, is used for the profile call only. The graph is exactly the one flow mode walks
(Recommender.genre_graph(): Wikidata links, learned weights, Yandex genre tree). With a listener the page gets
the Taste and Blend modes, profiles per half-life, the spread (nearby genres) and a flow preview whose real
paths come from Post-Radio's own planner (Radio.flow_plan + flow.plan_path); the page's JS port of the walk
is checked against them.

The layout is computed here, once (seeded force layout, numpy/scipy), so every page load shows the same frozen
picture. The output holds the listener's genre shares (no artists, titles or account data): treat it as personal.
"""
import argparse
import dataclasses
import json
import os
import random
import sys
from html import escape
from pathlib import Path

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components, minimum_spanning_tree
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from postradio import flow as flow_mod  # noqa: E402
from postradio.api import Listener, Recommender  # noqa: E402
from postradio.engine import Params, Providers, Radio  # noqa: E402
from postradio.genres import GRAPH_FILE, PACKAGED_GRAPH, WEIGHTS_FILE, pair, yandex_edges  # noqa: E402
from postradio.providers import yandex as ymod  # noqa: E402
from postradio.providers.yandex import genre_table  # noqa: E402

EXPLORES = (0.0, 0.25, 0.5, 0.75, 1.0)
SEEDS = (1, 2, 3, 4, 5, 6)
KINDS = ("subgenre_of", "influenced_by", "yandex_parent", "cotag", "similar", "cotag+similar")


# ---------------------------------------------------------------- data
def edge_kinds(rec: Recommender, graph) -> dict[tuple[str, str], int]:
    """Which source each final edge comes from: index into KINDS. A learned weight wins where the final weight
    equals it (as in genres.combine, the strongest data layer); otherwise the strongest structural kind."""
    a = rec.cfg.advanced
    data_dir = rec.data_dir
    gpath = data_dir / GRAPH_FILE if (data_dir / GRAPH_FILE).is_file() else PACKAGED_GRAPH
    data = json.loads(gpath.read_text())
    edges = [(e["src"], e["dst"], e["kind"]) for e in data["edges"]]
    edges += [e for e in yandex_edges(genre_table()) if e[0] in data["nodes"] and e[1] in data["nodes"]]
    best: dict[tuple[str, str], tuple[float, str]] = {}
    for s, d, kind in edges:
        w = a.genre_kind_weights.get(kind)
        if w and w > best.get(pair(s, d), (0.0, ""))[0]:
            best[pair(s, d)] = (w, kind)
    learned: dict[str, dict] = {}
    wf = data_dir / WEIGHTS_FILE
    if wf.is_file():
        raw = json.loads(wf.read_text())
        learned = {n: {pair(*k.split("|", 1)): v for k, v in (raw.get(n) or {}).items()} for n in ("cotag", "similar")}
    out = {}
    for x, nb in graph.adj.items():
        for y, w in nb.items():
            if x >= y:
                continue
            p = pair(x, y)
            c, s = learned.get("cotag", {}).get(p), learned.get("similar", {}).get(p)
            hit = [n for n, v in (("cotag", c), ("similar", s)) if v is not None and abs(v - w) < 1e-9]
            if len(hit) == 2 or (hit and c is not None and s is not None and c == s):
                out[p] = KINDS.index("cotag+similar")
            elif hit:
                out[p] = KINDS.index(hit[0])
            else:
                out[p] = KINDS.index(best[p][1]) if p in best else KINDS.index("subgenre_of")
    return out


def load_listener(export: str):
    """A Listener from a playlist link (read live from Yandex) or an export JSON (replayed: Yandex blocks some
    regions)."""
    if export.startswith(("http://", "https://")):
        return Listener(yandex_playlist=export, lb_token=os.environ.get("LISTENBRAINZ_TOKEN") or None)
    raw = json.load(open(export))
    replay = ymod.Playlist(raw["title"], raw["total"], [
        ymod.LikedTrack(tuple(tuple(x) for x in t["artists"]), t["title"], t.get("added_at"), t.get("genre"))
        for t in raw["tracks"]])

    def _playlist(self, url, limit=None, log=lambda _m: None):
        return dataclasses.replace(replay, tracks=replay.tracks[:limit] if limit else list(replay.tracks))

    ymod.YandexClient.playlist = _playlist
    return Listener(yandex_playlist="https://music.yandex.ru/playlists/lk.export-replay",
                    lb_token=os.environ.get("LISTENBRAINZ_TOKEN") or None)


# ---------------------------------------------------------------- layout
def force_layout(n: int, edges: list[tuple[int, int, float]], seed: int, iters: int = 500) -> np.ndarray:
    """Seeded Fruchterman-Reingold on one connected piece: attraction along a backbone (spanning tree plus each
    node's strongest links) scaled by weight, repulsion cut off at a few spring lengths (KD-tree), gravity, a
    cooling step limit, then a short overlap removal. Deterministic for a given seed."""
    rng = np.random.default_rng(seed)
    if n == 1:
        return np.zeros((1, 2))
    if n == 2:
        return np.array([[-0.5, 0.0], [0.5, 0.0]])
    pos = rng.normal(size=(n, 2)) * np.sqrt(n) * 0.5
    ea, eb, ew = (np.array([e[i] for e in edges]) for i in range(3))
    cost = coo_matrix((1.05 - ew, (ea, eb)), shape=(n, n))
    mst = minimum_spanning_tree(cost.tocsr()).tocoo()
    keep = {(min(a, b), max(a, b)) for a, b in zip(mst.row, mst.col, strict=True)}
    by_node: dict[int, list[tuple[float, int, int]]] = {}
    for a, b, w in edges:
        by_node.setdefault(a, []).append((w, a, b))
        by_node.setdefault(b, []).append((w, a, b))
    for lst in by_node.values():
        keep |= {(min(a, b), max(a, b)) for _, a, b in sorted(lst, reverse=True)[:3]}
    wmap = {(min(a, b), max(a, b)): w for a, b, w in edges}
    ba = np.array([k[0] for k in keep])
    bb = np.array([k[1] for k in keep])
    bw = np.array([wmap[k] for k in keep]) * 0.5 + 0.1
    k = 1.0
    for it in range(iters):
        t = 0.9 * (1 - it / iters) ** 1.5 + 0.02
        disp = np.zeros_like(pos)
        pairs = cKDTree(pos).query_pairs(10 * k, output_type="ndarray")
        if len(pairs):
            d = pos[pairs[:, 0]] - pos[pairs[:, 1]]
            dist = np.maximum(np.linalg.norm(d, axis=1), 0.01)
            f = (k * k / dist**2)[:, None] * d
            np.add.at(disp, pairs[:, 0], f)
            np.add.at(disp, pairs[:, 1], -f)
        d = pos[ba] - pos[bb]
        dist = np.maximum(np.linalg.norm(d, axis=1), 0.01)
        f = (dist / k * bw)[:, None] * d
        np.add.at(disp, ba, -f)
        np.add.at(disp, bb, f)
        disp -= pos * 0.003
        length = np.maximum(np.linalg.norm(disp, axis=1), 1e-9)
        pos += disp / length[:, None] * np.minimum(length, t)[:, None]
    for _ in range(40):  # overlap removal
        pairs = cKDTree(pos).query_pairs(0.9, output_type="ndarray")
        if not len(pairs):
            break
        d = pos[pairs[:, 0]] - pos[pairs[:, 1]]
        dist = np.maximum(np.linalg.norm(d, axis=1), 0.01)
        push = ((0.9 - dist) / 2 / dist)[:, None] * d
        np.add.at(pos, pairs[:, 0], push)
        np.add.at(pos, pairs[:, 1], -push)
    return pos - pos.mean(axis=0)


def layout(n: int, edges: list[tuple[int, int, float]], seed: int, width: float = 2000.0) -> np.ndarray:
    """All nodes 0..n-1: the largest connected piece laid out, the smaller pieces and isolated nodes laid out on
    their own and packed in rows below it. Result scaled to `width` wide, y flipped to screen orientation."""
    ncomp, labels = connected_components(coo_matrix(([1] * len(edges), ([e[0] for e in edges], [e[1] for e in edges])),
                                                    shape=(n, n)), directed=False)
    members = [np.flatnonzero(labels == c) for c in range(ncomp)]
    members.sort(key=lambda m: (-len(m), int(m[0])))
    boxes = []  # (members, local positions, w, h)
    for ci, m in enumerate(members):
        local = {int(g): i for i, g in enumerate(m)}
        le = [(local[a], local[b], w) for a, b, w in edges if a in local and b in local]
        p = force_layout(len(m), le, seed + ci) if len(m) > 1 else np.zeros((1, 2))
        p -= p.min(axis=0)
        boxes.append((m, p, p[:, 0].max() + 1.0, p[:, 1].max() + 1.0))
    out = np.zeros((n, 2))
    main_m, main_p, mw, mh = boxes[0]
    out[main_m] = main_p
    rest = boxes[1:]
    x = y = rowh = 0.0
    gap, row_w = 1.5, max(mw, 18.0)
    y0 = mh + 3.0
    singles = [b for b in rest if len(b[0]) == 1]
    for m, p, w, h in [b for b in rest if len(b[0]) > 1] + singles:
        step = w if len(m) > 1 else 1.1
        if x + step > row_w:
            x, y, rowh = 0.0, y + rowh + gap, 0.0
        out[m] = p + [x, y0 + y]
        x += step + (gap if len(m) > 1 else 0.0)
        rowh = max(rowh, h if len(m) > 1 else 1.0)
    out -= (out.min(axis=0) + out.max(axis=0)) / 2
    return out * (width / max(float(np.ptp(out[:, 0])), 1e-9))


# ---------------------------------------------------------------- build
HALF_LIVES = (30, 90, 180, 365, 730, 1460, 0)  # days; 0 = no decay (every like counts the same)


def build(home: str, export: str | None) -> dict:
    rec = Recommender(home=home)
    graph = rec.genre_graph()
    a = rec.cfg.advanced
    profile, nearby_input, variants = {}, None, {}
    hl_default = int(a.flow_half_life_days)
    if export:
        listener = load_listener(export)
        for hl in sorted(set(HALF_LIVES) | {hl_default}):
            adv = dataclasses.replace(a, flow_half_life_days=hl)
            with rec.with_config(advanced=adv) as r:
                res = r.profile(listener)
            variants[hl] = {r_["genre"]: r_["share"] for r_ in res.profile}
            nearby_input = res.input
        rec.close()
        profile = variants[hl_default]
    spread = graph.spread(profile, a.genre_spread_hops, a.genre_spread_decay) if profile else []

    every_taste = {g for v in variants.values() for g in v} | set(profile)
    names = sorted(set(graph.nodes) | set(graph.adj) | {n for nb in graph.adj.values() for n in nb} | every_taste)
    idx = {n: i for i, n in enumerate(names)}
    kinds = edge_kinds(rec, graph)
    roots_names = sorted({r for n in graph.nodes.values() for r in n.roots})
    ridx = {r: i for i, r in enumerate(roots_names)}
    nb = []  # per node, in the python adjacency's own order: the page's Dijkstra breaks ties the same way
    for n in names:
        nb.append([[idx[m], w, kinds[pair(n, m)]] for m, w in graph.adj.get(n, {}).items()])
    n_edges = sum(len(x) for x in nb) // 2

    taste_ids = sorted(idx[g] for g in every_taste)  # every half-life's genres share one frozen taste layout
    tset = set(taste_ids)
    tedges = [(i, j, w) for i in taste_ids for j, w, _ in nb[i] if j in tset and i < j]
    print(f"layout: {len(names)} nodes / {n_edges} edges, taste {len(taste_ids)} nodes / {len(tedges)} edges",
          file=sys.stderr)
    pos = layout(len(names), [(i, j, w) for i in range(len(names)) for j, w, _ in nb[i] if i < j], seed=7)
    tpos = None
    if taste_ids:
        loc = {g: i for i, g in enumerate(taste_ids)}
        tp = layout(len(taste_ids), [(loc[i], loc[j], w) for i, j, w in tedges], seed=11, width=1400.0)
        tpos = [[round(float(tp[i, 0]), 1), round(float(tp[i, 1]), 1)] for i in range(len(taste_ids))]

    pc = flow_mod.PlannerConfig()
    engine = Radio(Providers(resolver=None, similar=[], tracks=[]), a)  # only for flow_plan: no network

    def real_paths(sp, prof) -> dict:
        """Paths planned by Post-Radio's own code, with the values explore gives (Radio.flow_plan)."""
        out = {}
        for ex in EXPLORES:
            plan, adj = engine.flow_plan(Params(explore=ex, adv=a), graph, prof)
            out[f"{ex:.2f}"] = []
            for seed in SEEDS:
                rng = random.Random(seed)  # as Radio.flow: the planner gets its own Random drawn from the seed's
                path = flow_mod.plan_path(sp, adj, plan["stops"], plan["taste_weight"], random.Random(rng.random()),
                                          radius=plan["radius"], cfg=pc)
                out[f"{ex:.2f}"].append([idx[g] for g in path])
        return out

    real = real_paths(spread, profile) if spread else {}
    packed = {}
    for hl, prof in variants.items():
        sp = graph.spread(prof, a.genre_spread_hops, a.genre_spread_decay)
        packed[str(hl)] = {"profile": [[idx[g], v] for g, v in prof.items()],
                           "spread": [[idx[x.genre], x.score, x.hop] for x in sp], "real": real_paths(sp, prof)}
    return {
        "meta": {"nodes": len(names), "edges": n_edges, "learned": graph.learned, "taste": bool(profile),
                 "taste_nodes": len(taste_ids), "taste_edges": len(tedges), "input": nearby_input,
                 "kinds": list(KINDS)},
        "names": names,
        "labels": [graph.nodes[n].label if n in graph.nodes else n for n in names],
        "depth": [graph.nodes[n].depth if n in graph.nodes else None for n in names],
        "roots": [[ridx[r] for r in graph.nodes[n].roots] if n in graph.nodes else None for n in names],
        "rootNames": roots_names,
        "nb": nb,
        "pos": [[round(float(x), 1), round(float(y), 1)] for x, y in pos],
        "tasteIds": taste_ids,
        "tpos": tpos,
        "profile": [[idx[g], s] for g, s in profile.items()],
        "spread": [[idx[s.genre], s.score, s.hop] for s in spread],
        "cfg": {"stops": list(a.flow_stops), "taste_weight": list(a.flow_taste_weight),
                "min_link_weight": list(a.flow_min_link_weight), "min_genre_weight": a.flow_min_genre_weight,
                "radius": a.flow_radius, "candidates": pc.candidates, "floor": pc.floor,
                "max_genre_share": a.flow_max_genre_share},
        "real": real, "explores": list(EXPLORES), "seeds": list(SEEDS),
        "spreadParams": {"hops": a.genre_spread_hops, "decay": a.genre_spread_decay},
        "variants": packed, "halfLives": sorted(variants, key=lambda h: h or 10**9), "hlDefault": hl_default,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--home", required=True, help="Post-Radio home holding the installed data release")
    ap.add_argument("--yandex", "--yandex-export", dest="yandex_export", metavar="LINK_OR_JSON",
                    help="a Yandex playlist link, or its export JSON: enables the Taste and Blend modes")
    ap.add_argument("--title", default="Genre Atlas", help="page title")
    ap.add_argument("--out", required=True, help="HTML file to write")
    args = ap.parse_args()
    data = build(args.home, args.yandex_export)
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.replace("/*DATA*/", blob)
    html = html.replace("<title>Genre Atlas</title>", f"<title>{escape(args.title)}</title>")
    Path(args.out).write_text(html, encoding="utf-8")
    m = data["meta"]
    print(f"wrote {args.out}: {m['nodes']} nodes, {m['edges']} edges; taste {m['taste_nodes']} nodes, "
          f"{m['taste_edges']} edges", file=sys.stderr)


TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Genre Atlas</title>
<style>
:root{
  --bg:#f6f5f1; --panel:#ffffff; --ink:#1e2024; --muted:#666b73; --line:#dcdad3; --accent:#c2255c;
  --wiki:#3b6fb6; --learned:#6f8a8f; --yandex:#c98a12; --spread:#12a3a3; --route:#e8590c; --sim:#5f3dc4;
  --dimnode:#9aa0a6; --halo:#f6f5f1; --shadow:0 4px 18px rgba(0,0,0,.12);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#14161a; --panel:#1d2025; --ink:#e7e8ea; --muted:#9aa0a8; --line:#33373e; --accent:#f06595;
    --wiki:#6c9be0; --learned:#8fb0b5; --yandex:#e0a63a; --spread:#3fc7c7; --route:#ff922b; --sim:#9775fa;
    --dimnode:#6b7078; --halo:#14161a; --shadow:0 4px 18px rgba(0,0,0,.5);
  }
}
:root[data-theme="dark"]{
  --bg:#14161a; --panel:#1d2025; --ink:#e7e8ea; --muted:#9aa0a8; --line:#33373e; --accent:#f06595;
  --wiki:#6c9be0; --learned:#8fb0b5; --yandex:#e0a63a; --spread:#3fc7c7; --route:#ff922b; --sim:#9775fa;
  --dimnode:#6b7078; --halo:#14161a; --shadow:0 4px 18px rgba(0,0,0,.5);
}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--bg);color:var(--ink);
  font:14px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;overflow:hidden;overscroll-behavior:none}
#app{display:grid;grid-template-columns:340px 1fr;height:100dvh}
#side{background:var(--panel);border-right:1px solid var(--line);overflow-y:auto;padding:12px 14px 24px;z-index:3}
#stage{position:relative;min-width:0;min-height:0;background:var(--bg)}
#cv{position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none;cursor:grab}
#cv.drag{cursor:grabbing}
h1{font-size:17px;margin:0 0 2px}
.sub{color:var(--muted);font-size:12px;margin:0 0 10px}
details{border-top:1px solid var(--line);padding:8px 0}
summary{cursor:pointer;font-weight:600;font-size:13px;list-style:none;display:flex;justify-content:space-between}
summary::after{content:"+";color:var(--muted)} details[open] summary::after{content:"\2212"}
label.f{display:block;font-size:12px;color:var(--muted);margin:8px 0 2px}
input[type=text],input[type=number],select{width:100%;padding:6px 8px;border:1px solid var(--line);border-radius:6px;
  background:var(--bg);color:var(--ink);font:inherit}
input[type=range]{width:100%}
button{font:inherit;border:1px solid var(--line);background:var(--bg);color:var(--ink);border-radius:6px;
  padding:5px 10px;cursor:pointer}
button:hover{border-color:var(--muted)}
.row{display:flex;gap:8px;align-items:flex-end}.row>*{flex:1}.row>button{flex:0 0 auto}
.rel{position:relative}
.ac{position:absolute;left:0;right:0;top:100%;z-index:20;background:var(--panel);border:1px solid var(--line);
  border-radius:6px;box-shadow:var(--shadow);max-height:260px;overflow:auto;display:none}
.ac div{padding:5px 8px;cursor:pointer;font-size:13px}.ac div.on,.ac div:hover{background:var(--bg)}
.ac small{color:var(--muted)}
#bar{position:absolute;top:10px;left:10px;right:10px;display:flex;gap:8px;align-items:center;pointer-events:none;
  flex-wrap:wrap}
#bar>*{pointer-events:auto}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--panel);
  box-shadow:var(--shadow)}
.seg button{border:0;border-radius:0;background:transparent;padding:7px 14px}
.seg button.on{background:var(--accent);color:#fff}
.seg button:disabled{opacity:.4;cursor:not-allowed}
#panelBtn{display:none;background:var(--panel);box-shadow:var(--shadow)}
#zoomBox{position:absolute;right:10px;bottom:14px;display:flex;flex-direction:column;gap:6px}
#zoomBox button{width:34px;height:34px;padding:0;background:var(--panel);box-shadow:var(--shadow);font-size:17px}
#tip{position:absolute;pointer-events:none;background:var(--panel);border:1px solid var(--line);border-radius:8px;
  box-shadow:var(--shadow);padding:8px 10px;font-size:12px;max-width:290px;display:none;z-index:5}
#tip b{font-size:13px}#tip .m{color:var(--muted)}
#tip table{border-collapse:collapse;margin-top:4px}#tip td{padding:0 6px 0 0}
#pop{position:absolute;z-index:6;background:var(--panel);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);padding:8px;display:none;min-width:150px}
#pop b{display:block;margin:0 2px 6px;font-size:13px}#pop button{display:block;width:100%;text-align:left;margin-top:4px}
#status{position:absolute;left:10px;bottom:12px;font-size:12px;color:var(--muted);background:var(--panel);
  border:1px solid var(--line);border-radius:6px;padding:3px 8px;max-width:60%}
.leg{display:grid;grid-template-columns:auto 1fr;gap:4px 8px;align-items:center;font-size:12px}
.sw{display:inline-block;width:26px;height:0;border-top:3px solid;vertical-align:middle}
.dot{display:inline-block;width:12px;height:12px;border-radius:50%;vertical-align:middle;margin-left:7px}
.chips{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px;align-items:center}
.chip{border:1px solid var(--line);border-radius:12px;padding:1px 8px;font-size:12px;background:var(--bg)}
.arrow{color:var(--muted)}
.res{font-size:12px;margin-top:6px}.res .m{color:var(--muted)}
.pl{margin:3px 0;font-size:12px}.pl i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px}
.hide{display:none!important}
.adv{border:1px solid var(--line);border-radius:8px;padding:6px 8px;margin-top:8px}
.tag{font-size:11px;color:var(--muted);border:1px solid var(--line);border-radius:8px;padding:0 5px}
.tag.set{color:var(--accent);border-color:var(--accent)}
.dual{position:relative;height:26px}
.dual input{position:absolute;left:0;top:4px;width:100%;margin:0;background:none;pointer-events:none;-webkit-appearance:none;appearance:none}
.dual input::-webkit-slider-runnable-track{height:4px;background:var(--line);border-radius:2px}
.dual input::-moz-range-track{height:4px;background:var(--line);border-radius:2px}
.dual input::-webkit-slider-thumb{pointer-events:auto;-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:var(--accent);margin-top:-6px;cursor:pointer;border:2px solid var(--panel)}
.dual input::-moz-range-thumb{pointer-events:auto;width:14px;height:14px;border-radius:50%;background:var(--accent);cursor:pointer;border:2px solid var(--panel)}
.dual input+input::-webkit-slider-runnable-track{background:transparent}
.dual input+input::-moz-range-track{background:transparent}
@media (max-width:760px){
  #app{grid-template-columns:1fr}
  #side{position:absolute;left:0;right:0;bottom:0;max-height:58dvh;border-right:0;border-top:1px solid var(--line);
    border-radius:14px 14px 0 0;box-shadow:var(--shadow);transform:translateY(105%);transition:transform .2s}
  #side.open{transform:none}
  #panelBtn{display:inline-block}
  #status{display:none}
  #zoomBox{bottom:64px}
}
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <h1>Genre Atlas</h1>
    <p class="sub" id="sub"></p>
    <details open><summary>Find a genre</summary>
      <div class="rel"><input type="text" id="q" placeholder="Search genres..." autocomplete="off"><div class="ac" id="qac"></div></div>
    </details>
    <details open><summary>Route between two genres</summary>
      <p class="sub">Type or click two genres on the graph. Strongest route: Dijkstra, step cost 1.05 &minus; weight.</p>
      <div class="rel"><label class="f" for="from">From</label><input type="text" id="from" autocomplete="off"><div class="ac" id="fac"></div></div>
      <div class="rel"><label class="f" for="to">To</label><input type="text" id="to" autocomplete="off"><div class="ac" id="tac"></div></div>
      <div class="row" style="margin-top:8px"><button id="swap">Swap</button><button id="clr">Clear</button></div>
      <div class="res" id="routeOut"></div>
    </details>
    <details open id="hlBox"><summary>Taste memory</summary>
      <p class="sub">How fast old likes fade from the profile: a like this many days older than your newest counts half. Profiles are precomputed for each step.</p>
      <label class="f">Half-life: <b id="hlv"></b></label>
      <input type="range" id="hl" min="0" max="0" step="1" value="0">
      <label class="f">Max genre share: <b id="capv"></b> <span class="m">(no genre above this share of the profile; the excess goes to the rest)</span></label>
      <input type="range" id="cap" min="0.05" max="1" step="0.01" value="1">
    </details>
    <details open id="flowBox"><summary>Flow preview</summary>
      <p class="sub" id="flowNote">Simulates flow mode's genre path (JS port of plan_path).</p>
      <div id="flowCtl">
        <label class="f">Explore: <b id="exv">0.50</b> <span class="m">(sets the values below until you change them)</span></label>
        <input type="range" id="ex" min="0" max="1" step="0.05" value="0.5">
        <div class="row"><div><label class="f">Seed</label><input type="number" id="seed" min="0" value="1"></div>
          <button id="reroll" title="New random seed">Reroll</button></div>
        <div class="rel"><label class="f" for="start">Start genre (optional)</label><input type="text" id="start" autocomplete="off" placeholder="listener's own genre by default"><div class="ac" id="sac"></div></div>
        <div class="adv">
          <div class="row"><b class="f">Set by explore</b><button id="reset" title="Let explore set every value again">Reset</button></div>
          <label class="f">Stops: <b id="stopsv"></b> <span class="tag" id="stopsT"></span></label>
          <input type="range" id="stops" min="1" max="12" step="1" value="5">
          <label class="f">Taste weight: <b id="twv"></b> <span class="tag" id="twT"></span> <span class="m">(0 = link strength only)</span></label>
          <input type="range" id="tw" min="0" max="3" step="0.05" value="1">
          <label class="f">Min link weight: <b id="thv"></b> <span class="tag" id="thT"></span> <span class="m" id="thn"></span></label>
          <input type="range" id="th" min="0" max="0.5" step="0.01" value="0">
          <label class="f" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="tie"> tie min link weight to explore:
            <input type="number" id="tl0" min="0" max="0.5" step="0.01" style="width:64px"> at 0 &rarr; <input type="number" id="tl1" min="0" max="0.5" step="0.01" style="width:64px"> at 1</label>
          <label class="f">Min genre weight: <b id="nwv"></b> <span class="tag" id="nwT"></span> <span class="m" id="nwn"></span></label>
          <input type="range" id="nw" min="0" max="0.8" step="0.01" value="0">
          <p class="sub">Genres below it are hidden and left out of routes and the walk. Weight: your genres &radic;(share &divide; top share), others log(1 + links) &divide; log(1 + most links).</p>
          <label class="f">Radius: <b id="radv"></b> <span class="tag" id="radT"></span> <span class="m">(0 = off; else every stop within N links of the start)</span></label>
          <input type="range" id="rad" min="0" max="4" step="1" value="0">
        </div>
        <div class="res" id="planOut"></div>
        <label class="f" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="showSim" checked> Show simulated path</label>
        <label class="f" style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="showReal"> Show precomputed real paths (all 6 seeds)</label>
        <div class="res" id="simOut"></div>
        <div class="res" id="realOut"></div>
      </div>
    </details>
    <details open><summary>Dot size</summary>
      <p class="sub">Your genres: by taste share. Other genres: by number of links (in Blend, plus closeness to your taste). Both grow with zoom.</p>
      <label class="f">Your genres: <b id="szTv">1.0</b>&times;</label>
      <input type="range" id="szT" min="0.2" max="3" step="0.1" value="1">
      <label class="f">Other genres: <b id="szOv">1.0</b>&times;</label>
      <input type="range" id="szO" min="0.2" max="3" step="0.1" value="1">
    </details>
    <details open><summary>Legend</summary><div id="legend"></div></details>
    <details><summary>About</summary><p class="sub" id="about"></p></details>
  </aside>
  <main id="stage">
    <canvas id="cv"></canvas>
    <div id="bar">
      <button id="panelBtn">Panels</button>
      <div class="seg" id="modes">
        <button data-m="taste">Taste</button><button data-m="data">Data</button><button data-m="blend">Blend</button>
      </div>
    </div>
    <div id="zoomBox"><button id="zin" aria-label="Zoom in">+</button><button id="zout" aria-label="Zoom out">&minus;</button><button id="fit" aria-label="Fit" title="Fit">&#9635;</button></div>
    <div id="tip"></div>
    <div id="pop"><b id="popName"></b><button data-a="from">Route from here (A)</button><button data-a="to">Route to here (B)</button><button data-a="start">Flow start (S)</button></div>
    <div id="status"></div>
  </main>
</div>
<script id="data" type="application/json">/*DATA*/</script>
<script>
(() => {
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const $ = (s) => document.querySelector(s);
const N = D.names.length, names = D.names, labels = D.labels;
const idx = new Map(names.map((n, i) => [n, i]));
const KIND_TXT = ["subgenre of (Wikidata)", "influenced by (Wikidata)", "Yandex genre tree", "co-tagged artists",
  "similar artists", "co-tag + similar artists"];
const KGROUP = [0, 0, 2, 1, 1, 1]; // 0 Wikidata, 1 learned, 2 Yandex
const HAS_TASTE = D.meta.taste;

// ---- graph (adjacency in the python order, so Dijkstra breaks ties as route_between does)
// links outside [TH, TH_HI] are ignored by routes, the flow simulation and the drawing (sidebar range)
let TH = 0, TH_HI = 1;
const inBand = (w) => w >= TH - 1e-9 && w <= TH_HI + 1e-9;
let adj = [];
const adjKind = D.nb.map((l) => new Map(l.map(([j, , k]) => [j, k])));
const deg = new Int32Array(N);
let nodeOk = () => true; // set to the show-by-weight filter below: hidden genres are out of routes and flow too
function applyTh() {
  adj = D.nb.map((l, i) => new Map(nodeOk(i) ? l.filter(([j, w]) => inBand(w) && nodeOk(j)).map(([j, w]) => [j, w]) : []));
  for (let i = 0; i < N; i++) deg[i] = adj[i].size;
}
applyTh();
const E = []; // [a, b, w, k] for a < b
D.nb.forEach((l, a) => l.forEach(([b, w, k]) => { if (a < b) E.push([a, b, w, k]); }));
const nE = E.length;
const ea = new Int32Array(nE), eb = new Int32Array(nE), ew = new Float32Array(nE), ek = new Uint8Array(nE);
E.forEach((e, i) => { ea[i] = e[0]; eb[i] = e[1]; ew[i] = e[2]; ek[i] = e[3]; });

// ---- taste
const share = new Float64Array(N), inTaste = new Uint8Array(N);
D.profile.forEach(([i, s]) => { share[i] = s; inTaste[i] = 1; });
let topShare = Math.max(...D.profile.map((p) => p[1]), 1e-9);
const spreadScore = new Float32Array(N), spreadHop = new Int8Array(N).fill(-1);
D.spread.forEach(([i, s, h]) => { spreadScore[i] = s; spreadHop[i] = h; });
let topR = Math.max(...D.spread.map((p) => p[1]), 1e-9);
const relR = (i) => (spreadHop[i] >= 0 ? spreadScore[i] / topR : 0);

// ---- positions per layout
const pos = new Float32Array(N * 2);
D.pos.forEach(([x, y], i) => { pos[2 * i] = x; pos[2 * i + 1] = y; });
const tpos = new Float32Array(N * 2).fill(NaN);
if (D.tpos) D.tasteIds.forEach((g, i) => { tpos[2 * g] = D.tpos[i][0]; tpos[2 * g + 1] = D.tpos[i][1]; });
const tasteIds = D.tasteIds;
const tasteEdge = new Uint8Array(nE); // both ends in the taste profile
for (let e = 0; e < nE; e++) tasteEdge[e] = inTaste[ea[e]] && inTaste[eb[e]] ? 1 : 0;
const keptEdges = () => { let a = 0, t = 0; for (let e = 0; e < nE; e++) if (inBand(ew[e])) { a++; t += tasteEdge[e]; } return [a, t]; };

// ---- flow / route: port of postradio.flow (plan_path, destination, route_between, fit)
function mulberry32(a) {
  return () => { a |= 0; a = (a + 0x6D2B79F5) | 0; let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
const cmpName = (a, b) => (names[a] < names[b] ? -1 : names[a] > names[b] ? 1 : 0);

class Heap { // min-heap of [dist, node], ties by genre name like python's (float, str) tuples
  constructor() { this.a = []; }
  less(x, y) { return x[0] < y[0] || (x[0] === y[0] && cmpName(x[1], y[1]) < 0); }
  push(v) { const a = this.a; a.push(v); let i = a.length - 1;
    while (i > 0) { const p = (i - 1) >> 1; if (!this.less(a[i], a[p])) break; [a[i], a[p]] = [a[p], a[i]]; i = p; } }
  pop() { const a = this.a, top = a[0], last = a.pop();
    if (a.length) { a[0] = last; let i = 0;
      for (;;) { const l = 2 * i + 1, r = l + 1; let m = i;
        if (l < a.length && this.less(a[l], a[m])) m = l;
        if (r < a.length && this.less(a[r], a[m])) m = r;
        if (m === i) break; [a[i], a[m]] = [a[m], a[i]]; i = m; } }
    return top; }
  get size() { return this.a.length; }
}
function routeBetween(a, b) {
  const dist = new Map([[a, 0]]), prev = new Map(), h = new Heap();
  h.push([0, a]);
  while (h.size) {
    const [d, g] = h.pop();
    if (g === b) { const out = [b]; while (out[out.length - 1] !== a) out.push(prev.get(out[out.length - 1])); return out.reverse(); }
    if (d > (dist.has(g) ? dist.get(g) : Infinity)) continue;
    for (const [n, w] of adj[g]) {
      const nd = d + 1.05 - w;
      if (nd < (dist.has(n) ? dist.get(n) : Infinity)) { dist.set(n, nd); prev.set(n, g); h.push([nd, n]); }
    }
  }
  return null;
}
function fit(route, stops) {
  if (route.length <= stops) return route;
  if (stops === 1) return route.slice(0, 1);
  const s = new Set();
  for (let i = 0; i < stops; i++) s.add(pyRound(i * (route.length - 1) / (stops - 1)));
  return [...s].sort((x, y) => x - y).map((i) => route[i]);
}
function pyRound(x) { // python round(): half to even
  const f = Math.floor(x), d = x - f;
  return d < 0.5 ? f : d > 0.5 ? f + 1 : (f % 2 === 0 ? f : f + 1);
}
const rootsOf = (g) => D.roots[g]; // null: not a Wikidata genre (python's roots.get(g, default))
const rootSet = (g, dflt) => new Set((rootsOf(g) || dflt).map ? (rootsOf(g) || dflt) : []);
const intersects = (a, b) => { for (const x of a) if (b.has(x)) return true; return false; };
const CFG = D.cfg; // python's flow settings: explore curves, minimums, radius, planner candidates and floor
// roots entries are root-name ids; a genre's own id is used as its "family" when it has no roots entry
const famOf = (g) => new Set(D.roots[g] ? D.roots[g].map((r) => "r" + r) : ["g" + g]);
const famWithSelf = (g) => { const s = new Set(D.roots[g] ? D.roots[g].map((r) => "r" + r) : []); s.add("g" + g); return s; };
// a root's name may itself be a genre: align "r<id>" with "g<genre>" so both spellings meet
const rootAsGenre = D.rootNames.map((n) => idx.get(n));
function canon(set) { const o = new Set(); for (const x of set) { o.add(x);
  if (x[0] === "r" && rootAsGenre[+x.slice(1)] !== undefined) o.add("g" + rootAsGenre[+x.slice(1)]);
  if (x[0] === "g") { const gi = +x.slice(1); const rn = D.rootNames.indexOf(names[gi]); if (rn >= 0) o.add("r" + rn); } } return o; }

function prepSpread() {
  const sp = D.spread; // python order: strongest first
  const tr = Math.max(...sp.map((s) => s[1])) || 1;
  let tf = 0; for (const s of sp) if (s[2] === 0) tf = Math.max(tf, s[1]); tf = tf || tr;
  const f = new Map(), r = new Map();
  for (const [g, sc, hop] of sp) { f.set(g, hop === 0 ? sc / tf : 0); r.set(g, sc / tr); }
  const own = [...f.keys()].filter((g) => f.get(g) > 0).sort((a, b) => (f.get(b) - f.get(a)) || cmpName(a, b));
  return { f, r, own, first: sp.length ? sp[0][0] : null };
}
let SP = HAS_TASTE ? prepSpread() : null;
// half-life slider: swap in the profile, spread and real paths computed (in python) for that half-life
// max genre share: the profile capped like postradio.flow.cap_shares, then spread again over the full graph
// (port of genres.spread). At cap 1 the python-computed profile and spread are used as they are.
let CAP = 1, BASE = null;
function capShares(prof, cap) { // prof: [[id, share]]
  if (cap >= 1 || !prof.length) return prof.slice();
  if (cap * prof.length <= 1) return prof.map(([g]) => [g, 1 / prof.length]);
  const out = new Map(prof), fixed = new Set();
  for (;;) {
    const over = [...out].filter(([g, v]) => !fixed.has(g) && v > cap + 1e-12).map(([g]) => g);
    if (!over.length) break;
    over.forEach((g) => fixed.add(g));
    let rest = 0; for (const [g, v] of out) if (!fixed.has(g)) rest += v;
    const left = 1 - cap * fixed.size;
    for (const [g, v] of out) out.set(g, fixed.has(g) ? cap : rest ? v * left / rest : 0);
  }
  return [...out].sort((a, b) => b[1] - a[1]);
}
function spreadOf(prof) {
  const { hops, decay } = D.spreadParams, best = new Map(prof.filter(([, v]) => v > 0).map(([g, v]) => [g, [v, 0]]));
  let frontier = new Map(best);
  for (let h = 1; h <= hops; h++) {
    const nxt = new Map();
    for (const [g, [sc]] of frontier) for (const [n, w] of D.nb[g]) {
      const v = sc * w * decay;
      if (v > (best.get(n) || [0])[0] && v > (nxt.get(n) || [0])[0]) nxt.set(n, [v, h]);
    }
    for (const [k, v] of nxt) best.set(k, v);
    frontier = nxt;
  }
  return [...best].map(([g, [v, h]]) => [g, v, h]).sort((a, b) => (b[1] - a[1]) || cmpName(a[0], b[0]));
}
function applyProfile() {
  if (!BASE) BASE = { profile: D.profile, spread: D.spread };
  D.profile = CAP >= 1 ? BASE.profile : capShares(BASE.profile, CAP);
  D.spread = CAP >= 1 ? BASE.spread : spreadOf(D.profile);
  share.fill(0); inTaste.fill(0); D.profile.forEach(([i, s]) => { share[i] = s; inTaste[i] = 1; });
  topShare = Math.max(...D.profile.map((p) => p[1]), 1e-9);
  spreadScore.fill(0); spreadHop.fill(-1); D.spread.forEach(([i, s, h]) => { spreadScore[i] = s; spreadHop[i] = h; });
  topR = Math.max(...D.spread.map((p) => p[1]), 1e-9);
  for (let e = 0; e < nE; e++) tasteEdge[e] = inTaste[ea[e]] && inTaste[eb[e]] ? 1 : 0;
  SP = prepSpread();
}
function useHalfLife(hl) {
  const v = D.variants && D.variants[String(hl)]; if (!v) return;
  BASE = { profile: v.profile, spread: v.spread }; D.real = v.real;
  applyProfile();
}

function choices(items, weights, rnd) {
  let tot = 0; for (const w of weights) tot += w;
  const x = rnd() * tot; let acc = 0;
  for (let i = 0; i < items.length; i++) { acc += weights[i]; if (x < acc) return items[i]; }
  return items[items.length - 1];
}
// ---- port of postradio.flow.plan_path (the walk) and Radio.flow_plan (explore -> values)
function hopsFrom(a, R) { // BFS distance (in links) from a, up to R
  const d = new Map([[a, 0]]); let fr = [a];
  for (let h = 1; h <= R && fr.length; h++) { const nx = [];
    for (const g of fr) for (const n of (adj[g] || new Map()).keys()) if (!d.has(n)) { d.set(n, h); nx.push(n); }
    fr = nx; }
  return d;
}
const interp = (lohi, e) => lohi[0] + (lohi[1] - lohi[0]) * e;
const round3 = (x) => Math.round(x * 1000) / 1000;
// pick(kind, items, weights, ctx) -> item. Live runs sample with a seeded PRNG; verification forces python's choices.
function planPath(stops, tw, pick, start, radius, warnings) {
  const { r } = SP, floor = CFG.floor, cand = CFG.candidates;
  if (!r.size || stops < 1) return [];
  const pull = (g) => Math.pow(Math.max(r.get(g) || 0, floor), tw);
  const own = D.spread.filter((s) => s[2] === 0).sort((x, y) => (y[1] - x[1]) || cmpName(x[0], y[0])).slice(0, 3);
  let path;
  if (start !== null && start !== undefined) path = [start];
  else if (own.length) path = [pick("start", own.map((s) => s[0]), own.map((s) => s[1]), { path: [] })];
  else path = [D.spread[0][0]];
  const allowed = radius > 0 ? hopsFrom(path[0], radius) : null;
  const ok = (g) => !allowed || allowed.has(g);
  while (path.length < stops) {
    const last = path[path.length - 1], nb = [];
    for (const [n, w] of adj[last] || []) if (!path.includes(n) && ok(n) && w > 0) nb.push([n, w * pull(n)]);
    if (nb.length) {
      const best = nb.sort((x, y) => (y[1] - x[1]) || cmpName(x[0], y[0])).slice(0, cand);
      path.push(pick("step", best.map((b) => b[0]), best.map((b) => b[1]), { path }));
      continue;
    }
    const reach = hopsFrom(last, N);
    const targets = [...reach.keys()].filter((g) => !path.includes(g) && ok(g))
      .sort((a, b) => (pull(b) - pull(a)) || (reach.get(a) - reach.get(b)) || cmpName(a, b));
    const route = targets.length ? routeBetween(last, targets[0]) : null;
    if (!route) { warnings && warnings.push(`path ended after ${path.length} of ${stops} stops: nothing reachable from ${labels[last]}`); break; }
    warnings && warnings.push(`stuck at ${labels[last]}: routed through the graph to ${labels[targets[0]]}`);
    for (const g of route.slice(1)) if (!path.includes(g) && ok(g) && path.length < stops) path.push(g);
  }
  return path;
}
// the values a flow runs with: explore-derived unless the page sets them (S.over), as Radio.flow_plan
function flowPlan() {
  const e = +$("#ex").value, ov = S.over || {}, explicit = [];
  const val = (k, derived) => { if (ov[k] !== undefined) { explicit.push(k); return ov[k]; } return derived; };
  const linkCurve = S.tieLink ? [+$("#tl0").value, +$("#tl1").value] : CFG.min_link_weight;
  return {
    explore: e, stops: val("stops", Math.round(interp(CFG.stops, e))), taste_weight: val("taste_weight", round3(interp(CFG.taste_weight, e))),
    min_link_weight: val("min_link_weight", round3(interp(linkCurve, e))), min_genre_weight: val("min_genre_weight", CFG.min_genre_weight),
    radius: val("radius", CFG.radius), new_artist_share: e, half_life_days: HL, explicit, warnings: [],
  };
}
function simulate(plan, seed, start) {
  const rnd = mulberry32(seed >>> 0);
  return planPath(plan.stops, plan.taste_weight, (k, items, w) => choices(items, w, rnd), start, plan.radius, plan.warnings);
}
// Replay a python path through the port with every random choice forced to python's: true when the port's rules can
// produce exactly that path (each pick was among the candidates the port offers, each deterministic step matches).
function verifyPath(path, plan) {
  const why = [];
  const out = planPath(plan.stops, plan.taste_weight, (kind, items, w, ctx) => {
    const want = kind === "start" ? path[0] : path[ctx.path.length];
    if (items.includes(want)) return want; why.push(kind + " " + ctx.path.length + " not a candidate"); return items[0];
  }, undefined, plan.radius, null);
  const same = out.length === path.length && out.every((g, i) => g === path[i]);
  return { ok: same && !why.length, why, out };
}

// ---- view state and colours
const cv = $("#cv"), ctx = cv.getContext("2d");
let W = 0, H = 0, DPR = 1;
const view = { x: 0, y: 0, k: 1 };
let fitK = 1;
const S = { over: {}, tieLink: false, plan: null, mode: HAS_TASTE ? "blend" : "data", hover: -1, sel: -1, from: -1, to: -1, route: null, sim: null,
  showSim: true, showReal: false, tip: null };
const C = {};
function readColors() {
  const cs = getComputedStyle(document.documentElement);
  for (const k of ["bg", "ink", "muted", "line", "accent", "wiki", "learned", "yandex", "spread", "route", "sim", "dimnode", "halo"])
    C[k] = cs.getPropertyValue("--" + k).trim();
}
const dark = () => matchMedia("(prefers-color-scheme: dark)").matches;
const REAL_COL = ["#0c8599", "#7048e8", "#e67700", "#2b8a3e", "#c2255c", "#1971c2"];
const GROUP_COL = () => [C.wiki, C.learned, C.yandex];

const visNodes = {}, prio = {}, bounds = {};
let HL = D.hlDefault;
const hlText = (hl) => (hl === 0 ? "off (every like counts the same)" : hl >= 365 ? `${(hl / 365).toFixed(hl % 365 ? 1 : 0)} year${hl === 365 ? "" : "s"} (${hl} days)` : `${hl} days`);
function hlInit() {
  const el = $("#hl"), list = D.halfLives || [];
  if (!HAS_TASTE || !list.length) { $("#hlBox").classList.add("hide"); return; }
  const h = new URLSearchParams(location.hash.slice(1));
  if (h.has("hl") && list.includes(+h.get("hl"))) HL = +h.get("hl");
  el.max = list.length - 1; el.value = list.indexOf(HL); $("#hlv").textContent = hlText(HL);
  CAP = Math.min(Math.max(+(h.get("cap") || CFG.max_genre_share || 1), 0.05), 1);
  $("#cap").value = CAP; $("#capv").textContent = CAP >= 1 ? "off" : CAP.toFixed(2);
  if (HL !== D.hlDefault) useHalfLife(HL); else if (CAP < 1) applyProfile();
  $("#cap").addEventListener("input", () => {
    CAP = +$("#cap").value; $("#capv").textContent = CAP >= 1 ? "off" : CAP.toFixed(2);
    applyProfile(); setupModes(); legend(); nwText(); updateSim(); updateRoute(); saveHash(); schedule();
  });
  el.addEventListener("input", () => {
    HL = list[+el.value]; $("#hlv").textContent = hlText(HL);
    useHalfLife(HL); setupModes(); legend(); nwText(); updateSim(); updateRoute(); saveHash(); schedule();
  });
}
function setupModes() {
  const all = Array.from({ length: N }, (_, i) => i);
  visNodes.data = all; visNodes.blend = all; visNodes.taste = tasteIds.filter((g) => inTaste[g]);
  for (const m of ["data", "blend", "taste"]) {
    const arr = visNodes[m].slice();
    arr.sort((a, b) => {
      const sa = m === "data" ? 0 : share[a], sb = m === "data" ? 0 : share[b];
      if (sa !== sb) return sb - sa;
      if (m === "blend") { const d = relR(b) - relR(a); if (d) return d; }
      return deg[b] - deg[a] || cmpName(a, b);
    });
    prio[m] = new Map(arr.map((g, r) => [g, r]));
    prio[m].order = arr;
    const P = m === "taste" ? tpos : pos;
    let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
    for (const g of arr) { const x = P[2 * g], y = P[2 * g + 1]; if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
    bounds[m] = [x0, y0, x1, y1];
  }
}
const P = () => (S.mode === "taste" ? tpos : pos);
function fitView() {
  const [x0, y0, x1, y1] = bounds[S.mode], pad = 40;
  const k = Math.min((W - 2 * pad) / Math.max(x1 - x0, 1), (H - 2 * pad - 40) / Math.max(y1 - y0, 1));
  fitK = k; view.k = k; view.x = W / 2 - k * (x0 + x1) / 2; view.y = H / 2 + 20 - k * (y0 + y1) / 2;
}
const zoomRel = () => view.k / fitK;
function zoomAt(mx, my, s) {
  const nk = Math.min(Math.max(view.k * s, fitK * 0.4), fitK * 60), r = nk / view.k;
  view.x = mx - (mx - view.x) * r; view.y = my - (my - view.y) * r; view.k = nk;
}
function centerOn(g, minZoom) {
  const p = P(); const nk = Math.max(view.k, fitK * (minZoom || 5));
  view.k = nk; view.x = W / 2 - nk * p[2 * g]; view.y = H / 2 - nk * p[2 * g + 1];
}

// ---- drawing
let raf = 0;
const schedule = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; draw(); }); };
// dot sizes: your genres by taste share, other genres by number of links (+ closeness to your taste in Blend);
// both grow with sqrt(zoom). SZ_T / SZ_O are the sidebar multipliers.
let SZ_T = 1, SZ_O = 1;
// node weight 0..1 for the show/hide range: your genres sqrt(share / top share), others log(1 + links) / log(1 + most)
let NW_LO = 0, NW_HI = 1;
const maxDeg0 = Math.max(...D.nb.map((l) => l.length), 1);
const nodeW = (g) => (S.mode !== "data" && inTaste[g] ? Math.sqrt(share[g] / topShare) : Math.log1p(D.nb[g].length) / Math.log1p(maxDeg0));
const shown = (g) => nodeW(g) >= NW_LO - 1e-9; // a minimum only (the upper bound went unused)
nodeOk = shown;
function nodeR(g) {
  const z = Math.min(Math.max(Math.sqrt(zoomRel()), 0.75), 4.5);
  if (S.mode === "taste" || (S.mode === "blend" && inTaste[g])) return (3.2 + 34 * Math.sqrt(share[g])) * (S.mode === "taste" ? 1 : 0.8) * z * SZ_T;
  const base = 1.6 + 0.55 * Math.sqrt(deg[g]);
  return (S.mode === "blend" ? base * 0.8 + 3.5 * relR(g) : base) * z * SZ_O;
}
function hueOf(g) {
  const r = D.roots[g]; if (!r || !r.length) return -1;
  let h = 0; const s = D.rootNames[r[0]]; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h % 360;
}
const hues = Int16Array.from({ length: N }, (_, g) => hueOf(g));
function nodeFill(g) {
  if (S.mode === "taste" || (S.mode === "blend" && inTaste[g])) {
    const t = Math.sqrt(share[g] / topShare);
    return dark() ? `hsl(338 ${55 + 30 * t}% ${70 - 22 * t}%)` : `hsl(338 ${60 + 25 * t}% ${78 - 33 * t}%)`;
  }
  if (S.mode === "blend") { const r = relR(g); if (r > 0) return dark() ? `hsl(180 ${30 + 40 * r}% ${34 + 24 * r}%)` : `hsl(180 ${35 + 35 * r}% ${72 - 28 * r}%)`; return C.dimnode; }
  const h = hues[g]; if (h < 0) return C.dimnode;
  return dark() ? `hsl(${h} 45% 62%)` : `hsl(${h} 50% 52%)`;
}
const buckets = [0.15, 0.3, 0.5, 0.75, 1.01];
function bucketOf(w) { for (let i = 0; i < buckets.length; i++) if (w < buckets[i]) return i; return 4; }

function visibleEdge(e, P_, x0, y0, x1, y1) {
  const ax = P_[2 * ea[e]], ay = P_[2 * ea[e] + 1], bx = P_[2 * eb[e]], by = P_[2 * eb[e] + 1];
  if (ax !== ax || bx !== bx) return false;
  if (!shown(ea[e]) || !shown(eb[e])) return false;
  return !((ax < x0 && bx < x0) || (ax > x1 && bx > x1) || (ay < y0 && by < y0) || (ay > y1 && by > y1));
}
function drawEdges() {
  const P_ = P(), m = S.mode;
  const x0 = -view.x / view.k, y0 = -view.y / view.k, x1 = (W - view.x) / view.k, y1 = (H - view.y) / view.k;
  const zr = Math.pow(zoomRel(), 0.35), cols = GROUP_COL();
  // class: 0 plain data edge, 1 touches a taste node (blend), 2 taste-taste (overlap)
  const groups = new Map();
  for (let e = 0; e < nE; e++) {
    if (!inBand(ew[e])) continue;
    if (m === "taste" && !tasteEdge[e]) continue;
    if (!visibleEdge(e, P_, x0, y0, x1, y1)) continue;
    const cls = m === "blend" ? (tasteEdge[e] ? 2 : (inTaste[ea[e]] || inTaste[eb[e]] ? 1 : 0)) : 0;
    const key = cls * 100 + (cls === 2 ? 0 : KGROUP[ek[e]]) * 10 + bucketOf(ew[e]);
    let l = groups.get(key); if (!l) groups.set(key, l = []); l.push(e);
  }
  const keys = [...groups.keys()].sort((a, b) => a - b);
  for (const key of keys) {
    const cls = Math.floor(key / 100), grp = Math.floor(key / 10) % 10, b = key % 10, w = [0.08, 0.22, 0.4, 0.62, 0.9][b];
    ctx.beginPath();
    for (const e of groups.get(key)) {
      const a = ea[e], c = eb[e];
      ctx.moveTo(P_[2 * a] * view.k + view.x, P_[2 * a + 1] * view.k + view.y);
      ctx.lineTo(P_[2 * c] * view.k + view.x, P_[2 * c + 1] * view.k + view.y);
    }
    let alpha, lw, col;
    if (m === "taste") { alpha = 0.25 + 0.6 * w; lw = (0.6 + 3.2 * w); col = cols[grp]; }
    else if (m === "data") { alpha = 0.07 + 0.55 * w * w + 0.1 * w; lw = (0.3 + 1.9 * w); col = cols[grp]; }
    else if (cls === 2) { alpha = 0.45 + 0.5 * w; lw = (0.8 + 3 * w); col = C.accent; }
    else if (cls === 1) { alpha = 0.06 + 0.3 * w; lw = (0.3 + 1.4 * w); col = cols[grp]; }
    else { alpha = 0.03 + 0.12 * w; lw = (0.25 + 1.0 * w); col = cols[grp]; }
    ctx.globalAlpha = Math.min(alpha, 1); ctx.strokeStyle = col; ctx.lineWidth = lw * zr; ctx.stroke();
  }
  ctx.globalAlpha = 1;
}
function drawNodes() {
  const P_ = P(), m = S.mode, vis = visNodes[m];
  if (m === "blend") { // faint spread halo
    ctx.fillStyle = C.spread;
    for (const g of vis) { const r = relR(g); if (r <= 0 || inTaste[g] || !shown(g)) continue;
      const x = P_[2 * g] * view.k + view.x, y = P_[2 * g + 1] * view.k + view.y;
      if (x < -40 || y < -40 || x > W + 40 || y > H + 40) continue;
      ctx.globalAlpha = 0.07 + 0.22 * r; ctx.beginPath(); ctx.arc(x, y, (5 + 22 * r) * Math.sqrt(zoomRel()), 0, 6.2832); ctx.fill(); }
    ctx.globalAlpha = 1;
  }
  const order = m === "data" ? vis : prio[m].order.slice().reverse(); // big/important on top
  ctx.strokeStyle = C.halo; ctx.lineWidth = 1;
  for (const g of order) {
    if (!shown(g)) continue;
    const x = P_[2 * g] * view.k + view.x, y = P_[2 * g + 1] * view.k + view.y;
    if (x < -30 || y < -30 || x > W + 30 || y > H + 30) continue;
    const r = nodeR(g);
    ctx.globalAlpha = m === "blend" && !inTaste[g] && !relR(g) ? 0.55 : 1;
    ctx.fillStyle = nodeFill(g); ctx.beginPath(); ctx.arc(x, y, r, 0, 6.2832); ctx.fill();
    if (r > 3) ctx.stroke();
  }
  ctx.globalAlpha = 1;
}
function pathOverlay() {
  const P_ = P(), out = [];
  const seg = (path, col, lw, numbers, dash) => {
    ctx.strokeStyle = C.halo; ctx.lineWidth = lw + 3; ctx.globalAlpha = 0.85; ctx.setLineDash([]);
    for (const pass of [0, 1]) {
      if (pass) { ctx.strokeStyle = col; ctx.lineWidth = lw; ctx.globalAlpha = 1; }
      for (let i = 0; i + 1 < path.length; i++) {
        const a = path[i], b = path[i + 1];
        if (P_[2 * a] !== P_[2 * a] || P_[2 * b] !== P_[2 * b]) continue;
        ctx.setLineDash(dash || !adj[a].has(b) ? [6, 5] : []);
        ctx.beginPath(); ctx.moveTo(P_[2 * a] * view.k + view.x, P_[2 * a + 1] * view.k + view.y);
        ctx.lineTo(P_[2 * b] * view.k + view.x, P_[2 * b + 1] * view.k + view.y); ctx.stroke();
      }
    }
    ctx.setLineDash([]); ctx.globalAlpha = 1;
    path.forEach((g, i) => {
      if (P_[2 * g] !== P_[2 * g]) return;
      const x = P_[2 * g] * view.k + view.x, y = P_[2 * g + 1] * view.k + view.y;
      ctx.fillStyle = col; ctx.strokeStyle = C.halo; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(x, y, numbers ? 9 : (i === 0 ? 6 : 4), 0, 6.2832); ctx.fill(); ctx.stroke();
      if (numbers) { ctx.fillStyle = "#fff"; ctx.font = "bold 11px system-ui,sans-serif"; ctx.textAlign = "center";
        ctx.textBaseline = "middle"; ctx.fillText(String(i + 1), x, y + 0.5); }
      out.push(g);
    });
  };
  if (S.showReal && HAS_TASTE) {
    const key = realKey();
    (D.real[key] || []).forEach((p, i) => seg(p, REAL_COL[i % 6], 3, false, false));
  }
  if (S.route) {
    seg(S.route.path, C.route, 4, true, false);
    // per-edge weights at the segment middles
    ctx.font = "11px system-ui,sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (let i = 0; i + 1 < S.route.path.length; i++) {
      const a = S.route.path[i], b = S.route.path[i + 1];
      if (P_[2 * a] !== P_[2 * a] || P_[2 * b] !== P_[2 * b]) continue;
      const x = (P_[2 * a] + P_[2 * b]) / 2 * view.k + view.x, y = (P_[2 * a + 1] + P_[2 * b + 1]) / 2 * view.k + view.y;
      const t = adj[a].get(b).toFixed(2), tw = ctx.measureText(t).width + 8;
      ctx.fillStyle = C.bg; ctx.globalAlpha = 0.92; ctx.fillRect(x - tw / 2, y - 8, tw, 16); ctx.globalAlpha = 1;
      ctx.strokeStyle = C.route; ctx.lineWidth = 1; ctx.strokeRect(x - tw / 2, y - 8, tw, 16);
      ctx.fillStyle = C.ink; ctx.fillText(t, x, y);
    }
  }
  if (S.sim && S.showSim) seg(S.sim, C.sim, 4, true, false);
  return out;
}
function drawLabels(forced) {
  const P_ = P(), m = S.mode, order = prio[m].order, z = zoomRel();
  const budget = (m === "taste" ? 26 : 14) * Math.pow(z, m === "taste" ? 1.6 : 2.2);
  ctx.textBaseline = "middle"; ctx.textAlign = "left";
  const placed = [], want = [];
  const extra = new Set(forced);
  if (S.hover >= 0) extra.add(S.hover);
  if (S.sel >= 0) extra.add(S.sel);
  for (const g of [S.from, S.to]) if (g >= 0) extra.add(g);
  for (const g of extra) want.push(g);
  for (let r = 0, k = 0; r < order.length && k < budget; r++) { const g = order[r]; if (!shown(g) || extra.has(g)) continue; want.push(g); k++; }
  const fs = m === "taste" ? 12 : 11;
  for (const g of want) {
    const p = P_[2 * g]; if (p !== p) continue;
    const x = p * view.k + view.x, y = P_[2 * g + 1] * view.k + view.y;
    if (x < -50 || y < -20 || x > W + 50 || y > H + 20) continue;
    const big = inTaste[g] && m !== "data";
    ctx.font = `${big ? "600 " : ""}${fs}px system-ui,-apple-system,sans-serif`;
    const t = labels[g], tw = ctx.measureText(t).width, r = nodeR(g);
    const box = [x + r + 3, y - fs / 2 - 1, tw + 2, fs + 2];
    const force = extra.has(g);
    if (!force && placed.some((q) => box[0] < q[0] + q[2] && box[0] + box[2] > q[0] && box[1] < q[1] + q[3] && box[1] + box[3] > q[1])) continue;
    placed.push(box);
    ctx.lineWidth = 3; ctx.strokeStyle = C.halo; ctx.lineJoin = "round"; ctx.strokeText(t, box[0], y);
    ctx.fillStyle = force ? C.ink : C.ink; ctx.globalAlpha = force || z > 1.3 || big ? 1 : 0.85; ctx.fillText(t, box[0], y); ctx.globalAlpha = 1;
  }
}
function drawRings() {
  const P_ = P();
  const ring = (g, col, w, r0) => { if (g < 0 || P_[2 * g] !== P_[2 * g]) return;
    const x = P_[2 * g] * view.k + view.x, y = P_[2 * g + 1] * view.k + view.y;
    ctx.strokeStyle = col; ctx.lineWidth = w; ctx.beginPath(); ctx.arc(x, y, nodeR(g) + r0, 0, 6.2832); ctx.stroke(); };
  ring(S.sel, C.ink, 2, 4);
  [[S.from, "A", C.route], [S.to, "B", C.route], [S.start === undefined ? -1 : S.start, "S", C.sim]].forEach(([g, t, col]) => {
    if (g < 0 || P_[2 * g] !== P_[2 * g]) return;
    ring(g, col, 3, 5);
    const x = P_[2 * g] * view.k + view.x + nodeR(g) + 5, y = P_[2 * g + 1] * view.k + view.y - nodeR(g) - 5;
    ctx.fillStyle = col; ctx.beginPath(); ctx.arc(x, y, 8, 0, 6.2832); ctx.fill();
    ctx.fillStyle = "#fff"; ctx.font = "bold 11px system-ui,sans-serif"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(t, x, y + 0.5);
  });
  if (S.hover >= 0) { // hovered node's own edges
    const g = S.hover; ctx.strokeStyle = C.ink; ctx.globalAlpha = 0.55;
    for (const [n, w] of adj[g]) { if (P_[2 * n] !== P_[2 * n]) continue;
      if (S.mode === "taste" && !inTaste[n]) continue;
      ctx.lineWidth = 0.6 + 2.6 * w; ctx.beginPath();
      ctx.moveTo(P_[2 * g] * view.k + view.x, P_[2 * g + 1] * view.k + view.y);
      ctx.lineTo(P_[2 * n] * view.k + view.x, P_[2 * n + 1] * view.k + view.y); ctx.stroke(); }
    ctx.globalAlpha = 1; ring(g, C.ink, 2, 3);
  }
}
function draw() {
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
  drawEdges(); drawNodes();
  const forced = pathOverlay();
  drawRings(); drawLabels(forced);
  const zr = zoomRel();
  const [ka, kt] = keptEdges();
  $("#status").textContent = `${S.mode === "taste" ? tasteIds.length : N} genres, ${S.mode === "taste" ? kt : ka} links` +
    `${TH > 0 ? ` (weight ≥ ${TH.toFixed(2)})` : ""} · zoom ${zr.toFixed(1)}x`;
}

// ---- interaction
function hit(mx, my) {
  const P_ = P(); let best = -1, bd = 1e9;
  for (const g of visNodes[S.mode]) {
    if (!shown(g)) continue;
    const x = P_[2 * g] * view.k + view.x - mx, y = P_[2 * g + 1] * view.k + view.y - my, d = Math.hypot(x, y) - nodeR(g);
    if (d < 4 && d < bd) { bd = d; best = g; }
  }
  return best;
}
function tipHtml(g) {
  const parts = [`<b>${esc(labels[g])}</b>` + (labels[g] !== names[g] ? ` <span class="m">(${esc(names[g])})</span>` : "")];
  const bits = [];
  if (HAS_TASTE) {
    bits.push(inTaste[g] ? `share ${(share[g] * 100).toFixed(1)}%` : "share 0");
    bits.push(spreadHop[g] >= 0 ? `r ${relR(g).toFixed(2)} (hop ${spreadHop[g]})` : "r 0");
  }
  bits.push(`degree ${deg[g]}`);
  parts.push(`<div class="m">${bits.join(" · ")}</div>`);
  const nb = [...adj[g]].sort((a, b) => b[1] - a[1]).slice(0, 7);
  if (nb.length) parts.push("<table>" + nb.map(([n, w]) => `<tr><td>${w.toFixed(2)}</td><td>${esc(labels[n])}</td><td class="m">${KIND_TXT[adjKind[g].get(n)].split(" (")[0]}</td></tr>`).join("") + "</table>");
  else parts.push('<div class="m">no links (isolated)</div>');
  return parts.join("");
}
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
function showTip(g, mx, my) {
  const t = $("#tip");
  if (g < 0) { t.style.display = "none"; return; }
  t.innerHTML = tipHtml(g); t.style.display = "block";
  const rw = $("#stage").getBoundingClientRect(), tw = t.offsetWidth, th = t.offsetHeight;
  t.style.left = Math.min(Math.max(mx + 14, 6), rw.width - tw - 6) + "px";
  t.style.top = Math.min(Math.max(my + 14, 6), rw.height - th - 6) + "px";
}
const ptrs = new Map(); let drag = null, pinch = null;
cv.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
  let dx = e.deltaX, dy = e.deltaY;
  if (e.deltaMode === 1) { dx *= 16; dy *= 16; } else if (e.deltaMode === 2) { dx *= H; dy *= H; }
  if (e.ctrlKey) zoomAt(mx, my, Math.min(Math.max(Math.exp(-dy * 0.01), 0.6), 1.67)); // pinch on a Mac touchpad
  else { view.x -= dx; view.y -= dy; } // two-finger drag
  S.hover = -1; showTip(-1); hidePop(); schedule();
}, { passive: false });
for (const t of ["gesturestart", "gesturechange", "gestureend"]) cv.addEventListener(t, (e) => e.preventDefault());
cv.addEventListener("pointerdown", (e) => {
  cv.setPointerCapture(e.pointerId); ptrs.set(e.pointerId, [e.clientX, e.clientY]);
  if (ptrs.size === 1) drag = { x: e.clientX, y: e.clientY, moved: 0, t: performance.now(), vx: view.x, vy: view.y };
  if (ptrs.size === 2) { const [a, b] = [...ptrs.values()]; pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), cx: (a[0] + b[0]) / 2, cy: (a[1] + b[1]) / 2 }; if (drag) drag.moved = 99; }
  cv.classList.add("drag");
});
cv.addEventListener("pointermove", (e) => {
  const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
  if (ptrs.has(e.pointerId)) {
    const prev = ptrs.get(e.pointerId); ptrs.set(e.pointerId, [e.clientX, e.clientY]);
    if (ptrs.size === 2 && pinch) {
      const [a, b] = [...ptrs.values()], d = Math.hypot(a[0] - b[0], a[1] - b[1]), cx = (a[0] + b[0]) / 2, cy = (a[1] + b[1]) / 2;
      view.x += cx - pinch.cx; view.y += cy - pinch.cy;
      zoomAt(cx - r.left, cy - r.top, d / Math.max(pinch.d, 1)); pinch = { d, cx, cy }; schedule();
    } else if (drag) {
      view.x += e.clientX - prev[0]; view.y += e.clientY - prev[1];
      drag.moved += Math.abs(e.clientX - prev[0]) + Math.abs(e.clientY - prev[1]); schedule();
    }
    return;
  }
  const g = hit(mx, my);
  if (g !== S.hover) { S.hover = g; schedule(); }
  showTip(g, mx, my);
});
function endPtr(e) {
  const was = drag; ptrs.delete(e.pointerId); pinch = null;
  if (!ptrs.size) { cv.classList.remove("drag"); drag = null; }
  if (was && was.moved < 6 && performance.now() - was.t < 600 && e.type === "pointerup") {
    const r = cv.getBoundingClientRect(), g = hit(e.clientX - r.left, e.clientY - r.top);
    onClick(g, e.clientX - r.left, e.clientY - r.top, e.pointerType);
  }
}
cv.addEventListener("pointerup", endPtr); cv.addEventListener("pointercancel", endPtr);
cv.addEventListener("pointerleave", () => { if (!ptrs.size) { S.hover = -1; showTip(-1); schedule(); } });
function onClick(g, mx, my, type) {
  showTip(-1);
  if (g < 0) { S.sel = -1; hidePop(); schedule(); return; }
  S.sel = g; showPop(g, mx, my); schedule();
}
function showPop(g, mx, my) {
  const p = $("#pop"); $("#popName").textContent = labels[g]; p.dataset.g = g; p.style.display = "block";
  const rw = $("#stage").getBoundingClientRect();
  p.style.left = Math.min(Math.max(mx + 10, 6), rw.width - p.offsetWidth - 6) + "px";
  p.style.top = Math.min(Math.max(my + 10, 6), rw.height - p.offsetHeight - 6) + "px";
}
function hidePop() { $("#pop").style.display = "none"; }
$("#pop").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return; const g = +$("#pop").dataset.g;
  if (b.dataset.a === "start") setStart(g); else setEnd(b.dataset.a, g, true);
  hidePop();
});
function setStart(g) {
  S.start = g; $("#start").value = g === undefined ? "" : labels[g]; S.showSim = true; $("#showSim").checked = true;
  updateSim(); saveHash(); schedule();
}
// URL hash keeps the choices
function saveHash() {
  const q = [];
  const add = (k, v) => q.push(k + "=" + encodeURIComponent(v));
  if (S.from >= 0) add("from", names[S.from]); if (S.to >= 0) add("to", names[S.to]);
  if (S.start !== undefined) add("start", names[S.start]);
  add("mode", S.mode); add("ex", $("#ex").value); add("seed", $("#seed").value);
  if (S.showReal) add("real", 1);
  for (const [k, v] of Object.entries(S.over)) add("o_" + k, v); // values set by hand (explore sets the rest)
  if (S.tieLink) { add("tie", 1); add("tl0", $("#tl0").value); add("tl1", $("#tl1").value); }
  if (HAS_TASTE && HL !== D.hlDefault) add("hl", HL);
  if (HAS_TASTE && CAP !== (CFG.max_genre_share || 1)) add("cap", CAP);
  if (SZ_T !== 1) add("szt", SZ_T); if (SZ_O !== 1) add("szo", SZ_O);
  try { history.replaceState(null, "", "#" + q.join("&")); } catch (e) { /* sandboxed */ }
}
function loadHash() {
  const h = new URLSearchParams(location.hash.slice(1)), g = (k) => (h.has(k) && idx.has(h.get(k)) ? idx.get(h.get(k)) : -1);
  if (h.get("ex")) $("#ex").value = h.get("ex");
  for (const k of Object.keys(OV)) if (h.has("o_" + k) && h.get("o_" + k) !== "") S.over[k] = +h.get("o_" + k);
  if (h.get("tie")) { S.tieLink = true; $("#tie").checked = true; $("#tl0").value = h.get("tl0"); $("#tl1").value = h.get("tl1"); }
  if (h.get("seed")) $("#seed").value = h.get("seed");
  const m = h.get("mode"); if (["taste", "data", "blend"].includes(m) && (HAS_TASTE || m === "data")) S.mode = m;
  if (g("from") >= 0) { S.from = g("from"); $("#from").value = labels[S.from]; }
  if (g("to") >= 0) { S.to = g("to"); $("#to").value = labels[S.to]; }
  if (g("start") >= 0) { S.start = g("start"); $("#start").value = labels[S.start]; }
  applyTh();
  for (const [k, id] of [["szt", "szT"], ["szo", "szO"]]) if (h.get(k)) { const v = Math.min(Math.max(+h.get(k) || 1, 0.2), 3);
    $("#" + id).value = v; if (k === "szt") SZ_T = v; else SZ_O = v; $("#" + id + "v").textContent = v.toFixed(1); }
  if (h.get("real")) { S.showReal = true; $("#showReal").checked = true; }
}
$("#zin").onclick = () => { zoomAt(W / 2, H / 2, 1.5); schedule(); };
$("#zout").onclick = () => { zoomAt(W / 2, H / 2, 1 / 1.5); schedule(); };
$("#fit").onclick = () => { fitView(); schedule(); };

// ---- autocomplete
const lowers = labels.map((l, i) => (l + " " + names[i]).toLowerCase());
function matches(q, limit, only) {
  q = q.trim().toLowerCase(); if (!q) return [];
  const a = [], b = [], c = [];
  for (let i = 0; i < N; i++) {
    if (only && !only(i)) continue;
    const l = labels[i].toLowerCase(), n = names[i].toLowerCase();
    if (l.startsWith(q) || n.startsWith(q)) a.push(i);
    else if (l.includes(" " + q) || n.includes(" " + q)) b.push(i);
    else if (lowers[i].includes(q)) c.push(i);
  }
  const by = (x, y) => deg[y] - deg[x];
  return [...a.sort(by), ...b.sort(by), ...c.sort(by)].slice(0, limit);
}
function autocomplete(input, box, onPick, only) {
  let items = [], on = -1;
  const close = () => { box.style.display = "none"; on = -1; };
  const render = () => {
    items = matches(input.value, 8, only);
    if (!items.length) return close();
    box.innerHTML = items.map((g, i) => `<div data-i="${i}" class="${i === on ? "on" : ""}">${esc(labels[g])}${labels[g] !== names[g] ? ` <small>${esc(names[g])}</small>` : ""} <small>· ${deg[g]} links</small></div>`).join("");
    box.style.display = "block";
  };
  const pick = (g) => { input.value = labels[g]; close(); onPick(g); };
  input.addEventListener("input", render);
  input.addEventListener("focus", render);
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { on = Math.min(on + 1, items.length - 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { on = Math.max(on - 1, 0); render(); e.preventDefault(); }
    else if (e.key === "Enter" && items.length) { pick(items[Math.max(on, 0)]); e.preventDefault(); }
    else if (e.key === "Escape") close();
  });
  box.addEventListener("mousedown", (e) => { const d = e.target.closest("[data-i]"); if (d) { pick(items[+d.dataset.i]); e.preventDefault(); } });
  input.addEventListener("blur", () => setTimeout(close, 120));
}
function ensureVisible(g) { // a genre outside the taste view: switch to the whole graph
  if (S.mode === "taste" && !inTaste[g]) setMode(HAS_TASTE ? "blend" : "data");
}
function setEnd(which, g, fromClick) {
  S[which] = g; $("#" + which).value = labels[g];
  if (!fromClick) { ensureVisible(g); centerOn(g, 4); }
  updateRoute(); saveHash(); schedule();
}
autocomplete($("#q"), $("#qac"), (g) => { ensureVisible(g); S.sel = g; centerOn(g, 5); schedule(); });
autocomplete($("#from"), $("#fac"), (g) => setEnd("from", g, false));
autocomplete($("#to"), $("#tac"), (g) => setEnd("to", g, false));
autocomplete($("#start"), $("#sac"), (g) => setStart(g));
$("#start").addEventListener("input", () => { if (!$("#start").value.trim()) { setStart(undefined); } });
$("#swap").onclick = () => { [S.from, S.to] = [S.to, S.from]; $("#from").value = S.from >= 0 ? labels[S.from] : ""; $("#to").value = S.to >= 0 ? labels[S.to] : ""; updateRoute(); schedule(); };
$("#clr").onclick = () => { S.start = undefined; $("#start").value = ""; S.from = S.to = -1; $("#from").value = $("#to").value = ""; updateRoute(); updateSim(); saveHash(); schedule(); };

// ---- route panel
function updateRoute() {
  const out = $("#routeOut");
  S.route = null;
  if (S.from < 0 || S.to < 0) { out.innerHTML = S.from >= 0 ? '<span class="m">Pick the second genre.</span>' : ""; return; }
  if (S.from === S.to) { out.textContent = "Same genre."; return; }
  if (!nodeOk(S.from) || !nodeOk(S.to)) { out.innerHTML = '<span class="m">A chosen genre is hidden by the weight range.</span>'; return; }
  const p = routeBetween(S.from, S.to);
  if (!p) { out.innerHTML = `<b>No route</b> between ${esc(labels[S.from])} and ${esc(labels[S.to])}: they are in different, disconnected parts of the graph.`; return; }
  let cost = 0, weak = 1; const ws = [];
  for (let i = 0; i + 1 < p.length; i++) { const w = adj[p[i]].get(p[i + 1]); ws.push(w); cost += 1.05 - w; weak = Math.min(weak, w); }
  S.route = { path: p, ws, cost };
  const missing = p.filter((g) => P()[2 * g] !== P()[2 * g]).length;
  out.innerHTML = `<b>${p.length - 1} step${p.length > 2 ? "s" : ""}</b>, total cost ${cost.toFixed(2)} (weakest link ${weak.toFixed(2)})<div class="chips">` +
    p.map((g, i) => (i ? `<span class="arrow">${ws[i - 1].toFixed(2)} &rarr;</span>` : "") + `<span class="chip">${esc(labels[g])}</span>`).join("") + "</div>" +
    (missing ? `<div class="m">${missing} stop(s) are outside the Taste view; switch to Data or Blend to see the whole route.</div>` : "");
}

// ---- flow panel
const realKey = () => { const ex = +$("#ex").value; let b = D.explores[0];
  for (const e of D.explores) if (Math.abs(e - ex) < Math.abs(b - ex)) b = e; return b.toFixed(2); };
function chips(path) {
  return '<div class="chips">' + path.map((g, i) => (i ? '<span class="arrow">&rarr;</span>' : "") + `<span class="chip">${esc(labels[g])}</span>`).join("") + "</div>";
}
const OV = { stops: "stops", taste_weight: "tw", min_link_weight: "th", min_genre_weight: "nw", radius: "rad" };
function syncControls(plan) { // show the plan's values on the controls; mark which ones the user set
  const fmt = { stops: (v) => String(v), taste_weight: (v) => v.toFixed(2), min_link_weight: (v) => v.toFixed(2),
    min_genre_weight: (v) => v.toFixed(2), radius: (v) => (v ? String(v) : "off") };
  for (const [k, id] of Object.entries(OV)) {
    $("#" + id).value = plan[k]; $("#" + id + "v").textContent = fmt[k](plan[k]);
    const t = $("#" + id + "T"), set = S.over[k] !== undefined; t.textContent = set ? "set" : "auto"; t.classList.toggle("set", set);
  }
}
function updateSim() {
  if (!HAS_TASTE) return;
  const ex = +$("#ex").value, seed = +$("#seed").value || 0;
  $("#exv").textContent = ex.toFixed(2);
  const plan = flowPlan();
  if (TH !== plan.min_link_weight || NW_LO !== plan.min_genre_weight) { TH = plan.min_link_weight; NW_LO = plan.min_genre_weight; applyTh(); }
  syncControls(plan); thText(); nwText();
  S.sim = simulate(plan, seed, S.start);
  S.plan = plan;
  const fam = S.sim.length ? (S.sim.reduce((s, g) => s + (SP.f.get(g) || 0), 0) / S.sim.length).toFixed(2) : "0";
  $("#simOut").innerHTML = `<b style="color:var(--sim)">Simulated</b> <span class="m">(seed ${seed}, familiarity ${fam})</span>${chips(S.sim)}`;
  const shown = Object.entries(plan).filter(([k]) => !["warnings", "explicit"].includes(k)).map(([k, v]) => `${k} ${v}`).join(" · ");
  $("#planOut").innerHTML = `<span class="m">plan: ${esc(shown)}${plan.explicit.length ? " · set: " + plan.explicit.join(", ") : ""}</span>` +
    plan.warnings.map((w) => `<div style="color:var(--route)">&#9888; ${esc(w)}</div>`).join("");
  const key = realKey(), reals = D.real[key] || [];
  $("#realOut").innerHTML = `<b>Real paths</b> <span class="m">explore ${key} with its own default values${CAP < 1 ? ", computed without the max share" : ""}, seeds ${D.seeds.join(", ")}${+key !== ex ? " (nearest precomputed)" : ""}</span>` +
    reals.map((p, i) => `<div class="pl"><i style="background:${REAL_COL[i % 6]}"></i>${p.map((g) => esc(labels[g])).join(" &rarr; ")}</div>`).join("");
}
function thText() { const [ka] = keptEdges(); $("#thn").textContent = `(${ka} of ${nE} links)`; }
function nwText() { const vis = visNodes[S.mode] || []; $("#nwn").textContent = `(${vis.filter(shown).length} of ${vis.length} genres)`; }
for (const id of ["szT", "szO"]) $("#" + id).addEventListener("input", () => {
  const v = +$("#" + id).value; if (id === "szT") SZ_T = v; else SZ_O = v;
  $("#" + id + "v").textContent = v.toFixed(1); saveHash(); schedule();
});
for (const [k, id] of Object.entries(OV)) $("#" + id).addEventListener("input", () => {
  S.over[k] = +$("#" + id).value; updateSim(); updateRoute(); saveHash(); schedule();
});
$("#reset").onclick = () => { S.over = {}; updateSim(); updateRoute(); saveHash(); schedule(); };
$("#tie").onchange = (e) => { S.tieLink = e.target.checked; delete S.over.min_link_weight; updateSim(); updateRoute(); saveHash(); schedule(); };
for (const id of ["tl0", "tl1"]) $("#" + id).addEventListener("input", () => { if (S.tieLink) { updateSim(); updateRoute(); saveHash(); schedule(); } });
for (const id of ["ex", "seed"]) $("#" + id).addEventListener("input", () => { updateSim(); updateRoute(); saveHash(); schedule(); });
$("#reroll").onclick = () => { $("#seed").value = Math.floor(Math.random() * 1e6); updateSim(); saveHash(); schedule(); };
$("#showSim").onchange = (e) => { S.showSim = e.target.checked; schedule(); };
$("#showReal").onchange = (e) => { S.showReal = e.target.checked; saveHash(); schedule(); };

// ---- modes, legend, layout of the page
function setMode(m) {
  if (m === S.mode) return;
  S.mode = m; S.hover = -1; showTip(-1);
  document.querySelectorAll("#modes button").forEach((b) => b.classList.toggle("on", b.dataset.m === m));
  if (m === "taste") { for (const k of ["from", "to"]) if (S[k] >= 0 && !inTaste[S[k]]) { S[k] = -1; $("#" + k).value = ""; } if (S.sel >= 0 && !inTaste[S.sel]) S.sel = -1; }
  applyTh(); fitView(); updateRoute(); updateSim(); legend(); nwText(); saveHash(); schedule();
}
function legend() {
  const m = S.mode, L = [];
  const line = (col, w, a, txt) => `<span class="sw" style="border-color:${col};border-top-width:${w}px;opacity:${a}"></span><span>${txt}</span>`;
  const wl = [0.15, 0.4, 0.7, 1].map((w) => line("var(--ink)", (0.5 + 3 * w).toFixed(1), 0.3 + 0.6 * w, `weight ${w}`)).join("");
  L.push(`<div class="leg">${wl}</div><p class="sub">Link width and opacity grow with weight (0..1).</p>`);
  L.push(`<div class="leg">${line("var(--wiki)", 2, 1, "Wikidata (subgenre / influence)")}${line("var(--learned)", 2, 1, "learned (co-tag, similar artists)")}${line("var(--yandex)", 2, 1, "Yandex genre tree")}</div>`);
  if (m === "blend") L.push(`<div class="leg">${line("var(--accent)", 3, 1, "overlap: link between two of your genres")}${line("var(--ink)", 1, 0.3, "data only: link outside your taste")}` +
    `<span><span class="dot" style="background:hsl(338 80% 50%)"></span></span><span>your genre (size and colour = share)</span>` +
    `<span><span class="dot" style="background:var(--spread);opacity:.5"></span></span><span>nearby: spread score r (faint halo)</span>` +
    `<span><span class="dot" style="background:var(--dimnode)"></span></span><span>data only: not in your taste or nearby</span></div>`);
  if (m === "taste") L.push(`<div class="leg"><span><span class="dot" style="background:hsl(338 80% 50%)"></span></span><span>your genre, size = share</span></div><p class="sub">Only your profile genres; a link shows where the main graph links two of them directly. Isolated genres are shown too.</p>`);
  if (m === "data") L.push(`<div class="leg"><span><span class="dot" style="background:hsl(210 50% 52%)"></span></span><span>node colour = Wikidata root family; size = number of links</span></div>`);
  L.push(`<div class="leg"><span><span class="sw" style="border-color:var(--route)"></span></span><span>route</span><span><span class="sw" style="border-color:var(--sim)"></span></span><span>simulated flow path</span>` +
    (HAS_TASTE ? REAL_COL.map((c, i) => `<span><span class="sw" style="border-color:${c}"></span></span><span>real path, seed ${D.seeds[i]}</span>`).join("") : "") + "</div>");
  $("#legend").innerHTML = L.join("");
}

function resize() {
  const r = cv.getBoundingClientRect(); DPR = Math.min(window.devicePixelRatio || 1, 2.5);
  const oldW = W, oldH = H; W = Math.max(r.width, 1); H = Math.max(r.height, 1);
  cv.width = Math.round(W * DPR); cv.height = Math.round(H * DPR);
  if (oldW) { view.x += (W - oldW) / 2; view.y += (H - oldH) / 2; } else fitView();
  schedule();
}
function init() {
  readColors(); hlInit(); setupModes(); loadHash();
  const sub = `${D.meta.nodes} genres, ${D.meta.edges} links` + (D.meta.learned ? ", learned weights" : "") +
    (HAS_TASTE ? ` · taste: ${D.meta.taste_nodes} genres` : " · no taste input");
  $("#sub").textContent = sub;
  $("#about").textContent = "Positions are computed once offline (seeded force layout) and frozen: nothing moves. Pinch or Ctrl+wheel zooms around the cursor, two-finger drag or wheel pans, mouse drag pans. Hover a genre for details; click two genres to route between them. Flow simulation uses a JS port of plan_path with its own PRNG, so a seed gives a different path than Python's random module would; 'real' paths are planned by the actual Python code.";
  if (!HAS_TASTE) {
    $('#modes [data-m="taste"]').disabled = true; $('#modes [data-m="blend"]').disabled = true;
    $("#flowNote").textContent = "Needs the listener's taste (run with --yandex-export)."; $("#flowCtl").classList.add("hide");
  }
  document.querySelectorAll("#modes button").forEach((b) => { b.classList.toggle("on", b.dataset.m === S.mode); b.onclick = () => { if (!b.disabled) setMode(b.dataset.m); }; });
  $("#panelBtn").onclick = () => $("#side").classList.toggle("open");
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { readColors(); legend(); schedule(); });
  if (HAS_TASTE) { $("#tl0").value = CFG.min_link_weight[0]; $("#tl1").value = CFG.min_link_weight[1]; }
  if (location.hash.includes("tie=1")) { const h = new URLSearchParams(location.hash.slice(1)); $("#tl0").value = h.get("tl0"); $("#tl1").value = h.get("tl1"); }
  thText(); nwText(); legend(); updateSim(); updateRoute();
  new ResizeObserver(resize).observe(cv); resize();
}
window.__atlas = { D, view, S, get adj() { return adj; }, nodeOk: (g) => nodeOk(g), get TH() { return TH; }, get NW() { return NW_LO; }, routeBetween, fit, planPath, simulate, verifyPath, P, names, idx, hit, fitView, setMode, zoomRel,
  get fitK() { return fitK; }, draw };
init();
})();
</script>
</body>
</html>
"""  # noqa: E501

if __name__ == "__main__":
    main()
