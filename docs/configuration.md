# Configuration and options

## CLI: `postradio`

Global flags work before or after the subcommand: `--config PATH` (default `$POSTRADIO_CONFIG` or
`./config.yaml`), `--no-token`, `--offline` (cache only) and `--json`. User errors print one line instead
of a traceback.

| Command | Flags (default) |
|---|---|
| `radio` | **Seed, exactly one:** `--artist` · `--track "Artist - Title"` · `--artist-mbid` · `--recording-mbid` · `--genre "a,b"`<br>**Seed options:** `--genre-op or\|and` (or) · `--unheard none\|tracks\|artists` (config; `none` overrides it)<br>**Shape:** `-e/--explore` 0–1 (0.5) · `-p/--popularity` 0–1 (unset = follows explore) · `--auto-popularity` · `-n` (50) · `--per-artist` (2) · `--rng-seed`<br>**History, at most one:** `--lb-user U` · `--history-file F.jsonl\|csv` · `--no-history`<br>**Taste:** `--no-feedback` · `--navidrome-url URL`<br>**Streams:** `--mix "deezer-top=0.2,troi=1"`<br>**Save:** `--save [NAME]` |
| `deep-cuts` | `--user` (config / `$LISTENBRAINZ_USERNAME`) · `--kind exploration\|jams` (exploration) · `--save [NAME]` |
| `feedback import FILE\|-` | Append a dated loves/hates batch |
| `feedback batches` | List imported batches with dates |
| `saved [name]` | List saved playlists, or print one |
| `serve` | `--host` (127.0.0.1) · `--port` (8765) |

Environment variables: `LISTENBRAINZ_TOKEN`, `LISTENBRAINZ_USERNAME`, `POSTRADIO_CONFIG`, `NAVIDROME_URL`,
`NAVIDROME_USERNAME`, `NAVIDROME_PASSWORD`. A `.env` file in the repository root is loaded at startup and
doesn't override variables already set.

## HTTP API: `postradio serve` (FastAPI, OpenAPI at `/docs`)

| Endpoint | Does |
|---|---|
| `POST /radio` | Body: exactly one of `artist`, `track`, `artist_mbid`, `recording_mbid`, `genres`. Optional: `genre_op`, `unheard`, `explore`, `popularity`, `n` (1–500), `per_artist`, `rng_seed`, `auto_popularity`, `mix` (`{stream: 0..1}`). Returns **422** for invalid input (seed count, malformed MBID or track string, out-of-range values, unknown mix stream) and **404** when the seed doesn't exist. |
| `GET /deep-cuts?user=&kind=exploration\|jams` | LB Weekly Exploration or Jams, one entry per week |
| `GET /results/{id}` | An unsaved result from memory; 404 once expired |
| `POST /results/{id}/save` | `{name, overwrite}`; 409 if the file exists |
| `POST /feedback` | Import a loves/hates batch. Typed body `FeedbackImport`: `loves`/`hates` → `artists`, `albums`, `tracks`. |
| `GET /feedback/batches` | Imported batches with dates and sizes |
| `GET /saved`, `GET /saved/{name}` | List saved playlists, or get one |

## Python API

```python
from postradio.api import Recommender

with Recommender() as rec:                      # reads config.yaml / $POSTRADIO_CONFIG / defaults
    res = rec.generate(artist="Radiohead", explore=0.7, popularity=0.3, mix={"troi": 1.0})
    rec.save(res["result_id"])                  # writes JSON to storage.save_dir
    rec.deep_cuts()                             # {"weeks": [{"week": ..., "tracks": [...]}, ...]}
    rec.import_feedback({"loves": {"artists": ["Slowdive"]}})
```

`Recommender(cfg, token=, lb_user=, history=, feedback=, offline=, log=)`. Pass `history=` or `feedback=`
to plug in any `HistoryProvider` or `FeedbackProvider`, such as a Maloja or multi-scrobbler adapter.
`with_config(...)` returns a modified copy; use it in a `with` block so its worker threads are released.

## config.yaml: user settings

