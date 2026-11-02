# Open items (from the first e2e run, 2026-11-01)

Kept up to date as items are settled. Details of each finding: `docs/second cloud session.md`, "E2E run 1".

- [x] Liked collaborations come back: fixed (Yandex likes/history credit every artist).
- [x] Unknown artist seeds: not Post-Radio's job; Post-Vinyl verifies every input first.
- [x] Genre Atlas page (`scripts/genre_atlas.py`): link-strength and show-by-weight ranges, dot sizes, local walk.
- [ ] Old commit 83523c3 contains the listener's playlist link: rewrite history and force-push? (user to decide)
- [ ] Genre radio drift: filter hop-1 genre artists against the store genres; genre-closeness weighting from hop 2?
- [ ] Seed contributes nothing: no forced share; report per-seed counts so Post-Vinyl can warn.
- [ ] Seed under another name (e.g. DONDA for Kanye West): treat MusicBrainz same-person/project relations as the seed.
- [ ] Odd artist genres in flow stops: trace through the store and genre index.
- [ ] Flow paths: minimum link weight (most links are < 0.2 and act as shortcuts); paths converge on the
      listener's top genre; local walk mode (stay within N links of the start, X stops) prototyped in the Atlas.
- [ ] Timeout overshoot: docs wording ("after the current fetch").
- [ ] Release manifest says the MusicBrainz dump is "unknown".
- [ ] Harness gaps: per-seed counts, tracks carry a requested genre.
- [ ] Genres with no English name show Wikidata ids as labels.
