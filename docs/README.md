# Post-radio documentation

postradio is a seeded, tunable exploration radio. You give it an artist, a track or genres, plus two knobs:
`explore` sets how far out to go and `popularity` sets hits vs deep cuts. It builds a playlist from
ListenBrainz, Deezer and MusicBrainz, shaped by your listening history and loves/hates. It also passes
through ListenBrainz's own Weekly Exploration and Jams playlists. The default setup needs no API key.

| Document | What's in it |
|---|---|
| [architecture.md](architecture.md) | The pipeline in execution order (numbered diagram), how scoring works, concurrency |
| [classes.md](classes.md) | Class diagram: models, provider interfaces, adapters, engine, API, config |
| [configuration.md](configuration.md) | Every CLI command and flag, HTTP endpoint, Python API call, and config key with defaults |
| [library.md](library.md) | Using Post-Radio from another app (Post-Vinyl): folders, per-user calls, results, progress, errors |
| [advanced.md](advanced.md) | Every `advanced` config key (algorithm constants): default and what it does |
| [dependencies.md](dependencies.md) | Module imports, external services (auth, rate limits), measured cost, Deezer genre list |

The quick start is in the [project README](../README.md). `config.example.yaml` is generated from
`postradio/config.py` by `scripts/gen_config_example.py`, so it's always the authoritative list of keys.
