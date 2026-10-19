# Post-radio

Tunable "exploration radio", like ListenBrainz's Weekly Exploration but seeded from a track or artist you pick,
with two knobs: how far it wanders (`explore`) and how mainstream it gets (`popularity`). It also pulls LB's
own Weekly Exploration/Jams playlists. **No API key needed**; an LB token only adds a second track source.

Full documentation lives in [docs/](docs/README.md): [architecture](docs/architecture.md),
[classes](docs/classes.md), [configuration](docs/configuration.md) and [dependencies](docs/dependencies.md).

## Use

```
postradio radio --artist "Radiohead" -e 0.7            # explore 0..1
postradio radio --track "Radiohead - Karma Police" -e 0.4 -p 0.1   # popularity 0 = deep cuts, 1 = hits
postradio radio --artist-mbid <mbid> --save "late night"           # keep it
postradio radio --genre "shoegaze,dream pop" --genre-op and   # genre seed (LB tags + Deezer genres)
postradio radio --artist "Radiohead" --unheard artists   # tracks: drop played tracks; artists: drop known artists
postradio feedback import loves.json / postradio feedback batches   # your own loves/hates, dated batches
postradio deep-cuts [--user U] [--kind exploration|jams] [--save]  # LB Weekly Exploration, both weeks
postradio saved [name]
postradio serve                                          # HTTP API on 127.0.0.1:8765
```
Global flags: `--config path.yaml`, `--no-token`, `--json`, `--offline`. Radio history: `--lb-user U`,
`--history-file plays.jsonl|csv`, `--no-history`.

### From Python (how Post-Vinyl uses it)

```python
from postradio.api import Recommender
rec = Recommender()                                   # config.yaml / $POSTRADIO_CONFIG / defaults
res = rec.generate(artist="Radiohead", explore=0.7)  # blocking I/O: run in a worker thread
rec.save(res["result_id"], name=None)                 # default name: <date>_<seed>_e<explore>_p<popularity|auto>
rec.deep_cuts(user="LOSOSb")                          # {"weeks": [{"week": ..., "tracks": [...]}, ...]}
rec.list_saved(); rec.load_saved(name); rec.get(result_id)
```
Results are held in memory (`storage.result_ttl_s`, `result_cache_size`) until saved; saving writes JSON to
`storage.save_dir`. `Recommender(history=...)` accepts any `HistoryProvider`, e.g. a Maloja/multi-scrobbler adapter.

HTTP (`postradio serve`): `POST /radio`, `GET /deep-cuts`, `GET /results/{id}`, `POST /results/{id}/save`,
`GET /saved`, `GET /saved/{name}`.

## Taste (loves / hates / ratings)

On by default (`feedback.enabled`, or `--no-feedback`). Sources, in priority order (the first with an
opinion on a track wins): **your imported JSON** (`postradio feedback import`; an append-only, dated log in
`<save_dir>/feedback/opinions.jsonl` where the newest opinion per item wins and only the newest
`advanced.feedback_store_depth` opinions are read; the format is in `providers/feedback_store.py`), **Navidrome** (stars = +1; ratings 1★..5★ map to −1..+1 with 3★ neutral; artist
and album stars/ratings count for the artist) and **LB** loves/hates (tokenless).
Effects:
- An artist's weight is multiplied by `love_factor` (1.5) to the power of a positive score, or by
  `hate_factor` (0.3) to the power of minus a negative score.
- Same for an exact track.
- The 50 strongest track opinions spread to their LB similar-recordings, scaled by similarity.

A track seed also injects its own similar recordings. Navidrome needs `feedback.navidrome_url` (or
`$NAVIDROME_URL`), `$NAVIDROME_USERNAME` and `$NAVIDROME_PASSWORD`.

## Stream caps (`advanced.mix`, per run `--mix`)

Each candidate stream (`deezer-top`, `lb`, `deezer-radio`, `troi`, `track-sim`, `lb-tags`) has a cap from 0 to 1.
1 keeps everything and 0 turns the stream off. The cap applies to the whole stream: `deezer-top: 0.2` keeps 20%
of all Deezer catalogue candidates across every artist, those closest to the popularity target. `track-sim` keeps
LB's most-similar-first order instead. A cap only touches its own stream, so lowering the catalogue streams
leaves more room for troi and radio. Example run: `postradio radio --artist X --mix "deezer-top=0.2,lb=0.2"`.

## Config

Copy `config.example.yaml` to `config.yaml`; only include keys you change. The user sections are
`defaults`, `storage`, `sources`, `feedback` and `server`. `advanced` holds every algorithm constant (weights,
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

## Cost (cold cache, Radiohead, explore 0.6, n=50, token + LB user)

About 2 s of CPU, under 100 MB RAM, and around 275 requests, mostly sequential, in about 130 s. About 23 s of
that is MusicBrainz's 1 req/s limit and the rest is request latency. A warm rerun takes about 0.7 s and makes
no requests. The SQLite cache is about 16 MB after one cold run (7-day TTL).

## Dev

```
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/python scripts/gen_config_example.py   # after changing postradio/config.py
.venv/bin/pytest -q && .venv/bin/ruff check postradio tests
```