Only the keys you change are needed. The config is validated on load: unknown keys, wrong types
(e.g. `n: "ten"`), out-of-range values (`explore: 2`, `n: 0`) and unknown `mix` streams are errors
that name the key.

| Section | Keys (default) |
|---|---|
| `defaults` | `explore` 0.5 · `popularity` null · `n` 50 · `per_artist` 2 · `unheard` null (`tracks` \| `artists`) |
| `storage` | `save_dir` `~/.local/share/postradio/playlists` · `feedback_file` null (= `<save_dir>/feedback/opinions.jsonl`) · `result_ttl_s` 3600 · `result_cache_size` 50 · `http_cache` `.cache/http.sqlite` · `http_ttl_s` 604800 |
| `sources` | `use_lb_token` true · `history` lb (`lb` \| `file` \| `none`) · `lb_user` null · `history_file` null |
| `feedback` | `enabled` true · `sources` [json, navidrome, lb] (priority order; the first with an opinion on an item wins) · `navidrome_url` null (`$NAVIDROME_URL`) · `lb_user` null |
| `server` | `host` 127.0.0.1 · `port` 8765 |

## config.yaml: `advanced` (algorithm constants)

| Group | Keys (default) |
|---|---|
| Explore → graph shape | `hop_span` 3.0 · `max_extra_hops` 2 · `radio_mode_thresholds` [0.34, 0.67] |
| Explore / popularity → targets | `pop_slope` 0.85 · `ceiling_base` 0.95 · `ceiling_slope` 0.6 · `slider_ceiling_base` 0.35 · `slider_ceiling_slope` 0.6 · `seed_pop_margin` 0.2 |
| Fit curve widths | `hop_width` 1.2 · `pop_width` 0.12 · `artist_pop_width` 0.03 · `unknown_artist_pop_fit` 0.7 |
| Graph | `source_weights` (lb 1.0, deezer 0.85, mb 0.6, tag 0.5, radio 0.5, genre 0.9) · `agreement_bonus` 1.15 · `graph_cap` 160 · `fanout` {1: 14, 2: 8, 3: 6} · `frontier_base` 16 · `frontier_min` 4 · `sparse_graph` 8 · `tag_neighbours` 20 · `tag_radio_recordings` 60 · `genre_seed_artists` 30 · `genre_seed_tracks` 100 · `genre_recordings_per_artist` 3 |
| Stream caps | `mix`: deezer-top, lb, deezer-radio, troi, track-sim, lb-tags, each 1.0 (see [architecture.md](architecture.md#stream-caps)) |
| Candidates | `shortlist_min` 40 · `candidate_artists_min` 18 · `radio_neighbours` 3 · `top_tracks_limit` 100 · `junk_title_pattern` (live, demo, remix, …) |
| Ranking | `familiarity_strength` 0.9 · `familiarity_saturation` 200 · `heard_skip_explore` 0.3 · `heard_penalty` 0.6 · `multi_source_bonus` 0.2 · `jitter` 0.3 · `shuffle_band` 10 · `max_source_share` 0.5 |
| Taste | `love_factor` 1.5 · `hate_factor` 0.3 · `feedback_track_cap` 50 · `similar_recordings_limit` 50 · `seed_track_similar_limit` 50 · `seed_track_boost` 1.0 · `seed_track_affinity` 0.8 · `feedback_max_age_s` 900 · `history_refresh_s` 86400 · `feedback_store_depth` 5000 |
| Provider scales | `deezer_fans_full_scale` 6.9 · `deezer_rank_full_scale` 900000 · `lb_listeners_full_scale` 5.5 · `lb_min_listener_share` 0.002 · `troi_window_width` 34 |
| Rate limits & concurrency | `min_interval_s` (MusicBrainz 1.1, Deezer 0.12, LB 0.34, LB labs 0.1, other 0.05) · `max_in_flight` (MusicBrainz 1, Deezer 6, LB 4, LB labs 4, other 4) · `ratelimit_floor` 2 · `max_attempts` (MusicBrainz 5, other 4) · `backoff_cap_s` 30 · `fetch_workers` 8 · `http_cache_keep_s` 2592000 (30 days) |

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
