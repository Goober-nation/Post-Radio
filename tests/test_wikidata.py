"""Wikidata genre graph: SPARQL bindings in, collapsed MusicBrainz-genre graph out."""
import pytest

from postradio.providers.wikidata import Wikidata, build_graph


def b(**kw):
    return {k: {"value": (f"http://www.wikidata.org/entity/{v}" if k in ("a", "p", "g", "r") else v)}
            for k, v in kw.items()}


MB = {"m-hiphop": "hip hop", "m-trap": "trap", "m-drill": "drill", "m-grime": "grime"}
ROWS = [
    b(a="Q1", mb="m-hiphop"),
    b(a="Q2", p="Q1"),  # Wikidata-only step ("American hip hop"): collapsed
    b(a="Q3", mb="m-trap", p="Q2"),
    b(a="Q4", mb="m-drill", p="Q3"),
    b(a="Q5", mb="m-grime", p="Q1"),
    b(a="Q6", mb="m-gone"),  # P8052 MusicBrainz no longer lists: dropped
    {"a": "broken row"},
]
LABELS = [b(a="Q1", aLabel="hip hop music"), b(a="Q3", aLabel="trap music"), b(a="Q4", aLabel="drill")]
INFL = [b(g="Q5", r="Q4"), b(g="Q5", r="Q6")]


def test_build_graph_collapses_wikidata_only_steps_and_sets_roots_and_depth():
    g = build_graph(ROWS, INFL, MB, LABELS)
    edges = {(e["src"], e["dst"], e["kind"]) for e in g["edges"]}
    assert edges == {("trap", "hip hop", "subgenre_of"), ("drill", "trap", "subgenre_of"),
                     ("grime", "hip hop", "subgenre_of"), ("grime", "drill", "influenced_by")}
    assert set(g["nodes"]) == {"hip hop", "trap", "drill", "grime"}
    assert g["nodes"]["drill"] == {"qid": "Q4", "label": "drill", "roots": ["hip hop"], "depth": 2}
    assert g["nodes"]["hip hop"]["depth"] == 0 and g["nodes"]["hip hop"]["roots"] == ["hip hop"]
    assert g["nodes"]["hip hop"]["label"] == "hip hop music" and g["nodes"]["grime"]["label"] == "Q5"  # no label


def test_adapter_sends_sparql_through_http_and_rejects_empty_answers():
    class Http:
        def __init__(self, answer):
            self.answer, self.seen = answer, []

        def get(self, url, params=None, headers=None, timeout=20, **_):
            self.seen.append((url, headers, timeout))
            return self.answer(params["query"]) if callable(self.answer) else self.answer

    http = Http(lambda q: {"results": {"bindings": INFL if "P737" in q else LABELS if "rdfs:label" in q else ROWS}})
    assert len(Wikidata(http).genre_graph(MB)["edges"]) == 4
    assert http.seen[0][1]["Accept"] == "application/sparql-results+json" and http.seen[0][2] == 180
    with pytest.raises(RuntimeError):
        Wikidata(Http(None)).genre_graph(MB)
