# postvinyl-recs

Tunable "exploration radio", like ListenBrainz Deep Cuts but seeded from a track or artist you pick,
with one knob for how far it wanders.

```
pvrecs --artist "Radiohead" -e 0.7 -n 50
pvrecs --track "Tyler, The Creator - See You Again" -e 0.4
pvrecs --artist-mbid <mbid> | --recording-mbid <mbid>   [--json] [--rng-seed N] [--per-artist 2]
```

## How it works

1. **Seed**: resolved via MusicBrainz (names), or LB metadata (recording MBID). A track seed currently
   seeds from its artist and drops the track itself from the output.
2. **Graph**: BFS out to `1 + round(2·explore)` hops, merging edges from
   LB Labs similar-artists, Deezer related-artists and MusicBrainz relations (members/side projects, hop 1).
   Nodes are merged by normalised name. An edge seen from more than one source gets a bonus.
   Deezer artists are matched to MBID nodes only when their top tracks share titles with LB's top
   recordings, which stops name collisions (e.g. two different artists both named "Bilal").
3. **Candidates**: LB popularity top-recordings per artist (junk like live/demo/remix filtered, the
   rank inside the artist used as track popularity), Deezer top tracks, Deezer artist radio around the
   seed and its closest neighbours, and **troi**'s LB-radio artist search in easy/medium/hard mode.
4. **Ranking**: `affinity × hop_fit × artist_pop_fit × track_pop_fit × familiarity`, plus a multi-source bonus.
   - `explore` moves the target hop from 0 to 3, the target track popularity from hits to deep cuts, and the
     artist-popularity ceiling down.
   - Your LB history (top artists and recordings) lowers the score of familiar artists as `explore` rises.
     At `explore ≥ 0.3` it drops tracks you've already played.
   - At most `--per-artist` tracks come from any graph node, and at most half the list can come from a
     single source.

Output columns: `h` = hop, `p` = track popularity, `*` = artist is in your history. `--json` adds the
graph path that led to each artist (`Kendrick Lamar > Thundercat > Terrace Martin`).

## Setup

```
python3.14 -m venv .venv && .venv/bin/pip install -e '.[dev]'
cp ../Post-vinyl/.env-style values -> .env  # LISTENBRAINZ_TOKEN, LISTENBRAINZ_USERNAME
.venv/bin/pytest -q && .venv/bin/ruff check pvrecs tests
```

HTTP responses are cached in `.cache/http.sqlite` (7-day TTL). `--offline` uses the cache only.
A cold run takes about 30–100 s, mostly MusicBrainz's 1 req/s limit. A warm run takes about 1 s.
Peak RSS is about 90 MB. The venv plus cache use about 300 MB.
