# Second cloud session notes

Branch `flow-integration` in Post-Radio. Work plan: `docs/post-radio integration plan.md` on
Post-Radio-Webpage's `genre-graph` branch. One phase at a time, each reviewed before the next.

## Decisions (user, 2026-11-01)

- Post-Radio licence: MIT. Data derived from MusicBrainz stays CC BY-NC-SA 3.0.
- The genre explorer page moves into Post-Radio.
- `genre_artists.json` also ships as a data-release asset, for users without the store.

## Phase 1: genre layer + MusicBrainz store (done, commit de7b174)

- `postradio/genres.py`: the graph, learned-weight combine, `spread()`. The packaged
  `postradio/data/genre_graph.json` is CC0.
- `providers/musicbrainz_store.py` and the `MusicBrainz` adapter: store first, API fallback, plus a
  `GenreSource`.
- `providers/wikidata.py`: the genre graph from SPARQL.
- `storage.data_dir`.

Checked on real services:
- **Wikidata** rebuilt the graph exactly: 2,178 genres and 2,701 links.
  - The old single labelled query now hits Wikidata's 60 s limit and comes back cut off mid-JSON. It is
    split into three queries now, and `Http` retries bodies it can't parse.
  - The old build listed roots more than once for 125 genres; the new build lists each root once.
- **Store built from dump `20261031-001001`:** 3,028,753 artists, 218,399 with genres, 584 MB, 458 s.
  - Kanye West → Ye, SABA, ЛСП, Macklemore, Twista and Sainté all resolve locally, with 0 API calls.
  - Without the store, the same names take 1–8 s each over the API.
- The user confirmed the store works locally.

## Phase 2: data commands (done)

- `postradio data build|fetch|status` (see docs/architecture.md).
- The live weights build matches the old one: 992 seeds, 9,891 pairs, co-tag 1,148 edges over 279 genres,
  similar 10,823 over 457. The old build had 1,153/291 and 10,909/449.
- Release `data-2026-11` was created by the user. Fetch was verified against it.
- The first upload had no store: `data_dir` pointed at a folder without one, and nothing warned.

## Phase 3: Yandex adapter

- Real playlist (952 tracks) loads on the user's machine: all pages arrive, history works (`*` on known
  artists, liked songs dropped). Taste was off with an older config's source list. Fixed: `--yandex` now
  adds the `yandex` source itself.
- `providers/yandex.py`: `YandexLikes` (taste, `as_history()`, tags), `YandexSimilar` (soundtrack
  catalogues dropped). Config `sources.yandex_playlist` / `yandex_tracks` / `history: yandex`;
  CLI `radio --yandex URL`.
- The cloud session gets 451 on playlist endpoints (`/genres` answers 200), so it is tested with fakes only.

## Phase 4: flow mode (done; tested here with imported loves, not yet with the Yandex playlist)

- `postradio/flow.py` (pure planner, ported), `Radio.flow` / `Radio.profile` in the engine,
  `Recommender.flow` / `profile`, CLI `flow` / `profile`, `POST /flow`, `GET /profile`.
  `FeedbackProvider.liked()` gives dated likes (Yandex: when each track was added).
- Live run in the cloud: 16 loved tracks over 15 artists, with the store, real Deezer/LB/MusicBrainz, `-p 0.5`,
  12 tracks, 4 stops, `--rng-seed 3`:
  - explore 0: hip hop → contemporary r&b → experimental hip hop → electronic. All tracks are by familiar
    artists, and no liked songs come back.
  - explore 0.5: hip hop → afro house → gangsta rap → hardcore hip hop. Mostly new artists.
  - explore 1: hip hop → dancehall → electropop → dance-pop. All new.
  - Runs take 40–110 s cold, depending on how many new artists need a Deezer lookup.
- Deviations from the webpage planner:
  1. Destination closeness is now relative to the closest eligible genre. With a broad profile every
     outside genre scored ≤ 0.41 against the top genre, so explore 0.5 and 1 picked the same destination.
  2. The artist-gap swap stays within a stop, because it used to pull tracks across stops and break the path.
  3. A stop that comes up short hands its remainder to the other stops.
  4. Liked tracks are dropped whatever the source (the webpage relied on Yandex history).
  5. The junk filter (also used by radio) gained: tribute, originally performed, in the style of,
     re-recording, from the series/film/movie/game.
- The user's first real `profile` (952-track Yandex playlist, store installed) took 1–2 min: "mb genres for
  130 of 150". Each artist the store didn't know cost ~4.5 API calls (~10 s), and each it knew without
  genres ~1.4 calls (~1.7 s). The profile now uses a store-only MusicBrainz view (`MusicBrainz.local()`)
  whenever the store is installed, and Yandex genres cover the gaps. `advanced.flow_profile_api: true`
  brings the API fallback back. The log line reports resolved artists and network calls. Measured: 22
  artists (2 of them made-up names) took 1 s with 0 calls.
