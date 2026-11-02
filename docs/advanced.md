# `advanced`: every algorithm constant

Every key under `advanced:` in `config.yaml`, with its default and what it does. Only the keys you change
are needed. Dicts merge with the defaults: `source_weights: {deezer: 0.5}` changes only `deezer`. Unknown
keys and wrong types are errors. The defaults are hand-tuned on about 10 seeds (radio) or ported untuned
(genre graph, flow), so treat them as starting points. `postradio/config.py` is the source of truth.

Terms used below:
- *explore* and *popularity* are the run's knobs (0–1).
- A *hop* is one step in the artist graph (radio) or the genre graph (flow).
- A *fit* is a bell curve `exp(−(x − target)² / width)`, so a smaller width means stricter.

## Stream caps

| key | default | what it does |
|---|---|---|
| `mix` | each stream 1.0 | Caps each candidate stream's pool, 0–1: 1 keeps all, 0 switches it off. It keeps `round(len × cap)` candidates nearest the popularity target (`track-sim` keeps the most similar first). It shrinks the pool, not the stream's share of the playlist. Per run: `--mix`. Streams: `deezer-top` (Deezer artist top tracks), `lb` (ListenBrainz popularity catalogue, token only), `deezer-radio` (Deezer artist radio), `troi` (LB Radio), `track-sim` (similar recordings injected by a track seed), `lb-tags` (tagged recordings injected by a genre seed). |

## Radio: explore → graph shape

| key | default | what it does |
|---|---|---|
| `hop_span` | 3.0 | Target hop = `hop_span × explore`; artists near it fit best |
| `max_extra_hops` | 2 | Graph depth = `1 + round(max_extra_hops × explore)` hops (1–3) |
| `radio_mode_thresholds` | [0.34, 0.67] | Explore cut-offs for LB Radio's easy / medium / hard mode |

## Radio: explore and popularity → targets

| key | default | what it does |
|---|---|---|
| `pop_slope` | 0.85 | Without `-p`: track popularity target = `1 − pop_slope × explore` |
| `ceiling_base` | 0.95 | Without `-p`: artist popularity ceiling = `ceiling_base − ceiling_slope × explore` |
| `ceiling_slope` | 0.6 | See `ceiling_base` |
| `slider_ceiling_base` | 0.35 | With `-p`: artist ceiling = `slider_ceiling_base + slider_ceiling_slope × popularity` |
| `slider_ceiling_slope` | 0.6 | See `slider_ceiling_base` |
| `seed_pop_margin` | 0.2 | Without `-p`: the ceiling never exceeds the seed's popularity plus this, so obscure seeds stay obscure |

## Radio: fit curve widths

| key | default | what it does |
|---|---|---|
| `hop_width` | 1.2 | Width of the hop fit around the target hop |
| `pop_width` | 0.12 | Width of the track popularity fit around the target (also used by flow) |
| `artist_pop_width` | 0.03 | Width of the penalty for artists above the popularity ceiling (only being *above* is penalised) |
| `unknown_artist_pop_fit` | 0.7 | Artist fit used when an artist's popularity is unknown |

## Radio: artist graph

| key | default | what it does |
|---|---|---|
| `source_weights` | lb 1.0, deezer 0.85, mb 0.6, tag 0.5, radio 0.5, genre 0.9, yandex 0.85 | Edge weight per similarity source. An artist's affinity is the product of the weights along its best path from the seed. |
| `agreement_bonus` | 1.15 | Affinity multiplier when a second source reaches the same artist (capped at 1) |
| `blend_bonus` | 1.3 | Several seeds: affinity multiplier for each further seed that reaches the same artist (capped at 1), so artists between the seeds rank higher |
| `graph_cap` | 160 | Maximum artists in the graph, split across hops in proportion to the hop number |
| `fanout` | {1: 14, 2: 8, 3: 6} | Neighbours asked per artist at each hop; deeper hops use the last value |
| `frontier_base` | 16 | Artists expanded at hop h = `max(frontier_min, frontier_base // h)` |
| `frontier_min` | 4 | See `frontier_base` |
| `sparse_graph` | 8 | A seed with fewer hop-1 artists than this gets extra neighbours from its tags |
| `tag_neighbours` | 20 | Neighbours added by that tag fallback |
| `tag_radio_recordings` | 60 | Tagged recordings fetched to find those tag neighbours |
| `genre_seed_artists` | 30 | Genre seeds: hop-1 artists per genre source |
| `genre_seed_tracks` | 100 | Genre seeds: tagged recordings injected as candidates |
| `genre_recordings_per_artist` | 3 | LB tag search fetches `limit × this` recordings to find `limit` distinct artists |

