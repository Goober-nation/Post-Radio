# Configuration and options

## CLI: `postradio`

Global flags work before or after the subcommand: `--config PATH` (default `$POSTRADIO_CONFIG` or
`./config.yaml`), `--no-token`, `--offline` (cache only) and `--json`. User errors print one line instead
of a traceback.

| Command | Flags (default) |
|---|---|
| `radio` | **Seed, exactly one:** `--artist` · `--track "Artist - Title"` · `--artist-mbid` · `--recording-mbid` · `--genre "a,b"`<br>**Seed options:** `--genre-op or\|and` (or) · `--unheard none\|tracks\|artists` (config; `none` overrides it)<br>**Shape:** `-e/--explore` 0–1 (0.5) · `-p/--popularity` 0–1 (unset = follows explore) · `--auto-popularity` · `-n` (50) · `--per-artist` (2) · `--rng-seed`<br>**History, at most one:** `--lb-user U` · `--history-file F.jsonl\|csv` · `--no-history`<br>**Yandex:** `--yandex URL` (playlist as history + taste) · `--yandex-tracks N`<br>**Taste:** `--no-feedback` · `--navidrome-url URL`<br>**Streams:** `--mix "deezer-top=0.2,troi=1"`<br>**Save:** `--save [NAME]`<br>**Preview:** `--dry` resolves the seed and shows the artist graph by hop (top artists, which source reached them). No catalogues, no tracks, nothing stored. |
| `flow` | `-e/--explore` 0–1 · `-p/--popularity` · `-n` · `--per-artist` · `--stops` (5) · `--rng-seed` · `--save [NAME]` · `--dry` (profile and genre path only: no artist pools, catalogues or tracks)<br>**Taste:** `--yandex URL` · `--yandex-tracks N` · `--navidrome-url`. No seed: the playlist comes from your likes. |
| `profile` | Same taste flags. Prints your genre profile and the nearest genres flow can head to. |
| `deep-cuts` | `--user` (config / `$LISTENBRAINZ_USERNAME`) · `--kind exploration\|jams` (exploration) · `--save [NAME]` |
| `feedback import FILE\|-` | Append a dated loves/hates batch |
| `feedback batches` | List imported batches with dates |
| `saved [name]` | List saved playlists, or print one |
| `data build` | `--out` (dist) · `--skip graph,dump,weights` · `--keep-dump` · `--seeds` (1000) · `--similar` (10). Builds into `storage.data_dir` and packages `dist/data-YYYY-MM/`. All steps take about 30 min and ~3 GB of disk. |
| `data fetch` | `--tag data-YYYY-MM` (newest) · `--no-store` (genre files only) · `--dry` (just check: downloads only `manifest.json`, lists each file as same / changed / new and the download size, installs nothing). Verifies against `manifest.json`, then installs into `storage.data_dir`. |
| `data status` | Shows the files in `storage.data_dir`, the installed release and the last build |
| `serve` | `--host` (127.0.0.1) · `--port` (8765) |

Environment variables: `LISTENBRAINZ_TOKEN`, `LISTENBRAINZ_USERNAME`, `POSTRADIO_CONFIG`, `NAVIDROME_URL`,
`NAVIDROME_USERNAME`, `NAVIDROME_PASSWORD`. A `.env` file in the repository root is loaded at startup and
doesn't override variables already set.

**Seeds.** A radio or flow run without `--rng-seed` picks a seed and reports it (`# rng seed: N`, and
`rng_seed` in the result). Passing it back repeats the run: the same path, and the same playlist while the
cached API answers stay the same. Dry runs report it too, so a preview you like can be run for real.

## Folders

Post-Radio never writes into its install location or the working directory.

| folder | holds | if lost |
|---|---|---|
| `cache/` | API response cache (`http.sqlite`) | Harmless; refills |
| `data/` | MusicBrainz store and genre files (`postradio data fetch`) | Re-downloadable |
| `state/` | Saved playlists, imported loves | User data: back it up |

- **Library:**
  - `Recommender(home=...)` puts all three under one folder.
  - `cache_dir=` / `data_dir=` / `state_dir=` override single ones, e.g. a shared read-only store
    (`data_dir="/store"`, mounted `:ro`).
  - Without either, the user's own folders are used: `~/.cache/postradio`, `~/.local/share/postradio`
    (`data/`, `playlists/`).
  - Precedence: explicit argument > the config's `storage.*` path > `home` > user folders.
- **CLI and `postradio serve`:**
  - `$POSTRADIO_HOME` sets the home.
  - The config file is the first found of: `--config`, `$POSTRADIO_CONFIG`, `$POSTRADIO_HOME/config.yaml`,
    `./config.yaml`, `~/.config/postradio/config.yaml`.
  - `.env` is read from the working directory.