- Not ported (Post-Radio already covers these): the webpage's own Deezer client and cache; the Gumbel
  temperature and position weight, replaced by `pop_fit`, jitter and `--rng-seed`; `title_key` remains
  only for song de-duplication in `fill`.
- Open: popularity "match me" (default = the user's own median), regionality, the three taste time
  scales, tempo ordering, and moving the explorer page here.

## Phase 4.5: library hardening + integration testing (decided 2026-11-01, before phase 5)

Why: radio and flow had been exercised from the CLI only. Library callers (Post-Vinyl, the webpage, other
projects) and the HTTP API were barely tested together. The trigger was a config-only setup that broke
flow ("flow needs likes") because a rule lived in the CLI.

Decisions (user):
1. **Order:** this phase comes before the webpage switch-over.
2. **Results:** typed objects with `to_dict()`, one shape for radio and flow (e.g. `Playlist`,
   `PlaylistTrack`: artist, title, MBIDs, Deezer id, stop/hop, scores), and a stable JSON form for HTTP.
3. **Multi-user:** one long-lived `Recommender`, with taste and history passed per call
   (generate/flow/profile). Users can also bring **their own ListenBrainz token** per call: it is their
   LB catalogue and makes identity checks fast.
4. **Long runs:** structured **progress events** (stage, done/total), with the standard `logging` module
   for prose, and **HTTP jobs** (POST returns a job id; GET /jobs/{id} gives status, progress and the
   result).
   - Not chosen for now: a cancel token or deadline per call, and recorded-response replay fixtures.

Planned work (to confirm order):
- **Paths:** stop resolving `config.yaml`, `.env` and the relative HTTP cache path against the repo root.
  In a pip install that root is site-packages. Use the user's config and cache directories, and read
  nothing implicitly as a library.
- **One error base `PostRadioError`** with subtypes for bad input, not found, a source unavailable and
  data missing. Today these are mixed `ValueError` and `FileNotFoundError` subclasses.
- **No storage by default:** results are only cached or saved when asked.
- **Rules move from the CLI into `Recommender`,** so library, CLI and HTTP behave the same.
- **Flow tracks need recording MBIDs** where possible (Deezer tracks usually have none). Post-Vinyl needs
  them for search and tagging.
- **Tests across Python API, CLI and HTTP** (`/radio`, `/flow`, `/profile`, `dry`, jobs, error codes) on
  the same fake providers, plus multi-user concurrency: two users' taste in parallel on one
  `Recommender`.
- A **library usage doc** for Post-Vinyl.

Storage layout (discussed): one `home` folder per deployment.
- Precedence: `Recommender(home=...)`, then `$POSTRADIO_HOME`, then user/system directories.
- Layout: `<home>/config.yaml`, `cache/`, `data/`, `saved/`.
- Relative config paths resolve against the config file's own folder.
- As a library, nothing is read implicitly from the working directory or the repo.
- Post-Vinyl mounts e.g. `./postradio:/data/postradio` in its compose project. All users share the store
  and cache; per-user taste comes through the call.

How Post-Vinyl (`rebuild/base`) handles ListenBrainz tokens, and what follows for Post-Radio:
- **Per user, entered in the app.** Checked with LB `validate-token` before storing (invalid 422, LB down
  503). Stored Fernet-encrypted (`users.lb_token_enc`, key `PV_SECRET_KEY`) and write-only. Decrypted just
  in time and passed per call (`token=` on its LB client). No `.env` migration: `LISTENBRAINZ_TOKEN` is
  being removed from its env. Tagging lookups use the admin's token today.
- **Post-Radio's planned role** there (architecture.md §7): a `clients/postradio.py` (ABC + fake);
  `services/recs` reads taste from Navidrome, asks Post-Radio, then `catalog.resolve()` → proposals
  (worker `rec_puller`).
- Consequences for Post-Radio:
  1. The token is a per-call argument; the `$LISTENBRAINZ_TOKEN` fallback is CLI-only.
  2. The token is never stored, logged or returned (add a test). The shared cache is fine: the
     token-only endpoint returns public data.
  3. Per-call taste accepts **data** (dated likes and plays with MBIDs), so Post-Vinyl needn't hand over
     Navidrome credentials. Provider objects stay available for the CLI and simple callers.
  4. Errors tell an auth failure apart from a source being unavailable, so Post-Vinyl can map them to
     422 and 503.
  5. Results carry IDs `catalog.resolve()` can use (recording MBIDs where possible) and are easy to fake.

### Step 1 done: folders, environment, errors, no storage by default

- `Recommender(home=, cache_dir=, data_dir=, state_dir=, env=)`; `config.resolve_dirs`, `find_config`
  (CLI only); `config.load(None)` = defaults, nothing implicit.
- The library reads no environment, no `.env` and no `./config.yaml`. The CLI passes `os.environ`.
- `errors.PostRadioError` hierarchy; one HTTP mapping in `server.http_error`.
- `keep=False` by default: nothing is cached or saved unless asked.
- Tests (`tests/test_library.py`): an unrelated working directory with a polluted env and a stray
  config → nothing read, nothing written outside `home`; a token is sent but never in the cache file or
  logs.
- **User action:** the repo's `./config.yaml` is still found by the CLI when run from the repo root.
  Relative paths in it now resolve against its own folder (the repo), as before.

### Step 2 done: typed results

- `postradio/results.py`: `Playlist`, `PlaylistTrack` (one shape for radio and flow), `Seed`, `DeepCuts`,
  `Week`, `GraphPreview`, `FlowPreview`, `Profile`. Each has `to_dict()`; `Playlist.from_dict()` ignores
  unknown keys; `schema` = 1.
- Tracks now carry `artist_mbid`, which was missing from both engines' rows before.
- The CLI and server convert at the edge (`to_dict()`). Saved files are `Playlist.to_dict()` plus
  `saved_as`.
- Tests: one shape for radio and flow, a JSON round trip, the dry radio preview, and HTTP sending the same
  keys as `to_dict()`.
- Not done yet: recording MBIDs for Deezer tracks. That needs a per-track lookup (LB name lookup with a token,
  or MusicBrainz at 1/s), to decide with step 3.

### Step 3 done: per-call listeners

- `api.Listener(likes, plays, yandex_playlist, yandex_tracks, lb_user, lb_token, feedback, history)` on
  `generate` / `flow` / `profile`. It replaces the configured taste and history for that call only.
- Taste as data: `models.Like` (artist, title, at, score, MBIDs); history as data: `models.Play`.
  `providers/listener.py` turns them into providers and merges several history sources.
- `Radio.view(providers)`: a per-call engine with its own taste and history snapshot, sharing the thread
  pool.
- HTTP: `listener` on `POST /radio` and `POST /flow`, and a new `POST /profile`.
- Tests:
  - two listeners × 4 calls on one `Recommender` in parallel each got their own profile;
  - the configured taste stays untouched;
  - each call's token reached its sources and is never echoed;
  - liked and played tracks are dropped, and played artists count as known.

### Step 4 done: progress events, logging, HTTP jobs

- `results.Progress` events via `progress=` (per call) at each stage. `Radio._map(stage=)` reports done/total
  as fetches finish. A broken handler is logged, never fatal.
- Prose goes to the `postradio` logger by default (the library was silent or printing before).
- HTTP jobs: `POST /jobs/{radio,flow,profile}` → 202 + `job_id`; `GET /jobs/{id}` gives status, progress,
  and the result or a typed error with its HTTP status. `server.job_workers` (2); finished jobs expire
  after `result_ttl_s`.
- Tests: stage order and done/total, a broken handler, jobs to done and to error, 422 before queueing, 404
  for an unknown job.
- Fixed two pre-existing flaky timing tests in `test_http_scheduler.py` (~1 failure in 15 full runs): they
  checked every gap between thread wake-ups. They now check the total span, which is what proves the
  shared schedule. 40 full runs then passed.

## Dry runs and seeds (user request, 2026-11-01)

- `radio --dry`: the seed and the artist graph by hop, no tracks. `flow --dry`: the profile and the genre
  path, no tracks. `data fetch --dry` (`Recommender.data_check`): the release listing and its manifest only.
- Every radio and flow run reports its seed, picking one when `--rng-seed` isn't given, so a preview can
  be repeated for real.

## Flow speed (measured)

A cold `flow -e 0.5 -n 30 --stops 5` (16 loved tracks, store installed, no LB token) took 185 s. Profile
and path took 0.1 s. Requests: 50 Deezer artist searches, 48 Deezer catalogues, and 84 MusicBrainz
requests (40 recording + 44 release-group lists). The MusicBrainz ones dominate: Deezer's identity check
fetches an artist's MB titles whenever a name search returns several same-named Deezer artists, and
MusicBrainz allows 1 request/s. Repeat runs on the same artists are fast (7-day cache).

- **With a ListenBrainz token** (`$LISTENBRAINZ_TOKEN`, or in `.env` in the repo root), the same cold run took
  22 s: 6 MusicBrainz requests instead of 84. LB's per-artist popularity catalogue (48 fast, parallel
  requests) supplies the titles that settle almost every same-name case. The token also adds LB as a track
  source and as the authoritative artist popularity. **Decision (user): use the token.**
