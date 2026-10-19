# postvinyl-recs

Tunable "exploration radio", like ListenBrainz's Weekly Exploration but seeded from a track or artist you pick,
with two knobs: how far it wanders (`explore`) and how mainstream it gets (`popularity`). It also pulls LB's
own Weekly Exploration/Jams playlists. **No API key needed**; an LB token only adds a second track source.

## Use

```
pvrecs radio --artist "Radiohead" -e 0.7            # explore 0..1
pvrecs radio --track "Radiohead - Karma Police" -e 0.4 -p 0.1   # popularity 0 = deep cuts, 1 = hits
pvrecs radio --artist-mbid <mbid> --save "late night"           # keep it
pvrecs deep-cuts [--user U] [--kind exploration|jams] [--save]  # LB Weekly Exploration, both weeks
pvrecs saved [name]
pvrecs serve                                          # HTTP API on 127.0.0.1:8765
```
Global flags: `--config path.yaml`, `--no-token`, `--json`, `--offline`. Radio history: `--lb-user U`,
`--history-file plays.jsonl|csv`, `--no-history`.

### From Python (how Post-Vinyl uses it)

```python
from pvrecs.api import Recommender
rec = Recommender()                                   # config.yaml / $PVRECS_CONFIG / defaults
res = rec.generate(artist="Radiohead", explore=0.7)  # blocking I/O: run in a worker thread
rec.save(res["result_id"], name=None)                 # default name: <date>_<seed>_e<explore>_p<popularity|auto>
rec.deep_cuts(user="LOSOSb")                          # {"weeks": [{"week": ..., "tracks": [...]}, ...]}
rec.list_saved(); rec.load_saved(name); rec.get(result_id)
```
Results are held in memory (`storage.result_ttl_s`, `result_cache_size`) until saved; saving writes JSON to
`storage.save_dir`. `Recommender(history=...)` accepts any `HistoryProvider`, e.g. a Maloja/multi-scrobbler adapter.

HTTP (`pvrecs serve`): `POST /radio`, `GET /deep-cuts`, `GET /results/{id}`, `POST /results/{id}/save`,
`GET /saved`, `GET /saved/{name}`.

## Config

Copy `config.example.yaml` to `config.yaml`; only include keys you change. The user sections are
`defaults`, `storage`, `sources` and `server`. `advanced` holds every algorithm constant (weights,
fanouts, curve widths, caps, rate limits). Unknown keys are an error.

## Pipeline

1. **Seed** → MusicBrainz (name/MBID/`Artist - Title`).
2. **Graph**: BFS to `1 + round(2·explore)` hops over LB similar-artists, Deezer related, MB relations
   (hop 1). An artist reached from more than one source gets a bonus. Sparse seeds (fewer than 8 neighbours)
   borrow artists that share their MB tags (falling back to release-group tags) via troi's LB tag radio.
   Deezer artists are matched to MBIDs by shared track/album titles, so same-named artists don't collide.
3. **Candidates**: Deezer top tracks (plus LB popularity with a token), Deezer artist radio, troi LB-radio.
4. **Score**: `affinity × hop_fit × artist_pop_fit × track_pop_fit × familiarity`, plus a multi-source
   bonus and jitter. History lowers familiar artists as explore rises and drops already-played tracks.
   Selection caps tracks per artist and prefers (but doesn't force) a source mix.

## Layout

`models.py` holds the normalised types (`Artist`, `Track`, `Edge`, `Play`, `History`).
`providers/__init__.py` defines the interfaces. `providers/*` are the adapters, the only code that
sees raw API JSON. `engine.py` builds the graph and ranks. `api.py` is the library facade with the
cache and storage. `server.py` and `cli.py` are thin wrappers. `http.py` is a SQLite response cache
with per-host throttling and backoff. MusicBrainz is held to 1 req/s and backs off exponentially on
503 rather than failing.

## Dev

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -q && .venv/bin/ruff check pvrecs tests
```
