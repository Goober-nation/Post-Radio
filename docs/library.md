# Using Post-Radio as a library

For a host app (Post-Vinyl, the survey page, your own project) that calls Post-Radio from Python. The CLI and
`postradio serve` are thin wrappers around the same calls.

## Install

```bash
pip install "postradio @ git+https://github.com/Goober-nation/Post-Radio@flow-integration"
```

Add the `server` extra (`postradio[server] @ ...`) only if you run the HTTP API.

## One Recommender per deployment

```python
from postradio import config
from postradio.api import Recommender

rec = Recommender(home="/data/postradio")      # cache/, data/, state/ under one folder you own
# or: Recommender(config.load("/config/postradio.yaml"), home=..., data_dir="/store")  # shared read-only store
...
rec.close()                                    # on shutdown (or use it as a context manager)
```

- **Construct it once.** It holds the HTTP cache, the MusicBrainz store and a thread pool, and it is
  thread-safe: call it from several worker threads at once.
- **Nothing is implicit.** It reads no environment variables, no `.env` and no `./config.yaml`, and it
  writes only under `home`, or the folders you pass. With no `home`, it uses the user's own folders.
- **The optional data** (the MusicBrainz store, ~600 MB) goes into `<home>/data`. Install it once per
  deployment, e.g. in an entrypoint or admin task:
  ```python
  rec.data_check()   # what would change (downloads only the manifest)
  rec.data_fetch()   # newest data-YYYY-MM release, verified by checksum
  ```
  Everything works without it, just slower and with less precise artist matching.

In compose, mount one host folder and pass it as `home`. Post-Radio needs no environment variables of its
own:

```yaml
services:
  postvinyl:
    volumes:
      - ./data:/data            # the host app's data; Post-Radio under /data/postradio
```

## Per-user calls

Pass each user's taste with the call. It replaces the configured taste for that call only, and concurrent
calls never see each other's.

```python
from postradio.api import Listener
from postradio.models import Like, Play

listener = Listener(
    likes=[Like("Slowdive", "Alison", at=starred_at_unix)],    # e.g. Navidrome stars; dislikes: score=-1
    plays=[Play("Ride", "Vapour Trail", timestamp=played_at)],  # listening history, if you have it
    lb_token=decrypted_token,                                   # optional; this call only, never stored
)

flow = rec.flow(explore=0.5, n=30, listener=listener)                    # from the user's likes, no seed
radio = rec.generate(artist="Radiohead", explore=0.7, listener=listener)  # seeded
preview = rec.flow(explore=0.5, listener=listener, dry=True)             # genre path only, ~1 s
```

Instead of data, a `Listener` can name sources for Post-Radio to fetch itself: `yandex_playlist` (a public
Yandex playlist link) or `lb_user` (ListenBrainz loves and listening stats).

**Seeds:** one kind per radio (artists, tracks, MBIDs or genres, never mixed). `artist`, `track`,
`artist_mbid` and `recording_mbid` take a list of up to 5: they become one graph, and artists close to more
than one seed rank higher (`advanced.blend_bonus`). `res.seeds` lists them; `res.seed` is the first.

**Taste switch:** `generate(..., taste=False)` stops likes and dislikes from changing scores. Disliked tracks
and artists are still dropped, and `unheard` still uses history.

**Flow start:** `flow(..., start="jazz rap")` makes that genre the first stop (any genre of the graph; an
unknown name raises `InvalidInput` with close names). Unset, the walk starts at one of the listener's three
strongest genres. From a genre the listener has none of, a low explore heads back toward their own genres.

**Token:** with an `lb_token`, a cold flow takes ~20 s instead of minutes. ListenBrainz titles settle which
same-named Deezer artist is meant, and ListenBrainz becomes a track source too.

## Results

`generate` and `flow` return a `postradio.results.Playlist`:

```python
for t in flow.tracks:                 # PlaylistTrack
    t.artist, t.title                 # always set
    t.artist_mbid                     # set whenever the artist was resolved
    t.recording_mbid, t.deezer_id     # when the source had them (Deezer tracks: usually no recording MBID)
    t.familiar                        # the user already knows this artist
    t.stop or t.hop                   # flow: genre stop; radio: graph distance from the seed
    t.via                             # why the artist is in: radio: linking sources; flow: "likes" / "genre:<source>"
    t.artist_popularity               # 0..1 the artist's fame, when a source reported it
flow.genre_path, flow.rng_seed        # pass rng_seed back to repeat the run
flow.stats["input"]                   # what was usable of the taste you passed (below)
flow.to_dict()                        # plain JSON; Playlist.from_dict() reads it back
```

