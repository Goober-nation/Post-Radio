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

## Ideas, not scheduled

- **Per-stream playlist-share cap.** `advanced.mix` caps a stream's *pool* by keeping the candidates
  nearest the popularity target. Lowering `deezer-top` from 0.06 to 0.04 left a 10-track playlist
  unchanged, because the dropped candidates would never have been picked anyway. A cap like "at most X%
  of the playlist from stream S" would act at selection time. The engine already has a global version of
  this, `max_source_share` (0.5); this would be a per-stream version of it.