- **On hold:** skipping the MusicBrainz identity check in flow, i.e. taking the most-followed exact-name Deezer
  match. A cold run without a token would drop to about 20–40 s, with a small risk of a namesake's catalogue
  at a stop. A capped check (only the first ~10 ambiguous artists) was the middle option.

## Development notes

- Setup, always in a venv:
  `python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`
- Checks: `.venv/bin/pytest -q` and `.venv/bin/ruff check postradio tests scripts`. The repo doesn't use
  `ruff format`.
- After changing `postradio/config.py`, run `.venv/bin/python scripts/gen_config_example.py` (a test
  compares the two).
- Commits use the user's own git identity, with no AI attribution (AGENTS.md). The first four commits were
  rewritten on 2026-11-01 to fix that, so clones from before then need
  `git fetch origin && git reset --hard origin/flow-integration`.
- `.gitignore` anchors `/data/` and `/dist/` to the repo root. An unanchored `data/` also matched
  `postradio/data/` and hid `yandex_genres.json`. Tests passed anyway, because the file existed locally.
  After adding a packaged file, check `git ls-files postradio/data`.
- Cloud sessions: Yandex answers 451 on playlist endpoints (`/genres` works), so Yandex is tested with
  fakes there. MusicBrainz, ListenBrainz, Wikidata and the MusicBrainz dump download all work.
