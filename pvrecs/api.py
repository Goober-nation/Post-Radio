"""Public library API: what Post-Vinyl (or the HTTP server) calls.

    rec = Recommender()                       # reads config.yaml / $PVRECS_CONFIG
    res = rec.generate(artist="Radiohead", explore=0.7)
    rec.save(res["result_id"])                 # user liked it -> JSON in storage.save_dir
    rec.deep_cuts()                            # LB Weekly Exploration, both weeks

Results live in an in-process TTL cache until saved; nothing touches disk unless save() is called.
"""
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from . import config as config_mod
from .engine import Params, Providers, Radio
from .http import Http
from .models import Track
from .providers import FeedbackProvider, HistoryProvider, NoHistory
from .providers.deezer import Deezer
from .providers.feedback import LBFeedback, NavidromeFeedback
from .providers.feedback_store import JsonFeedbackStore
from .providers.history import FileHistory, ListenBrainzHistory
from .providers.lb_playlists import KINDS, LBCreatedFor
from .providers.listenbrainz import LBSimilar, LBSimilarRecordings, LBTopTracks, TroiRadio, TroiTagRadio
from .providers.musicbrainz import MusicBrainz

SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class ResultCache:
    """Small thread-safe TTL + LRU cache for unsaved results."""

    def __init__(self, ttl_s: int, max_items: int):
        self.ttl, self.max = ttl_s, max_items
        self._d: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._d[key] = (time.monotonic(), value)
            self._d.move_to_end(key)
            while len(self._d) > self.max:
                self._d.popitem(last=False)

    def get(self, key: str) -> dict | None:
        with self._lock:
            item = self._d.get(key)
            if not item:
                return None
            if time.monotonic() - item[0] > self.ttl:
                del self._d[key]
                return None
            return item[1]


def slug(s: str) -> str:
    return SAFE_NAME.sub("-", s.strip().lower()).strip("-.")[:60] or "playlist"


def track_json(t: Track) -> dict:
    return {"artist": t.artist.name, "title": t.title, "artist_mbid": t.artist.mbid,
            "recording_mbid": t.recording_mbid, "deezer_id": t.deezer_id}


