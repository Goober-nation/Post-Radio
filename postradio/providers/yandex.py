"""Yandex Music: a public playlist (usually the user's "Мне нравится" likes) as taste, listening history and
genre tags, plus Yandex's own similar artists. No auth: the public playlist and artist endpoints.

Yandex answers HTTP 451 from some regions (cloud machines among them), so every call tries the .ru host
first, then .net; a source that can't load contributes nothing to a run, like any other provider.

Album genres are Yandex genre ids ("rusrap", "indie"); `postradio/data/yandex_genres.json` maps each to
MusicBrainz genres (reviewed by hand) and gives the Yandex genre tree, which the genre graph also uses."""
import json
import re
import threading
from collections import Counter
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

from ..errors import InvalidInput, SourceUnavailable
from ..http import Http
from ..models import Artist, Edge, Feedback, History, Play, norm
from . import FeedbackProvider, HistoryProvider, SimilarArtists, Tagger, parse_rows, seg
from .history import _timestamp

HOSTS = ("https://api.music.yandex.ru", "https://api.music.yandex.net")  # .net answers 451 from some regions
GENRE_TABLE = Path(__file__).resolve().parent.parent / "data" / "yandex_genres.json"
NON_ARTIST_ROOT = "soundtrack"  # films, TV series, video games, anime...: "similar artists" that are catalogues

NEW = re.compile(r"https?://music\.yandex\.[a-z]+/playlists/([A-Za-z0-9.\-]+)")
OLD = re.compile(r"https?://music\.yandex\.[a-z]+/users/([^/?#\s]+)/playlists/(\d+)")
IFRAME = re.compile(r"music\.yandex\.[a-z]+/iframe/(?:#/)?playlist/([^/\"]+)/(\d+)")


def playlist_path(url: str) -> str:
    """API path for a playlist link: new-style /playlists/<uuid>, old /users/<owner>/playlists/<kind>, or an
    embed iframe."""
    url = url.strip()
    if m := NEW.search(url):
        return f"/playlist/{seg(m.group(1))}"
    if m := OLD.search(url) or IFRAME.search(url):
        return f"/users/{seg(m.group(1))}/playlists/{seg(m.group(2))}"
    raise InvalidInput("not a Yandex Music playlist link (https://music.yandex.ru/playlists/… or "
                      "https://music.yandex.ru/users/<owner>/playlists/<number>)")


@cache
def genre_table(path: Path = GENRE_TABLE) -> dict[str, dict]:
    """Yandex genre id -> {parent, mb: [MusicBrainz genres], ru, en}."""
    return json.loads(path.read_text())


def non_artist_genres(table: dict[str, dict]) -> set[str]:
    """The soundtrack branch of the Yandex genre tree."""
    out, changed = {NON_ARTIST_ROOT}, True
    while changed:
        more = {k for k, v in table.items() if v.get("parent") in out} - out
        out |= more
        changed = bool(more)
    return out


@dataclass(frozen=True)
class LikedTrack:
    artists: tuple[tuple[str, str | None], ...]  # (name, Yandex artist id), as credited, main artist first
    title: str
    added_at: int | None  # unix seconds
    genre: str | None  # Yandex album genre id

    @property
    def artist(self) -> str:
        return self.artists[0][0]


@dataclass
class Playlist:
    title: str
    total: int
    tracks: list[LikedTrack] = field(default_factory=list)  # newest first


class YandexClient:
    def __init__(self, http: Http):
        self.http = http

    def _get(self, path: str, params: dict | None = None):
        for host in HOSTS:
            d = self.http.get(host + path, params=params)
            if isinstance(d, dict) and d.get("result"):
                return d["result"]
        return None

    def playlist(self, url: str, limit: int | None = None, log=lambda _m: None) -> Playlist:
        """Every track (or the newest `limit`), newest first. Big playlists come paged; if Yandex stops paging
        early, what arrived is kept and the gap is logged."""
        path = playlist_path(url)
        data = self._get(path)
        if not isinstance(data, dict):
            raise SourceUnavailable("Yandex didn't return the playlist (private, missing, or blocked in this region)")
        items = list(data.get("tracks") or [])
        total = int(data.get("trackCount") or len(items))
        per = int((data.get("pager") or {}).get("perPage") or 0)
        page = 1
        while per and len(items) < total:
            more = (self._get(path, {"page": page}) or {}).get("tracks") or []
            if not more:
                log(f"yandex: paging stopped at {len(items)} of {total} tracks")
                break
            items += more
            page += 1
        tracks = parse_rows(items, _liked)
        tracks.sort(key=lambda t: t.added_at or 0, reverse=True)
        return Playlist(str(data.get("title") or ""), total, tracks[:limit] if limit else tracks)

    def similar(self, artist_id: str) -> list[tuple[str, str, list[str]]]:
        """(id, name, Yandex genre ids) of the artists Yandex calls similar, in Yandex's order."""
        d = self._get(f"/artists/{seg(artist_id)}/similar")
        rows = (d or {}).get("similarArtists") if isinstance(d, dict) else None
        return parse_rows(rows, lambda a: (str(a["id"]), a["name"], [str(g) for g in a.get("genres") or []])
                          if a.get("id") and a.get("name") else None)


