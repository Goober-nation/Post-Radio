# postradio/data

Data files shipped inside the package.

| file | source | licence |
|---|---|---|
| `genre_graph.json` | Every MusicBrainz genre with its subgenre and influence links, from [Wikidata](https://www.wikidata.org). Each genre is keyed by its MusicBrainz name. Built by `providers/wikidata.py`. | CC0 (public domain), like Wikidata |

Optional data goes into `storage.data_dir` instead and is not part of the package:

| file | what | licence |
|---|---|---|
| `mb_artists.sqlite` | The local MusicBrainz artist store: names, aliases and top genres | Data © MusicBrainz contributors. Genres and tags are [CC BY-NC-SA 3.0](https://creativecommons.org/licenses/by-nc-sa/3.0/) |
| `genre_weights.json` | Learned genre-pair weights: MusicBrainz co-tags plus ListenBrainz similar artists | CC BY-NC-SA 3.0 |
| `genre_artists.json` | Genre → artists index, up to 300 per genre, for users without the store | CC BY-NC-SA 3.0 |

The code that builds and reads these files is under the MIT licence in the root `LICENSE`.