## Radio: candidates

| key | default | what it does |
|---|---|---|
| `shortlist_min` | 40 | Artists whose catalogues are fetched: the best `max(shortlist_min, n)` by weight |
| `candidate_artists_min` | 18 | Of those, the best `max(candidate_artists_min, n // 2)` give candidates |
| `radio_neighbours` | 3 | Radio streams are pulled for the seed and this many closest hop-1 artists |
| `top_tracks_limit` | 100 | Tracks asked per artist catalogue (radio and flow) |
| `junk_title_pattern` | live, demo, remix, edit, instrumental, karaoke, session, bootleg, version, dub, mix, unplugged, tribute, originally performed, in the style of, re-recording, from the series/film/movie/game, titles starting with "[" or with years in brackets, … | Regex (case-insensitive) on track titles; matches are dropped (radio and flow) |

## Radio: ranking

| key | default | what it does |
|---|---|---|
| `familiarity_strength` | 0.9 | A known artist's weight × `1 − strength × explore × familiarity` |
| `familiarity_saturation` | 200 | Plays at which an artist counts as fully familiar (log scale up to it) |
| `heard_skip_explore` | 0.3 | At or above this explore, tracks you've played are dropped |
| `heard_penalty` | 0.6 | Below it, played tracks are multiplied by this |
| `multi_source_bonus` | 0.2 | A track found by several streams × `1 + bonus × (sources − 1)` |
| `jitter` | 0.3 | Random factor in [1 − jitter/2, 1 + jitter/2] per track, so runs differ. `--rng-seed` repeats a run. (Radio and flow.) |
| `shuffle_band` | 10 | The ranked list is shuffled within bands of this many, so it doesn't read as a strict ranking |
| `max_source_share` | 0.5 | Soft cap: at most this share of the playlist from one source, unless the list would come up short |

## Taste (loves, hates, ratings)

| key | default | what it does |
|---|---|---|
| `love_factor` | 1.5 | Weight × `love_factor ^ score` for a positive score (artists and tracks) |
| `hate_factor` | 0.3 | Weight × `hate_factor ^ −score` for a negative score |
| `feedback_track_cap` | 50 | Strongest track opinions whose similar recordings get boosted |
| `similar_recordings_limit` | 50 | Similar recordings fetched per opinion |
| `seed_track_similar_limit` | 50 | A track seed injects this many similar recordings |
| `seed_track_boost` | 1.0 | Those injected tracks × `1 + boost × similarity` |
| `seed_track_affinity` | 0.8 | Graph affinity of artists reached only through the seed track's similar recordings |
| `feedback_max_age_s` | 900 | Loves and ratings are re-read after this many seconds |
| `history_refresh_s` | 86400 | Long-running processes reload listening history this often |
| `feedback_store_depth` | 5000 | The imported-loves log: only the newest N opinions are read |

## Provider scales