def _liked(item: dict) -> LikedTrack | None:
    t = item.get("track") or item
    artists = tuple((a["name"], str(a["id"]) if a.get("id") else None) for a in t.get("artists") or []
                    if isinstance(a, dict) and a.get("name"))
    if not artists or not t.get("title"):
        return None
    albums = t.get("albums") or [{}]
    genre = albums[0].get("genre") if isinstance(albums[0], dict) else None
    return LikedTrack(artists, t["title"], _timestamp(item.get("timestamp")), genre)


class YandexLikes(FeedbackProvider, Tagger):
    """A public Yandex playlist as taste (each track a love of that track), history (each track a play, dated
    when it was added) and tags (an artist's MusicBrainz genres from its albums' Yandex genres in the
    playlist). The playlist loads once, on first use; `limit` keeps only the newest N tracks. load() is the
    feedback; as_history() is the same playlist as a HistoryProvider."""

    name = "yandex"

    def __init__(self, http: Http, url: str, limit: int | None = None, log=lambda _m: None):
        playlist_path(url)  # a bad link fails now, not mid-run
        self.client, self.url, self.limit, self.log = YandexClient(http), url, limit, log
        self._playlist: Playlist | None = None
        self._lock = threading.Lock()

    @property
    def playlist(self) -> Playlist:
        with self._lock:
            if self._playlist is None:
                self._playlist = self.client.playlist(self.url, self.limit, self.log)
                self.log(f"yandex: {len(self._playlist.tracks)} of {self._playlist.total} tracks "
                         f"from '{self._playlist.title}'")
            return self._playlist

    def load(self) -> list[Feedback]:
        """Every liked track under each of its credited artists: a song liked as "A, B" is the same song when
        another source offers it under B alone."""
        return [Feedback(name, t.title, 1.0, self.name) for t in self.playlist.tracks for name, _ in t.artists]

    def liked(self) -> list[tuple[Feedback, int | None]]:
        """Every liked track, credited to each of its artists, dated when it was added."""
        return [(Feedback(name, t.title, 1.0, self.name), t.added_at) for t in self.playlist.tracks
                for name, _ in t.artists]

    def history(self) -> History:
        return History.from_plays(Play(name, t.title, t.added_at) for t in self.playlist.tracks
                                  for name, _ in t.artists)

    def as_history(self) -> HistoryProvider:
        return _History(self)

    def tags(self, artist: Artist, n: int = 4) -> list[str]:
        table, counts = genre_table(), Counter()
        key = norm(artist.name)
        for t in self.playlist.tracks:
            if any(norm(name) == key for name, _ in t.artists):
                counts.update(table.get(t.genre or "", {}).get("mb") or [])
        return [g for g, _ in sorted(counts.items(), key=lambda x: (-x[1], x[0]))][:n]

    def artist_ids(self) -> dict[str, str]:
        """normalised artist name -> Yandex artist id, for every credited artist in the playlist."""
        return {norm(name): aid for t in self.playlist.tracks for name, aid in t.artists if aid}

    def top_artists(self, n: int) -> list[tuple[str, str]]:
        """(name, Yandex id) of the main artists with the most tracks, most first."""
        c = Counter((t.artists[0][0], t.artists[0][1]) for t in self.playlist.tracks if t.artists[0][1])
        return [a for a, _ in c.most_common(n)]


class _History(HistoryProvider):
    def __init__(self, likes: YandexLikes):
        self.likes = likes

    def load(self) -> History:
        return self.likes.history()


class YandexSimilar(SimilarArtists):
    """Yandex's similar artists. Yandex ids come from the liked playlist, so only artists the user has liked
    can be expanded; for anyone else this returns nothing. Soundtrack/game/anime catalogues are dropped."""

    name = "yandex"

    def __init__(self, http: Http, likes: YandexLikes):
        self.client, self.likes = YandexClient(http), likes
        self.skip = non_artist_genres(genre_table())

    def neighbours(self, artist: Artist, limit: int) -> list[Edge]:
        aid = self.likes.artist_ids().get(norm(artist.name))
        if not aid:
            return []
        rows = [r for r in self.client.similar(aid) if not set(r[2]) & self.skip][:limit]
        return [Edge(Artist(name), 1.0 - 0.5 * i / max(1, len(rows)), self.name) for i, (_, name, _) in
                enumerate(rows)]