- **Docker / compose:**
  - A host app (Post-Vinyl) passes `home=` from its own data volume, e.g. `/data/postradio`, so no
    `POSTRADIO_*` variables are needed.
  - A standalone container sets `POSTRADIO_HOME=/data` and mounts a host folder there.

## Environment

Only the CLI and the standalone server read environment variables. They pass `os.environ` to
`Recommender(env=...)`; the library reads none by default. Variables: `LISTENBRAINZ_TOKEN`,
`LISTENBRAINZ_USERNAME`, `NAVIDROME_URL`, `NAVIDROME_USERNAME`, `NAVIDROME_PASSWORD`, plus `POSTRADIO_HOME`
and `POSTRADIO_CONFIG` for finding files. A library caller passes `token=` (and later per call) instead.

## Errors

Every error raised on purpose is a `postradio.errors.PostRadioError`:

| class | meaning | HTTP |
|---|---|---|
| `InvalidInput` | Malformed request: range, MBID, link, unknown stream or step | 422 |
| `NotFound` | The seed, release, result or saved playlist doesn't exist | 404 |
| `SourceAuthError` | A credential was refused (Navidrome login; later LB tokens) | 422 |
| `SourceUnavailable` | A service is unreachable, blocked (Yandex 451) or answered garbage | 503 |
| `ConfigError` | Bad config file or setting | 500 |
| `DataError` | No data release, or a checksum mismatch | 500 |

`InvalidInput`, `NotFound`, `ConfigError` and `DataError` also subclass `ValueError`.

## HTTP API: `postradio serve` (FastAPI, OpenAPI at `/docs`)

| Endpoint | Does |
|---|---|
| `POST /radio` | Body: exactly one of `artist`, `track`, `artist_mbid`, `recording_mbid`, `genres`. Optional: `genre_op`, `unheard`, `explore`, `popularity`, `n` (1–500), `per_artist`, `rng_seed`, `auto_popularity`, `mix` (`{stream: 0..1}`), `dry` (artist graph preview, nothing stored). Returns **422** for invalid input (seed count, malformed MBID or track string, out-of-range values, unknown mix stream) and **404** when the seed doesn't exist. |
| `POST /flow` | Body: `explore`, `popularity`, `n` (1–500), `per_artist`, `stops` (1–20), `rng_seed`, `dry` (profile and path only). Uses the configured likes. **422** without likes. |
| `GET /profile` | The genre profile flow starts from (configured taste), plus nearby genres |
| `POST /profile` | The same for the listener in the body: `likes`, `plays`, `yandex_playlist`, `yandex_tracks`, `lb_user`, `lb_token` |
| `POST /jobs/radio`, `POST /jobs/flow`, `POST /jobs/profile` | Same bodies as `/radio`, `/flow` and `POST /profile`, run in the background. Answers **202** at once with `{job_id, status, status_url}`. Bad input is still rejected up front with 422. At most `server.job_workers` (2) run at once; the rest queue. |
| `GET /jobs/{id}` | `status` (`queued`, `running`, `done`, `error`), `progress` (latest event), `result` (when done; same shape as the synchronous endpoint), `error` (`{type, message, status}`; status is the code the synchronous endpoint would have used). Finished jobs expire after `storage.result_ttl_s`; then 404. |
| `GET /deep-cuts?user=&kind=exploration\|jams` | LB Weekly Exploration or Jams, one entry per week |
| `GET /results/{id}` | An unsaved result from memory; 404 once expired |
| `POST /results/{id}/save` | `{name, overwrite}`; 409 if the file exists |
| `POST /feedback` | Import a loves/hates batch. Typed body `FeedbackImport`: `loves`/`hates` → `artists`, `albums`, `tracks`. |
| `GET /feedback/batches` | Imported batches with dates and sizes |
| `GET /saved`, `GET /saved/{name}` | List saved playlists, or get one |

## Python API

Results are stored nowhere unless asked: pass `keep=True` to `generate` / `flow` / `deep_cuts` to hold one
in memory under a `result_id` (for `get()` and `save()`). The CLI does this for `--save`, and the server does
it always.

```python
from postradio import config
from postradio.api import Recommender

with Recommender(config.load("config.yaml"), home="/data/postradio") as rec:   # or Recommender() = defaults
    pl = rec.generate(artist="Radiohead", explore=0.7, popularity=0.3, mix={"troi": 1.0}, keep=True)
    rec.save(pl.result_id)                      # writes JSON to <state>/playlists
    rec.deep_cuts(user="someone")               # DeepCuts: weeks -> tracks
    rec.import_feedback({"loves": {"artists": ["Slowdive"]}})
```

**Many listeners on one `Recommender`.** Keep one long-lived `Recommender` per deployment, which shares the
store, cache and threads, and pass each user's taste per call:

```python
from postradio.api import Listener
from postradio.models import Like, Play

me = Listener(likes=[Like("Slowdive", "Alison", at=1730000000)],   # loves/stars as data (dislike: score=-1)
              plays=[Play("Ride", "Vapour Trail")],                # history as data
              yandex_playlist=None, lb_user=None,                  # or sources Post-Radio fetches itself
              lb_token=user_token)                                 # this user's LB token, for this call only
pl = rec.flow(explore=0.5, n=30, listener=me)                      # also generate(..., listener=), profile()
```

A `Listener` replaces the configured taste and history for that call only; concurrent calls never see each
other's. Its token is used for that call's ListenBrainz requests and is never stored, cached or logged.
Over HTTP, `POST /radio` and `POST /flow` take the same fields under `"listener"`, and `POST /profile`
takes a listener as its body.

**Progress and logging.**
- `progress=` on `generate` / `flow` / `profile` is called with a `Progress(stage, done, total, detail)` at
  each stage. It runs on worker threads, so keep it quick; a failing handler is logged and never breaks the
  run.
- Radio stages: `seed`, `graph` (hop/hops), `catalogues` (artists done/total), `radio`, `scoring`, `done`.
- Flow stages: `likes`, `profile` (artists done/total), `path`, `pools` (stop i/n), `catalogues`, `fill`,
  `done`.
- Prose messages go to the standard `logging` logger `postradio` (INFO), unless `log=` is given. The CLI
  prints them to stderr.

**Result types** (`postradio/results.py`); each has `to_dict()`, which is also what the HTTP API returns:
- **`Playlist`**, from `generate` and `flow`:
  - common fields: `kind` (`radio` / `flow`), `tracks`, `explore`, `popularity`, `rng_seed`, `stats`,
    `result_id` (only when kept), `created_at`, `default_name`, `schema`;
  - radio adds `seed` (a `Seed`);
  - flow adds `genre_path`, `profile`, `familiarity` and `flow_score`.
- **`PlaylistTrack`**, one shape for both:
  - common fields: `artist`, `title`, `artist_mbid`, `recording_mbid`, `deezer_id`, `popularity`, `score`,
    `sources`, `familiar`;
  - radio adds `hop`, `path` and `taste`; flow adds `stop` and `genres`.
  - Fields that don't apply to a mode are `None` or empty.
- **Previews and profile:** `GraphPreview` (radio `dry=True`), `FlowPreview` (flow `dry=True`), `Profile`,
  and `DeepCuts` (weeks of `PlaylistTrack`s).
- **Stability:** `Playlist.from_dict()` reads the JSON back and ignores unknown keys. New fields may be
  added; `schema` changes only if an existing field changes meaning.
- **MBIDs:** flow and radio tracks from Deezer usually have no `recording_mbid`, but `artist_mbid` is set
  whenever the artist was resolved.

`Recommender(cfg, home=, cache_dir=, data_dir=, state_dir=, token=, lb_user=, history=, feedback=, offline=,
env=, log=)`. Pass `history=` or `feedback=`
to plug in any `HistoryProvider` or `FeedbackProvider`, such as a Maloja or multi-scrobbler adapter.
`flow(explore=, popularity=, n=, per_artist=, stops=, rng_seed=)` and `profile()` run flow mode from the
configured likes.
`data_build(out_dir, skip, keep_dump, seeds, similar)`, `data_fetch(tag, store)` and `data_check(tag)` (the dry fetch) and
`data_status()` manage the optional files in `storage.data_dir`. `genre_graph()` returns the loaded `GenreGraph`.
`with_config(...)` returns a modified copy; use it in a `with` block so its worker threads are released.

## config.yaml: user settings

Only the keys you change are needed. The config is validated on load: unknown keys, wrong types
(e.g. `n: "ten"`), out-of-range values (`explore: 2`, `n: 0`) and unknown `mix` streams are errors
that name the key.

| Section | Keys (default) |
|---|---|
| `defaults` | `explore` 0.5 · `popularity` null · `n` 50 · `per_artist` 2 · `unheard` null (`tracks` \| `artists`) |
| `storage` | `save_dir` null (= `<state>/playlists`) · `feedback_file` null (= `<save_dir>/feedback/opinions.jsonl`) · `result_ttl_s` 3600 · `result_cache_size` 50 · `http_cache` null (= `<cache>/http.sqlite`) · `http_ttl_s` 604800 · `data_dir` null (= `<home>/data`: optional `mb_artists.sqlite`, `genre_weights.json`, `genre_artists.json`; everything works without them). Relative paths resolve against the config file's folder. |
| `sources` | `use_lb_token` true · `history` lb (`lb` \| `file` \| `yandex` \| `none`) · `lb_user` null · `history_file` null · `yandex_playlist` null (a public Yandex Music playlist link) · `yandex_tracks` null (newest N only; null = all) |
| `feedback` | `enabled` true · `sources` [json, yandex, navidrome, lb] (priority order; the first with an opinion on an item wins) · `navidrome_url` null (`$NAVIDROME_URL`) · `lb_user` null |
| `server` | `host` 127.0.0.1 · `port` 8765 · `job_workers` 2 (background jobs running at once) |