| key | default | what it does |
|---|---|---|
| `deezer_fans_full_scale` | 6.9 | log10 of a superstar's Deezer fan count: artist popularity = `log10(fans) / this` |
| `deezer_rank_full_scale` | 900000 | Deezer track rank of a top hit. Track popularity = `rank / this`, used only without catalogue context (within an artist's catalogue, ranks are compared with each other). |
| `lb_listeners_full_scale` | 5.5 | log10 of a superstar's LB listener count: artist popularity from LB |
| `lb_min_listener_share` | 0.002 | LB catalogue: recordings below this share of the artist's top track are dropped |
| `troi_window_width` | 34 | Width of the popularity percentile window LB Radio picks from, centred by the popularity target |

## Genre graph (`postradio/genres.py`)

| key | default | what it does |
|---|---|---|
| `genre_kind_weights` | subgenre_of 1.0, influenced_by 0.5, yandex_parent 0.3 | Weight per structural link type (Wikidata subgenre, Wikidata influence, Yandex genre tree) |
| `genre_fallback_scale` | 0.2 | With learned weights installed, structural links only fill genres the data never reaches, scaled by this |
| `genre_spread_hops` | 2 | A taste profile spreads this many hops over the genre graph. This also limits how far flow's destination can be. |
| `genre_spread_decay` | 0.5 | Each hop passes on `score × link weight × decay`; a genre keeps its best path |

## Flow mode (`postradio/flow.py`)

| key | default | what it does |
|---|---|---|
| `flow_half_life_days` | 365 | A like this many days older than the newest counts half (0 = no decay). Long by default: a library is years of likes, not a mood |
| `flow_profile_artists` | 150 | Most-liked artists whose genres make up the profile |
| `flow_profile_api` | false | With the store installed, also ask the MusicBrainz API for artists the store doesn't know or has no genres for (1–4 s each). Off = store only. |
| `flow_source_shares` | mb 0.7, yandex 0.3 | Blend of genre sources in the profile; each is normalised on its own first |
| `flow_genre_rarity` | 1.0 | Profile: weights each genre by how rare it is among artists in the store, `(log(N / artists with it) / log N) ^ this`, so broad tags like pop or hip hop count less than specific ones. 0 = off; needs the store |
| `flow_max_genre_share` | 1.0 | Profile: no genre may hold more than this share; the excess goes to the other genres in proportion. 1 = off. Keeps one dominant genre from steering every flow |
| `flow_parent_discount` | 0.3 | Profile: when an artist has a genre and one of its subgenres (Wikidata "subgenre of"), the broader one counts this much for that artist. 1 = off |
| `flow_stops` | [2, 5] | Genre stops on the path, at explore 0 and 1 (straight line between, rounded). `stops=` overrides it |
| `flow_taste_weight` | [2.0, 0.7] | How strongly taste steers each step, at explore 0 and 1: a step scores link weight × closeness ^ this. 0 = follow link strength only. `taste_weight=` overrides it |
| `flow_min_link_weight` | [0.07, 0.07] | Genre links weaker than this aren't walked, at explore 0 and 1 (equal = not tied to explore). Most links are under 0.2 and act as shortcuts. `min_link_weight=` overrides it |
| `flow_min_genre_weight` | 0.23 | Genres below this weight are left out of the walk. Weight: a profile genre's √(share ÷ top share), any other genre's log(1 + links) ÷ log(1 + most links). 0.23 drops your minor genres (under ~5% of your top one) and genres with only a few links |
| `flow_radius` | 0 | Every stop within this many links of the start; 0 = off. `radius=` overrides it |
| `flow_new_artists` | 30 | Upper limit on new-artist candidates per stop (actually about 3 per wanted new track) |
| `flow_familiar_artists` | 12 | Upper limit on liked artists per stop (actually about 3 per wanted familiar track) |
| `flow_tracks_per_artist` | 5 | An artist's best-fitting tracks kept as candidates |
| `flow_artist_gap` | 4 | The same artist is kept at least this many tracks apart, by swaps within a stop |

Flow constants not yet in config (fixed in `flow.py` / `engine.py`):
- A destination must be a genre you have under 0.15 of your top genre's share.
- Each step picks among the 4 strongest neighbouring genres.
- Very close genres pull less at high explore (0.7).
- The path starts among your top 3 genres, and the destination is one of the best 3.
- After the destination, the start family is damped by `1 − explore`.
- A route step costs `1.05 − link weight`.
- A stop's share of new artists equals explore.

## Rate limits and concurrency

| key | default | what it does |
|---|---|---|
| `min_interval_s` | MusicBrainz 1.1, Wikidata 1.0, Deezer 0.12, LB 0.34, LB labs 0.1, other 0.05 | Minimum seconds between request starts per host, shared by every `Recommender` in the process |
| `max_in_flight` | MusicBrainz 1, Wikidata 1, Deezer 6, LB 4, LB labs 4, other 4 | Maximum concurrent requests per host |
| `max_attempts` | MusicBrainz 5, other 4 | Tries per request on errors, 429/5xx and unparseable bodies |
| `ratelimit_floor` | 2 | LB: when `X-RateLimit-Remaining` reaches this, every thread waits for the window to reset |
| `fetch_workers` | 8 | Threads for independent fetches (catalogues, neighbours, similar recordings) |
| `backoff_cap_s` | 30 | Longest single wait between retries (2, 4, 8, … seconds up to this) |
| `http_cache_keep_s` | 2592000 (30 days) | Cached responses older than this are deleted. Until then, a stale answer is used if the service fails. |