**How much of the input was usable:** flow results (`stats["input"]`), flow previews and profiles (`.input`)
count the likes received, distinct artists, artists considered (the most-liked, capped), matched to
MusicBrainz, and with genres, plus whether the local store was used:

```python
{"likes": 952, "artists": 176, "considered": 176, "matched": 133, "with_genres": 121, "store": True}
```

Few matched artists means a weak profile: worth telling the user, or asking for more likes.

**Repeatable runs:** every call reports `rng_seed`. Passing it back gives the same path, and the same
playlist while the cached API answers are unchanged.

**Nothing is stored:** results are kept only with `keep=True` (then `rec.get(id)` and `rec.save(id)` work).

**Faking in tests:** a host app's test double can return `Playlist.from_dict({...})` with hand-written
tracks.

## Progress

Pass `progress=` to report long runs:

```python
def on_progress(p):        # postradio.results.Progress; called from worker threads: keep it quick
    job.update(stage=p.stage, done=p.done, total=p.total)

rec.flow(explore=0.5, listener=listener, progress=on_progress)
```

- **Flow stages:** `likes`, `profile`, `path`, `pools`, `catalogues` (done/total), `fill`, `done`.
- **Radio stages:** `seed`, `graph`, `catalogues`, `radio`, `scoring`, `done`.
- **Logging:** prose goes to the standard `logging` logger `postradio`.
- **Over HTTP:** `POST /jobs/flow` and `GET /jobs/{id}` do the same.

## Stopping a run

```python
stop = threading.Event()
rec.flow(explore=0.5, listener=listener, cancel=stop)   # stop.set() from another thread stops it
rec.flow(explore=0.5, listener=listener, timeout_s=60)  # or a deadline
```

Both raise `postradio.errors.Cancelled` (`timed_out` tells which) at the next stage or fetch, usually within
a second. Fetches already sent finish in the background, and their answers still fill the cache.

## Errors

Catch `postradio.errors.PostRadioError`, or the specific class:

| class | when | suggested HTTP |
|---|---|---|
| `InvalidInput` | Bad parameters, a bad playlist link, no likes to build a flow from, a genre no source knows (the message suggests close names) | 422 |
| `NotFound` | The seed artist or track doesn't exist | 404 |
| `SourceAuthError` | A credential was refused (Navidrome login) | 422 |
| `SourceUnavailable` | A service is down or blocked (e.g. Yandex 451 in some regions) | 503 |
| `Cancelled` | The cancel event was set, or `timeout_s` passed | 499 / 504 |
| `DataError`, `ConfigError` | Broken data release or settings | 500 |

A single failing source (one ListenBrainz endpoint, Deezer for one artist) doesn't raise. That source
contributes nothing, the run continues, and the log says why.

## Threads and time

- Calls block, from about 1 s (`dry`, `profile` with the store) to about 20 s (cold flow with a token) or
  minutes (cold, no token). Run them in a worker thread or background job.
- Concurrent calls share per-host rate limits. MusicBrainz is held to 1 request/s for the whole process,
  so more threads don't make MusicBrainz-heavy work faster.

## Running the server

For a host that isn't Python, or should not install Post-Radio: run `postradio serve` in its own container
(`Dockerfile` in the repo root, compose example in `docs/examples/compose.yaml`) and call it over HTTP.

- `GET /health`: `{"ok", "version", "api", "schema", "store", "data_release"}`. Check `api` at start-up and
  refuse to run against a different one; `schema` is the result format (also in every result).
- Long runs as jobs: `POST /jobs/flow|radio|profile` (202 + `status_url`), poll `GET /jobs/{id}`,
  `DELETE /jobs/{id}` to cancel (status `cancelled`). At most `server.job_workers` run at once; the rest
  read `queued`. Queues per user, positions and limits are the host's to keep.
- Sync calls (`POST /flow`, `/radio`) and jobs take `timeout_s`; a sync call past it answers 504.
- Requests carry the listener (`likes`, `plays`, `yandex_playlist`, `lb_token`, ...) as in the library.
