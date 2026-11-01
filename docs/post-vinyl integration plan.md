# Post-Vinyl integration plan

How Post-Vinyl (`rebuild/base`) uses Post-Radio's radios and flow. Written 2026-11-01 from this session's
decisions and Post-Vinyl's own design (`agents_memory/rebuild/architecture.md` §7, milestone 9 "approval
inbox + Postradio"). This is a plan, not built. Post-Vinyl's AGENTS.md applies when it starts: interview
first, issues proposed when the milestone starts, a `rebuild/<topic>` branch off `rebuild/base`.

## What Post-Vinyl uses

| feature | Post-Radio call | user choices |
|---|---|---|
| Radio | `rec.generate(...)` | one seed kind: artists (1–5), tracks (1–5) or genres (`or`/`and`); taste on/off; explore; popularity |
| Flow | `rec.flow(...)` | explore; popularity; start genre (optional); stops; n |
| Previews | the same calls with `dry=True` | radio: artist graph by hop; flow: profile and genre path, ~1 s |
| Taste profile | `rec.profile(listener)` | none; shown so the user sees what flow starts from |
| Data install | `rec.data_check()`, `rec.data_fetch()` | admin only |

Not used: `deep_cuts`, `feedback import`, `save`/`results` (Post-Vinyl keeps its own records), the HTTP
server, Yandex inside Post-Radio (Post-Vinyl has its own Yandex module, §7).

Surrounding library parts it relies on: `Listener` (per-call taste and token), typed results
(`Playlist`, `PlaylistTrack`, previews, `Profile`), `Progress`, `cancel` / `timeout_s`, the error classes,
`stats["input"]`, `rng_seed`.

## Shape

```
user (radio / flow form) ─→ routes/recs ─→ services/recs ─→ clients/postradio ─→ postradio.Recommender
                                               │
                                               ├─ Navidrome stars → Listener.likes
                                               ├─ decrypted LB token → Listener.lb_token (this call only)
                                               ├─ each track → catalog.resolve() → song_id
                                               └─ missing songs → proposals (pending) → user approves → requests.create()
```

- **In-process library**, not the HTTP server: no extra container, and the token never crosses a
  network. Only `clients/postradio.py` knows Post-Radio, so a switch to the server later touches that one file.
- §7's rule holds: the recs module calls only `catalog`, `proposals` and its own client.

## Pieces

### 1. Dependency and folders
- `requirements.txt`: `postradio @ git+https://github.com/Goober-nation/Post-Radio@<commit>`, a commit,
  never a branch. Post-Radio needs Python ≥ 3.11; Post-Vinyl's image is 3.12.
- One `Recommender(home="/data/postradio")` built in `main.py` at start-up, closed on shutdown. It reads
  no environment variables and no `.env`, and writes only under that folder (`cache/`, `data/`,
  `state/`). Settings come from Post-Vinyl's YAML (piece 7), passed as a Post-Radio `Config`.
- mypy: Post-Radio ships no `py.typed` yet. **Post-Radio change:** add it, so Post-Vinyl type-checks
  against the real signatures instead of ignoring the import.

### 2. `clients/postradio.py` (ABC + real + fake)
- Methods: `radio(seeds, taste, explore, popularity, n, listener, cancel, timeout_s)`,
  `flow(start, explore, popularity, stops, n, listener, cancel, timeout_s)`, both with `dry`;
  `profile(listener)`; `data_status()`, `data_check()`, `data_fetch()`.
- Returns Post-Radio's typed results as they are (no wrapper types): `Playlist`, `GraphPreview`,
  `FlowPreview`, `Profile`.
- Maps `postradio.errors` to Post-Vinyl's soft errors: `InvalidInput` / `NotFound` → shown to the user as is
  (the "did you mean" text included); `SourceUnavailable` → "recommendations unavailable, try later";
  `Cancelled` → silent; anything else → logged.
