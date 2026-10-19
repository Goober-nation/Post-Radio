"""Feedback adapters: ListenBrainz loves/hates and Navidrome stars + 1-5 star ratings."""
import hashlib
import secrets

from ..config import Advanced
from ..http import Http
from ..models import Feedback, rating_score
from . import FeedbackProvider

LB_API = "https://api.listenbrainz.org/1"


class LBFeedback(FeedbackProvider):
    """Recording loves (+1) and hates (-1). Public, no token needed."""

    name = "lb"

    def __init__(self, http: Http, user: str, token: str | None = None,
                 max_age_s: int = Advanced.feedback_max_age_s, cap: int = 2000):
        self.http, self.user, self.max_age, self.cap = http, user, max_age_s, cap
        self.headers = {"Authorization": f"Token {token}"} if token else None

    def load(self) -> list[Feedback]:
        out = []
        for score in (1, -1):
            for off in range(0, self.cap, 100):
                d = self.http.get(f"{LB_API}/feedback/user/{self.user}/get-feedback",
                                  params={"score": score, "count": 100, "offset": off, "metadata": "true"},
                                  headers=self.headers, max_age=self.max_age)
                rows = (d or {}).get("feedback") or []
                for f in rows:
                    m = f.get("track_metadata") or {}
                    mapping = m.get("mbid_mapping") or {}
                    if not (m.get("artist_name") and m.get("track_name")):
                        continue
                    out.append(Feedback(m["artist_name"], m["track_name"], float(score), self.name,
                                        artist_mbid=(mapping.get("artist_mbids") or [None])[0],
                                        recording_mbid=f.get("recording_mbid") or mapping.get("recording_mbid")))
                if len(rows) < 100:
                    break
        return out


class NavidromeFeedback(FeedbackProvider):
    """Subsonic API: starred songs/albums/artists (+1) and userRating 1..5 on songs, albums and artists.
    Album opinions count as half-strength opinions about the album artist."""

    name = "navidrome"

    def __init__(self, http: Http, url: str, user: str, password: str,
                 max_age_s: int = Advanced.feedback_max_age_s, page: int = 500, max_songs: int = 200_000):
        self.http, self.url, self.user, self.password = http, url.rstrip("/"), user, password
        self.max_age, self.page, self.max_songs = max_age_s, page, max_songs

    def _call(self, endpoint: str, **params) -> dict:
        salt = secrets.token_hex(6)
        auth = {"u": self.user, "t": hashlib.md5((self.password + salt).encode()).hexdigest(), "s": salt,
                "v": "1.16.1", "c": "pvrecs", "f": "json"}
        # auth params change every call; cache on the stable part only
        d = self.http.get(f"{self.url}/rest/{endpoint}", params={**params, **auth}, max_age=self.max_age,
                          cache_params=params, valid=_subsonic_ok)
        r = (d or {}).get("subsonic-response") or {}
        if r.get("status") != "ok":
            raise RuntimeError(f"navidrome {endpoint}: {(r.get('error') or {}).get('message', 'no response')}")
        return r

    def load(self) -> list[Feedback]:
        out: dict[tuple, Feedback] = {}

        def put(f: Feedback, key: tuple):
            if f.score and (key not in out or abs(f.score) > abs(out[key].score)):
                out[key] = f

        def song(s: dict):
            score = rating_score(s["userRating"]) if s.get("userRating") else 1.0 if s.get("starred") else 0.0
            put(Feedback(s.get("artist") or "", s.get("title") or "", score, self.name,
                         recording_mbid=s.get("musicBrainzId") or None), ("s", s.get("id")))

        starred = self._call("getStarred2").get("starred2") or {}
        for s in starred.get("song") or []:
            song(s)
        for a in starred.get("artist") or []:
            put(Feedback(a["name"], None, 1.0, self.name, artist_mbid=a.get("musicBrainzId") or None), ("a", a["id"]))
        for al in starred.get("album") or []:
            put(Feedback(al.get("artist") or "", None, 0.5, self.name), ("al", al["id"]))
        # ratings aren't listable directly: walk the library (paged, cached for max_age)
        for off in range(0, self.max_songs, self.page):
            res = self._call("search3", query="", songCount=self.page, songOffset=off, artistCount=0, albumCount=0)
            songs = (res.get("searchResult3") or {}).get("song") or []
            for s in songs:
                if s.get("userRating") or s.get("starred"):
                    song(s)
            if len(songs) < self.page:
                break
        res = self._call("getArtists")
        for idx in (res.get("artists") or {}).get("index") or []:
            for a in idx.get("artist") or []:
                if a.get("userRating"):
                    put(Feedback(a["name"], None, rating_score(a["userRating"]), self.name,
                                 artist_mbid=a.get("musicBrainzId") or None), ("a", a["id"]))
        return list(out.values())


def _subsonic_ok(d) -> bool:
    return ((d or {}).get("subsonic-response") or {}).get("status") == "ok"