- Wikidata SPARQL must stay under its 60 s limit, or the answer comes back cut off mid-JSON with HTTP 200.

## User configuration (as used in testing)

`config.yaml` in the repo root (or `--config` / `$POSTRADIO_CONFIG`). Only changed keys are needed;
unknown keys are an error. A `config.yaml` generated before a release lacks the new keys, which then take
their defaults. The exception is an explicit list such as `feedback.sources`: it doesn't gain new sources
by itself.

```yaml
storage:
  data_dir: /path/to/Post-Radio/data   # the repo's data/ (ignored by git), holds the store and genre files
sources:
  history: yandex                      # lb | file | yandex | none
  yandex_playlist: https://music.yandex.ru/playlists/<your likes playlist id>
  yandex_tracks: null                  # newest N only; null = all
feedback:
  sources: [json, yandex, navidrome, lb]   # priority order, first opinion wins
advanced:
  mix: {deezer-top: 0.04}              # caps the pool, not the playlist share (see Ideas)
```

`storage.data_dir` contains:
- `mb_artists.sqlite` (584 MB), `genre_artists.json`, `genre_graph.json` and `genre_weights.json`, from
  release `data-2026-11`;
- leftovers from the webpage (`deezer_cache.json`, an old `manifest.json`), which Post-Radio ignores.

## How to use

```bash
# radio from an artist, with the Yandex likes playlist as history + taste
.venv/bin/postradio radio --artist "Kanye West" -n 20 --yandex "<playlist link>"
.venv/bin/postradio radio --genre shoegaze -n 20          # genre seed (store adds MusicBrainz artists)
.venv/bin/postradio radio --artist X --mix "deezer-top=0"  # switch a stream off for one run

# optional data (storage.data_dir)
.venv/bin/postradio data status                 # what is installed
.venv/bin/postradio data fetch [--no-store]     # newest data-YYYY-MM release, verified by checksum
.venv/bin/postradio data build --out dist       # full rebuild, ~30 min, ~3 GB; --skip graph,dump,weights
gh release upload data-YYYY-MM --repo Goober-nation/Post-Radio --clobber dist/data-YYYY-MM/*
```

What to look for in a radio run's log:
- `# yandex: N of M tracks` — the playlist loaded.
- `# history: …` — liked artists count as known (marked `*`), and liked songs are dropped.
- `# feedback: N opinions from json, yandex` — your likes act as taste.

Release checklist:
1. `data status` should show the store and index. Otherwise the build packages without them.
2. Run `data build`.
3. Upload the whole `dist/data-YYYY-MM/` folder.
4. `data fetch` should then list `mb_artists.sqlite.xz` and `genre_artists.json`.

## Ideas, not scheduled

- **Release guards.** `data build` should refuse to package without the store and index (unless
  `--genres-only`), and `data fetch` should say when a release has no store. Proposed after the half-empty
  first release; not done yet.
- **Release folder location.** It could default to `<data_dir>/releases/` instead of `dist/`.

- **Per-stream playlist-share cap.** `advanced.mix` caps a stream's *pool* by keeping the candidates
  nearest the popularity target. Lowering `deezer-top` from 0.06 to 0.04 left a 10-track playlist
  unchanged, because the dropped candidates would never have been picked anyway. A cap like "at most X%
  of the playlist from stream S" would act at selection time. The engine already has a global version of
  this, `max_source_share` (0.5); this would be a per-stream version of it.