- The fake builds results with `Playlist.from_dict(...)`; `tests/test_services/test_fakes_match.py` runs
  the same scenarios on fake and real (real against Post-Radio's own fakes, no network).

### 3. Taste from Navidrome
- Post-Vinyl's Navidrome client gets `starred(session)` (Subsonic `getStarred2`): starred songs with their
  dates → Post-Radio `Like(artist, title, at=...)`. Artist/album stars → `Like(artist)`.
- Ratings 1–2 → `Like(..., score=-1)` (dislikes). Decide in the interview whether ratings count.
- Plays: Navidrome keeps only play count and last played per song. Optional `Play` rows from that; full
  history needs ListenBrainz scrobbles, which the token path already covers.
- Fetched per generation, not stored (Navidrome owns stars; "never duplicate state another system owns").

### 4. ListenBrainz token
- Decrypt the user's `lb_token_enc` right before the call and pass it as `Listener(lb_token=...)`.
  Post-Radio uses it for that call only and never stores or logs it. Users without a token still get results,
  just slower (cold flow: minutes instead of ~20 s).
- Post-Radio's own `use_lb_token` stays on; no `LISTENBRAINZ_TOKEN` is migrated from `.env`.

### 5. `services/recs`
- `generate_radio(user, form)` and `generate_flow(user, form)`: build the listener (piece 3 + 4), call the
  client, then for each track:
  - **Resolve** with `catalog.resolve(user, source, id)`, preferring ids Post-Radio already gives:
    `recording_mbid` → `musicbrainz`, else `deezer_id` → `deezer`, else a name search (ListenBrainz is
    the name → MBID fallback for request sources, as decided). Unresolvable tracks are counted and shown.
  - **Already in the library** (song has an `indexed` file): part of the playable result straight away.
  - **Missing**: an `add` proposal (§7 inbox). A song the user rejected before is not proposed again.
- Keeps a `rec_runs` record per generation: kind, form values, `rng_seed`, Post-Radio version and
  `schema`, `stats["input"]`, counts (resolved / in library / proposed). Lets a user rerun the same playlist
  and explains weak results ("12 of 80 liked artists recognised").
- Runs in a background job: the route returns at once; the frontend polls status with `Progress` stage and
  counts. The job passes `cancel` (user closes it, replaces it, shutdown) and `timeout_s` (config).
- One running generation per user; more wait. Post-Radio doesn't queue; Post-Vinyl does.

### 6. Playable result
- Open question for the interview: a generated playlist becomes a **Navidrome playlist** (via §8's
  `services/playlists`, the only writer to Navidrome playlists), which grows as approved downloads arrive?
  Or a one-off list in Post-Vinyl's UI only? §8 suggests the former, as a `playlists` row of a new kind
  (`generated`) holding the rec run's song order.

### 7. Config (`post-vinyl.example.yaml`)
- `request:` already lists `postradio`; it gates the feature per user like every source (users can turn
  it off, never add it).
- New `postradio:` section, documented like the rest (settings schema): defaults for explore,
  popularity, n, stops, per-artist cap, taste on; `timeout_minutes`; `jobs_at_once`; `fetch_data_on_start`.
- Post-Radio's Advanced keys stay Post-Radio's (`docs/advanced.md`); Post-Vinyl doesn't expose them.

### 8. Data release
- Admin action (or `fetch_data_on_start`): `data_check()` then `data_fetch()` into
  `/data/postradio/data` (~600 MB, ~250 MB download). The admin page shows `data_status()` (release tag,
  MusicBrainz dump date). Everything works without it, slower and with worse artist matching.
- Licence: the store and genre files are CC BY-NC-SA (MusicBrainz data). Post-Vinyl downloads them at
  run time and never ships them, so its own licence is unaffected; the admin page shows the attribution.

### 9. Routes and frontend
- Radio form: seed kind picker (artists / tracks / genres), up to 5 seeds with search (artists and tracks
  via Post-Vinyl's catalog search, so seeds arrive as MBIDs: `artist_mbid` / `recording_mbid` lists), genre
  input with `or`/`and`, taste switch, explore and popularity sliders, "preview" (dry) and "generate".
- Flow form: explore, popularity, optional start genre, stops, n, preview first.
- Genre autocomplete reads the CC0 `genre_graph.json` that comes with the Post-Radio package
  (`postradio/data/genre_graph.json`, or `<data>/genre_graph.json` when a release is installed).
- Result view: tracks with stop / hop, `via`, familiar mark, in library / proposed / unresolved; a profile
  panel; the input counts.

### 10. Tests and proof
- Unit: services/recs against the fake client and fake catalog (resolve order, proposals not repeated,
  one job per user, cancel); client mapping of errors and listener building.
- Fakes match: the same scenarios on the fake and the real client with Post-Radio's offline fakes.
- Live (Post-Vinyl's rule: integration changes need a real-service run): one radio per seed kind, one flow,
  with and without token, on the test stack; recorded in `live-runs-log.md`. Until then, "unverified on
  real services".

## Order

1. Post-Radio: `py.typed` (done); pin a commit after this branch merges.
2. Post-Vinyl client + fake + fakes-match test (pieces 1, 2).
3. Navidrome stars (3), token (4).
4. Proposals store/service (§7 inbox) if not built yet: recs depends on it.
5. services/recs + job + `rec_runs` (5); config (7); data admin action (8).
6. Routes; frontend when milestone 8's frontend exists (9).
7. Playable result (6), with §8's playlists.
8. Live run, then close the milestone's issues.

## Open questions for the Post-Vinyl interview

- Generated playlists: Navidrome playlist (§8) or Post-Vinyl list only? Kept how long?
- Do Navidrome ratings 1–2 count as dislikes? Do album/artist stars count as likes?
- Auto-propose every missing song, or only when the user asks ("get the missing ones")?
- Scheduled recs (§7's `rec_puller`): still wanted next to on-demand radios and flows, e.g. a weekly flow?
- Who may fetch the data release: admin only, or automatically on first start?
