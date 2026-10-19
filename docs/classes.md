# Classes

Data crosses between modules only as the normalised types in `postradio/models.py`. Raw API payloads never
leave the adapters. The engine talks only to the interfaces in `postradio/providers/__init__.py`.

## Models and interfaces

```mermaid
classDiagram
    direction LR
    class Artist {
        name
        mbid
        deezer_id
        popularity
        virtual
        key
        absorb()
    }
    class Track {
        title
        artist
        recording_mbid
        deezer_id
        popularity
        sources
        key
    }
    class Edge {
        target: Artist
        weight
        source
    }
    class Play {
        artist
        title
        timestamp
        artist_mbid
        recording_mbid
    }
    class History {
        plays(artist)
        heard(track)
        from_plays()
        from_counts()
    }
    class Feedback {
        artist
        title|None
        score -1..1
        source
        mbids
    }
    class Taste {
        artist(a)
        track(t)
        strongest(n)
    }
    Track --> Artist
    Edge --> Artist
    History ..> Play : built from
    Taste ..> Feedback : built from

    class ArtistResolver {
        <<interface>>
        find_artist()
        artist_by_mbid()
        find_recording()
        recording_by_mbid()
    }
    class SimilarArtists {
        <<interface>>
        neighbours(artist, limit)
        max_hop
    }
    class TopTracks {
        <<interface>>
        top_tracks(artist, limit)
    }
    class RadioSource {
        <<interface>>
        radio(artist, popularity, mode, rng)
    }
    class Tagger {
        <<interface>>
        tags(artist, n)
    }
    class TagRadio {
        <<interface>>
        artists_for_tags()
    }
    class GenreSource {
        <<interface>>
        genre_artists()
        genre_tracks()
        supported_genres()
        unsupported()
    }
    class TrackSimilarity {
        <<interface>>
        similar(track, limit)
    }
    class HistoryProvider {
        <<interface>>
        load()
        version()
    }
    class FeedbackProvider {
        <<interface>>
        load()
        version()
    }
```

## Adapters (what implements what)

| Adapter | ArtistResolver | SimilarArtists | TopTracks | RadioSource | Tagger | TagRadio | GenreSource | TrackSimilarity | HistoryProvider | FeedbackProvider |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `MusicBrainz` | ✓ | ✓ (hop 1 only) | | | ✓ | | | | | |
| `Deezer` | | ✓ | ✓ | ✓ | | | ✓ | | | |
| `LBSimilar` | | ✓ | | | | | | | | |
| `LBTopTracks` (token) | | | ✓ | | | | | | | |
| `TroiRadio` | | | | ✓ | | | | | | |
| `TroiTagRadio` (lb-tags) | | | | | | ✓ | ✓ | | | |
| `LBSimilarRecordings` | | | | | | | | ✓ | | |
| `ListenBrainzHistory`, `FileHistory`, `NoHistory` | | | | | | | | | ✓ | |
| `JsonFeedbackStore`, `LBFeedback`, `NavidromeFeedback` | | | | | | | | | | ✓ |

| Module | Classes |
|---|---|
| `providers/musicbrainz.py` | `MusicBrainz` |
| `providers/deezer.py` | `Deezer` (Deezer identity check: free LB titles first, MusicBrainz titles only when needed) |
| `providers/listenbrainz.py` | `LBSimilar`, `LBTopTracks`, `LBSimilarRecordings`, `TroiRadio`, `TroiTagRadio`, `LBMetadata` |
| `providers/lb_playlists.py` | `LBCreatedFor` (Weekly Exploration / Jams; no interface, called directly) |
| `providers/history.py` | `ListenBrainzHistory`, `FileHistory` |
| `providers/feedback.py` | `LBFeedback`, `NavidromeFeedback` |
| `providers/feedback_store.py` | `JsonFeedbackStore` |

## Engine, API and infrastructure

```mermaid
classDiagram
    direction LR
    class Params {
        explore
        popularity
        n
        per_artist
        unheard
        mix
        seed
        adv
    }
    class Node {
        artist
        hop
        affinity
        via
        parent
    }
    class Graph {
        nodes
        add()
        node_for()
        path()
    }
    class Providers {
        resolver
        similar
        tracks
        radios
        tagger
        tag_radio
        genres
        history
        feedback
        track_similarity
    }
    class Radio {
        generate()
        build_graph()
        candidates()
        _map()
        close()
    }
    class Recommender {
        generate()
        deep_cuts()
        import_feedback()
        save()
        list_saved()
        close()
    }
    class ResultCache {
        put()
        get()
    }
    class Http {
        get()
        post()
        per-host scheduler
        SQLite cache
        close()
    }
    class Config {
        defaults
        storage
        sources
        feedback
        server
        advanced
    }
    Graph --> Node
    Radio --> Graph
    Radio --> Params
    Radio --> Providers
    Recommender --> Radio
    Recommender --> ResultCache
    Recommender --> LBCreatedFor
    Recommender --> JsonFeedbackStore
    Recommender --> Http
    Recommender --> Config
    Params --> Advanced
    Http ..> Advanced : rate-limit defaults
    Config --> Defaults
    Config --> Storage
    Config --> Sources
    Config --> FeedbackCfg
    Config --> Server
    Config --> Advanced
```

`server.py` and `cli.py` are thin wrappers around `Recommender`.