## config.yaml: `advanced` (algorithm constants)

Each key is described in [advanced.md](advanced.md).

| Group | Keys (default) |
|---|---|
| Explore → graph shape | `hop_span` 3.0 · `max_extra_hops` 2 · `radio_mode_thresholds` [0.34, 0.67] |
| Explore / popularity → targets | `pop_slope` 0.85 · `ceiling_base` 0.95 · `ceiling_slope` 0.6 · `slider_ceiling_base` 0.35 · `slider_ceiling_slope` 0.6 · `seed_pop_margin` 0.2 |
| Fit curve widths | `hop_width` 1.2 · `pop_width` 0.12 · `artist_pop_width` 0.03 · `unknown_artist_pop_fit` 0.7 |
| Graph | `source_weights` (lb 1.0, deezer 0.85, mb 0.6, tag 0.5, radio 0.5, genre 0.9, yandex 0.85) · `agreement_bonus` 1.15 · `graph_cap` 160 · `fanout` {1: 14, 2: 8, 3: 6} · `frontier_base` 16 · `frontier_min` 4 · `sparse_graph` 8 · `tag_neighbours` 20 · `tag_radio_recordings` 60 · `genre_seed_artists` 30 · `genre_seed_tracks` 100 · `genre_recordings_per_artist` 3 |
| Stream caps | `mix`: deezer-top, lb, deezer-radio, troi, track-sim, lb-tags, each 1.0 (see [architecture.md](architecture.md#stream-caps)) |
| Candidates | `shortlist_min` 40 · `candidate_artists_min` 18 · `radio_neighbours` 3 · `top_tracks_limit` 100 · `junk_title_pattern` (live, demo, remix, …) |
| Ranking | `familiarity_strength` 0.9 · `familiarity_saturation` 200 · `heard_skip_explore` 0.3 · `heard_penalty` 0.6 · `multi_source_bonus` 0.2 · `jitter` 0.3 · `shuffle_band` 10 · `max_source_share` 0.5 |
| Taste | `love_factor` 1.5 · `hate_factor` 0.3 · `feedback_track_cap` 50 · `similar_recordings_limit` 50 · `seed_track_similar_limit` 50 · `seed_track_boost` 1.0 · `seed_track_affinity` 0.8 · `feedback_max_age_s` 900 · `history_refresh_s` 86400 · `feedback_store_depth` 5000 |
| Provider scales | `deezer_fans_full_scale` 6.9 · `deezer_rank_full_scale` 900000 · `lb_listeners_full_scale` 5.5 · `lb_min_listener_share` 0.002 · `troi_window_width` 34 |
| Genre graph | `genre_kind_weights` (subgenre_of 1.0, influenced_by 0.5, yandex_parent 0.3) · `genre_fallback_scale` 0.2 · `genre_spread_hops` 2 · `genre_spread_decay` 0.5 |
| Flow mode | `flow_half_life_days` 730 · `flow_genre_rarity` 1 · `flow_parent_discount` 0.3 · `flow_profile_artists` 150 · `flow_profile_api` false (store only when installed) · `flow_source_shares` (mb 0.7, yandex 0.3) · `flow_destination_from` 0.3 · `flow_destination_width` 0.2 · `flow_destination_reach` 0.6 · `flow_new_artists` 30 · `flow_familiar_artists` 12 · `flow_tracks_per_artist` 5 · `flow_artist_gap` 4 |
| Rate limits & concurrency | `min_interval_s` (MusicBrainz 1.1, Wikidata 1.0, Deezer 0.12, LB 0.34, LB labs 0.1, other 0.05) · `max_in_flight` (MusicBrainz 1, Wikidata 1, Deezer 6, LB 4, LB labs 4, other 4) · `ratelimit_floor` 2 · `max_attempts` (MusicBrainz 5, other 4) · `backoff_cap_s` 30 · `fetch_workers` 8 · `http_cache_keep_s` 2592000 (30 days) |

## Loves/hates import format

```json
{
  "loves": {"artists": ["Slowdive", {"name": "Pixies", "mbid": "…"}],
            "albums":  [{"artist": "Blur", "title": "13"}],
            "tracks":  [{"artist": "Cocteau Twins", "title": "Heaven or Las Vegas", "recording_mbid": "…"}]},
  "hates": {"albums": [{"artist": "My Bloody Valentine", "title": "m b v"}]}
}
```

Each import is appended as one dated batch to the JSONL log, and nothing is ever rewritten. The newest
opinion per item wins. An album opinion counts as a half-strength opinion about its artist.