class Recommender:
    def __init__(self, cfg: config_mod.Config | None = None, *, token: str | None = None, lb_user: str | None = None,
                 history: HistoryProvider | None = None, feedback: list[FeedbackProvider] | None = None,
                 offline: bool = False,
                 log: Callable[[str], None] = lambda _m: None):
        """token/lb_user default to $LISTENBRAINZ_TOKEN / $LISTENBRAINZ_USERNAME (subject to cfg.sources).
        Pass `history` / `feedback` to supply listening history or loves/hates from anywhere
        (e.g. a multi-scrobbler/Maloja adapter); otherwise both come from config."""
        self.cfg = cfg or config_mod.load()
        s, a = self.cfg.sources, self.cfg.advanced
        # token: None = take $LISTENBRAINZ_TOKEN, "" = explicitly tokenless
        self.token = (token if token is not None else os.environ.get("LISTENBRAINZ_TOKEN")) or None
        if not s.use_lb_token:
            self.token = None
        self.lb_user = lb_user or s.lb_user or os.environ.get("LISTENBRAINZ_USERNAME") or None
        self.http = Http(self.cfg.path(self.cfg.storage.http_cache), ttl=self.cfg.storage.http_ttl_s, offline=offline,
                         min_interval=a.min_interval_s, max_attempts=a.max_attempts, backoff_cap_s=a.backoff_cap_s,
                         max_in_flight=a.max_in_flight, ratelimit_floor=a.ratelimit_floor)
        self.log, self.offline = log, offline
        st = self.cfg.storage
        store_path = (self.cfg.path(st.feedback_file) if st.feedback_file
                      else self.cfg.path(st.save_dir) / "feedback" / "opinions.jsonl")
        self.feedback_store = JsonFeedbackStore(store_path, self.cfg.advanced.feedback_store_depth)
        self.history = history or self._history_from_config()
        self.feedback = feedback if feedback is not None else self._feedback_from_config()
        self.cache = ResultCache(self.cfg.storage.result_ttl_s, self.cfg.storage.result_cache_size)
        self.radio = Radio(self._providers(), self.cfg.advanced, log=log)
        self.created_for = LBCreatedFor(self.http, self.token)

    def _history_from_config(self) -> HistoryProvider:
        s = self.cfg.sources
        if s.history == "file":
            if not s.history_file:
                raise ValueError("sources.history = file needs sources.history_file")
            return FileHistory(self.cfg.path(s.history_file))
        if s.history == "lb" and self.lb_user:
            return ListenBrainzHistory(self.http, self.lb_user, self.token,
                                       refresh_s=self.cfg.advanced.history_refresh_s)
        return NoHistory()

    def _feedback_from_config(self) -> list[FeedbackProvider]:
        f, a = self.cfg.feedback, self.cfg.advanced
        if not f.enabled:
            return []
        out: list[FeedbackProvider] = []
        for name in f.sources:
            if name == "json":
                out.append(self.feedback_store)
            elif name == "lb":
                user = f.lb_user or self.lb_user
                if user:
                    out.append(LBFeedback(self.http, user, self.token, a.feedback_max_age_s))
            elif name == "navidrome":
                url = f.navidrome_url or os.environ.get("NAVIDROME_URL")
                user, pw = os.environ.get("NAVIDROME_USERNAME"), os.environ.get("NAVIDROME_PASSWORD")
                if url and user and pw:
                    out.append(NavidromeFeedback(self.http, url, user, pw, a.feedback_max_age_s))
                elif url:  # configured but unusable: say why; unconfigured is simply skipped
                    self.log("feedback: navidrome skipped (needs feedback.navidrome_url or $NAVIDROME_URL, "
                             "$NAVIDROME_USERNAME, $NAVIDROME_PASSWORD)")
            else:
                raise ValueError(f"feedback.sources: unknown source '{name}' (json | navidrome | lb)")
        return out

    def _providers(self) -> Providers:
        http, a, token = self.http, self.cfg.advanced, self.token
        mb = MusicBrainz(http)
        lb_top = LBTopTracks(http, token, a.lb_listeners_full_scale, a.lb_min_listener_share) if token else None

        dz = Deezer(http, known_titles=mb.recording_titles, cheap_titles=lb_top.titles if lb_top else None,
                    fans_full_scale=a.deezer_fans_full_scale, rank_full_scale=a.deezer_rank_full_scale,
                    memo_ttl_s=self.cfg.storage.http_ttl_s)
        tracks = [dz] + ([lb_top] if lb_top else [])  # LB's popularity endpoint is the only one needing a token
        tags = TroiTagRadio(http, token, a.troi_window_width, a.genre_recordings_per_artist)
        return Providers(resolver=mb, similar=[LBSimilar(http), dz, mb], tracks=tracks,
                         radios=[dz, TroiRadio(http, token, a.troi_window_width)], tagger=mb, tag_radio=tags,
                         history=self.history, feedback=self.feedback, track_similarity=LBSimilarRecordings(http),
                         genres=[tags, dz])

    # ------------------------------------------------------------------ radio
    def generate(self, *, artist: str | None = None, track: str | None = None, artist_mbid: str | None = None,
                 recording_mbid: str | None = None, genres: list[str] | None = None, genre_op: str = "or",
                 explore: float | None = None, popularity: float | None = None, n: int | None = None,
                 per_artist: int | None = None, rng_seed: int | None = None, auto_popularity: bool = False,
                 unheard: str | None = None, mix: dict[str, float] | None = None) -> dict:
        """Unset knobs fall back to config defaults. auto_popularity=True ignores a configured default
        popularity so it follows explore."""
        d = self.cfg.defaults
        pop = popularity if popularity is not None else (None if auto_popularity else d.popularity)
        p = Params(explore=d.explore if explore is None else explore, n=n or d.n, per_artist=per_artist or d.per_artist,
                   seed=rng_seed, popularity=pop, unheard=self._unheard(unheard), mix=self._mix(mix),
                   adv=self.cfg.advanced)
        for name, v in (("explore", p.explore), ("popularity", p.popularity)):
            if v is not None and not 0 <= v <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        if sum(x is not None for x in (artist, track, artist_mbid, recording_mbid, genres or None)) != 1:
            raise ValueError("give exactly one seed: artist, track, artist_mbid, recording_mbid or genres")
        res = self.radio.generate(p, artist=artist, track=track, artist_mbid=artist_mbid,
                                  recording_mbid=recording_mbid, genres=genres, genre_op=genre_op)
        return self._remember("radio", res, default_name=self._radio_name(res))

    def _mix(self, overrides: dict[str, float] | None) -> dict[str, float]:
        """Config stream multipliers with per-run overrides. Keys: deezer-top, lb, deezer-radio, troi,
        track-sim, lb-tags (underscores accepted)."""
        base = self.cfg.stream_mix()
        for k, v in (overrides or {}).items():
            key = k.replace("_", "-")
            if key not in base:
                raise ValueError(f"unknown mix stream '{k}' (one of {', '.join(base)})")
            if not 0 <= float(v) <= 1:
                raise ValueError(f"mix {k} must be in [0, 1]")
            base[key] = float(v)
        return base

    def _unheard(self, value: str | None) -> str | None:
        """None = config default; "none" switches it off for this run even if the config sets it."""
        v = self.cfg.defaults.unheard if value is None else value
        if v in (None, "none"):
            return None
        if v not in ("tracks", "artists"):
            raise ValueError("unheard must be 'none', 'tracks' or 'artists'")
        return v

    @staticmethod
    def _radio_name(res: dict) -> str:
        seed = res["seed"]["artist"] or ("genre-" + "-".join(res["genres"]))
        seed += f"-{res['seed']['track']}" if res["seed"]["track"] else ""
        pop = "auto" if res["popularity"] is None else f"{res['popularity']:.2f}"
        return f"{datetime.now(UTC):%Y-%m-%d}_{slug(seed)}_e{res['explore']:.2f}_p{pop}"

    # ------------------------------------------------------------------ LB created-for
    def deep_cuts(self, user: str | None = None, kind: str = "exploration") -> dict:
        """LB's own Weekly Exploration ('deep cuts') or Weekly Jams; each week a separate entry."""
        user = user or self.lb_user
        if not user:
            raise ValueError("deep_cuts needs an LB user (argument, sources.lb_user or $LISTENBRAINZ_USERNAME)")
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        weeks = self.created_for.playlists(user, KINDS[kind])
        res = {"kind": KINDS[kind], "user": user, "weeks": [
            {"week": w.week, "title": w.title, "playlist_mbid": w.mbid,
             "url": f"https://listenbrainz.org/playlist/{w.mbid}", "tracks": [track_json(t) for t in w.tracks]}
            for w in weeks]}
        name = f"{datetime.now(UTC):%Y-%m-%d}_lb-{kind}_{slug(user)}"
        return self._remember("lb-created-for", res, default_name=name)

    # ------------------------------------------------------------------ feedback store
    def import_feedback(self, data: dict) -> dict:
        """Append a batch of loves/hates (see providers/feedback_store.py for the format); dated, newest wins."""
        return self.feedback_store.import_batch(data)  # the store's version() changes -> engine reloads taste

    def feedback_batches(self) -> list[dict]:
        return self.feedback_store.batches()

    # ------------------------------------------------------------------ cache & storage
    def _remember(self, kind: str, res: dict, default_name: str) -> dict:
        rid = uuid.uuid4().hex[:12]
        out = {"result_id": rid, "type": kind, "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
               "default_name": default_name, **res}
        self.cache.put(rid, out)
        return out

    def get(self, result_id: str) -> dict | None:
        return self.cache.get(result_id)

    @property
    def save_dir(self) -> Path:
        return self.cfg.path(self.cfg.storage.save_dir)

    def save(self, result_id: str, name: str | None = None, overwrite: bool = False) -> Path:
        res = self.get(result_id)
        if res is None:
            raise KeyError(f"result {result_id} expired or unknown (kept {self.cfg.storage.result_ttl_s}s)")
        path = self.save_dir / f"{slug(name) if name else res['default_name']}.json"
        if path.exists() and not overwrite:
            raise FileExistsError(path)
        self.save_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({**res, "saved_as": path.stem}, indent=2, ensure_ascii=False))
        tmp.replace(path)
        return path

    def list_saved(self) -> list[dict]:
        out = []
        for p in sorted(self.save_dir.glob("*.json")) if self.save_dir.exists() else []:
            try:
                d = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            n = len(d.get("tracks") or []) or sum(len(w["tracks"]) for w in d.get("weeks") or [])
            out.append({"name": p.stem, "type": d.get("type"), "created_at": d.get("created_at"), "tracks": n})
        return out

    def load_saved(self, name: str) -> dict:
        p = self.save_dir / f"{slug(name)}.json"
        if not p.exists():
            raise FileNotFoundError(p)
        return json.loads(p.read_text())

    def close(self) -> None:
        """Release worker threads (and the cache connection). Use per-request Recommenders as `with` blocks."""
        self.radio.close()
        self.http.close()

    def __enter__(self) -> "Recommender":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def with_config(self, **section_overrides) -> "Recommender":
        """Copy with e.g. defaults=replace(cfg.defaults, n=20) — handy for per-request tweaks. Close it when
        done (`with rec.with_config(...) as r:`), or its worker threads stay alive."""
        return Recommender(replace(self.cfg, **section_overrides), token=self.token, lb_user=self.lb_user,
                           history=self.history, feedback=self.feedback, offline=self.offline, log=self.log)
