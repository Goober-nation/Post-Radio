"""Optional HTTP wrapper around api.Recommender (the CLI's `serve`). Same operations as the library."""
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .api import Listener, Recommender
from .brand import DISPLAY_NAME, VERSION
from .errors import (
    Cancelled,
    ConfigError,
    DataError,
    InvalidInput,
    NotFound,
    PostRadioError,
    SourceAuthError,
    SourceUnavailable,
)
from .models import Like, Play
from .results import SCHEMA

API_VERSION = 1  # bumped when an endpoint or request field changes meaning or is removed; additions keep it

STATUS = [(NotFound, 404), (InvalidInput, 422), (SourceAuthError, 422), (SourceUnavailable, 503),
          (ConfigError, 500), (DataError, 500)]


def status_of(e: Exception) -> int:
    if isinstance(e, Cancelled):
        return 504 if e.timed_out else 499
    return next((c for cls, c in STATUS if isinstance(e, cls)), 500)


def http_error(e: Exception) -> HTTPException:
    """One mapping from Post-Radio errors to HTTP codes, for every endpoint."""
    return HTTPException(status_of(e), str(e))


class Jobs:
    """Long runs (20 s to minutes) as background jobs: submit returns an id at once; get() reports status, the
    latest progress event and, once finished, the result or a typed error. cancel() stops a queued or running
    job (status "cancelled"). Finished jobs expire after `ttl_s`."""

    def __init__(self, workers: int, ttl_s: float):
        self.pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="postradio-job")
        self.ttl_s, self.jobs, self.lock = ttl_s, {}, threading.Lock()

    def submit(self, kind: str, run: Callable[[Callable, threading.Event], object]) -> dict:
        """run(progress, cancel) does the work; it should pass `cancel` on to the Recommender call."""
        job = {"job_id": uuid.uuid4().hex[:12], "kind": kind, "status": "queued", "progress": None, "result": None,
               "error": None, "created": time.time(), "finished": None, "cancel": threading.Event()}
        with self.lock:
            self._prune()
            self.jobs[job["job_id"]] = job

        def on_progress(p):
            job["progress"] = p.to_dict()

        def work():
            with self.lock:
                if job["cancel"].is_set():
                    return  # cancelled while queued; cancel() already finished it
                job["status"] = "running"
            try:
                job["result"] = run(on_progress, job["cancel"]).to_dict()
                job["status"] = "done"
            except Cancelled as e:
                job["status"] = "error" if e.timed_out else "cancelled"
                job["error"] = {"type": "Cancelled", "message": str(e), "status": status_of(e)}
            except PostRadioError as e:
                job["error"] = {"type": type(e).__name__, "message": str(e), "status": status_of(e)}
                job["status"] = "error"
            except Exception as e:  # noqa: BLE001 - reported to the caller instead of a lost thread
                job["error"] = {"type": "InternalError", "message": f"{type(e).__name__}: {e}", "status": 500}
                job["status"] = "error"
            finally:
                job["finished"] = time.time()

        self.pool.submit(work)
        return self.view(job)

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            self._prune()
            job = self.jobs.get(job_id)
        return self.view(job) if job else None

    def cancel(self, job_id: str) -> dict | None:
        """Ask a job to stop. A queued job is finished at once; a running one stops at its next stage or fetch
        (usually within a second) and then reads "cancelled". A finished job is left as it is."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if job["finished"] is None:
                job["cancel"].set()
                if job["status"] == "queued":
                    job["status"], job["finished"] = "cancelled", time.time()
                    job["error"] = {"type": "Cancelled", "message": "cancelled", "status": 499}
        return self.view(job)

    def _prune(self) -> None:
        now = time.time()
        for k in [k for k, j in self.jobs.items() if j["finished"] and now - j["finished"] > self.ttl_s]:
            del self.jobs[k]

    @staticmethod
    def view(job: dict) -> dict:
        return {k: job[k] for k in ("job_id", "kind", "status", "progress", "result", "error")} | {
            "status_url": f"/jobs/{job['job_id']}"}

    def close(self) -> None:
        with self.lock:
            for j in self.jobs.values():
                j["cancel"].set()
        self.pool.shutdown(wait=False, cancel_futures=True)


class LikeIn(BaseModel):
    artist: str = Field(min_length=1)
    title: str | None = None
    at: int | None = None  # unix seconds
    score: float = Field(1.0, ge=-1, le=1)
    artist_mbid: str | None = None
    recording_mbid: str | None = None


class PlayIn(BaseModel):
    artist: str = Field(min_length=1)
    title: str = Field(min_length=1)
    timestamp: int | None = None
    artist_mbid: str | None = None
    recording_mbid: str | None = None


class ListenerIn(BaseModel):
    """This request's listener: replaces the server's configured taste and history for this call only. The
    token is used for the call and never stored or logged."""
    model_config = {"extra": "forbid"}
    likes: list[LikeIn] = []
    plays: list[PlayIn] = []
    yandex_playlist: str | None = None
    yandex_tracks: int | None = Field(None, ge=1)
    lb_user: str | None = None
    lb_token: str | None = None

    def build(self) -> Listener:
        return Listener(likes=[Like(**x.model_dump()) for x in self.likes],
                        plays=[Play(**x.model_dump()) for x in self.plays], yandex_playlist=self.yandex_playlist,
                        yandex_tracks=self.yandex_tracks, lb_user=self.lb_user, lb_token=self.lb_token)


class RadioRequest(BaseModel):
    """Exactly one seed kind. artist / track / artist_mbid / recording_mbid take one value or a list (up to 5,
    blended)."""
    artist: str | list[str] | None = None
    track: str | list[str] | None = None  # "Artist - Title"
    artist_mbid: str | list[str] | None = None
    recording_mbid: str | list[str] | None = None
    genres: list[str] | None = None
    genre_op: Literal["or", "and"] = "or"
    taste: bool = True  # False: likes/dislikes don't change scores (disliked tracks and artists still dropped)
    unheard: Literal["none", "tracks", "artists"] | None = None  # None = config default
    explore: float | None = Field(None, ge=0, le=1)
    popularity: float | None = Field(None, ge=0, le=1)
    n: int | None = Field(None, ge=1, le=500)
    per_artist: int | None = Field(None, ge=1)
    rng_seed: int | None = None
    auto_popularity: bool = False  # ignore configured default popularity; follow explore
    mix: dict[str, float] | None = None  # per-run stream multipliers 0..1, e.g. {"deezer-top": 0.2}
    dry: bool = False  # preview the artist graph only; nothing is stored
    timeout_s: float | None = Field(None, gt=0)  # stop with 504 (or a job error) after this long
    listener: ListenerIn | None = None  # per-request taste; None = the server's configured taste


class FlowRequest(BaseModel):
    """A playlist from the configured likes (Yandex playlist, Navidrome, LB loves, imported loves); no seed."""
    explore: float | None = Field(None, ge=0, le=1)
    popularity: float | None = Field(None, ge=0, le=1)
    n: int | None = Field(None, ge=1, le=500)
    per_artist: int | None = Field(None, ge=1)
    stops: int = Field(5, ge=1, le=20)
    start: str | None = None  # the first genre stop; None = one of the listener's strongest genres
    rng_seed: int | None = None
    dry: bool = False  # preview the profile and genre path only; nothing is stored
    timeout_s: float | None = Field(None, gt=0)  # stop with 504 (or a job error) after this long
    listener: ListenerIn | None = None  # per-request taste; None = the server's configured taste


class ArtistRef(BaseModel):
    name: str = Field(min_length=1)
    mbid: str | None = None


class AlbumRef(BaseModel):
    artist: str = Field(min_length=1)
    title: str = Field(min_length=1)
    artist_mbid: str | None = None


class TrackRef(AlbumRef):
    recording_mbid: str | None = None


class FeedbackSection(BaseModel):
    model_config = {"extra": "forbid"}
    artists: list[str | ArtistRef] = []
    albums: list[AlbumRef] = []
    tracks: list[TrackRef] = []


class FeedbackImport(BaseModel):
    """Binary loves/hates. Each import is stored as one dated batch; the newest opinion per item wins."""

    model_config = {"extra": "forbid", "json_schema_extra": {"examples": [{
        "loves": {"artists": ["Slowdive", {"name": "Pixies", "mbid": "b6b2bb8d-54a9-491f-9607-7b546023b433"}],
                  "tracks": [{"artist": "Cocteau Twins", "title": "Heaven or Las Vegas"}]},
        "hates": {"albums": [{"artist": "My Bloody Valentine", "title": "m b v"}]}}]}}
    loves: FeedbackSection | None = None
    hates: FeedbackSection | None = None


class SaveRequest(BaseModel):
    name: str | None = None
    overwrite: bool = False


def create_app(rec: Recommender | None = None) -> FastAPI:
    rec = rec or Recommender()

    jobs = Jobs(rec.cfg.server.job_workers, rec.cfg.storage.result_ttl_s)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        jobs.close()
        rec.close()  # release worker threads and the cache connection with the server

    app = FastAPI(title=DISPLAY_NAME, version=VERSION, lifespan=lifespan)

    @app.get("/health")
    def health():
        """For clients and container health checks: refuse to talk to a server whose api differs."""
        release = rec.data_status().get("release") or {}
        return {"ok": True, "version": VERSION, "api": API_VERSION, "schema": SCHEMA,
                "store": rec.store is not None, "data_release": release.get("tag")}

    # plain `def` handlers run in FastAPI's threadpool, so blocking provider I/O is fine
    @app.post("/radio")
    def radio(req: RadioRequest):
        seeds = (req.artist, req.track, req.artist_mbid, req.recording_mbid, req.genres)
        if sum(bool(x) for x in seeds) != 1:
            raise HTTPException(422, "give exactly one of artist, track, artist_mbid, recording_mbid, genres")
        try:
            args = req.model_dump(exclude={"listener"})
            return rec.generate(**args, keep=not req.dry,
                                listener=req.listener.build() if req.listener else None).to_dict()
        except PostRadioError as e:
            raise http_error(e) from e

    @app.post("/flow")
    def flow(req: FlowRequest):
        try:
            args = req.model_dump(exclude={"listener"})
            return rec.flow(**args, keep=not req.dry,
                            listener=req.listener.build() if req.listener else None).to_dict()
        except PostRadioError as e:
            raise http_error(e) from e

    @app.get("/profile")
    def profile():
        try:
            return rec.profile().to_dict()
        except PostRadioError as e:
            raise http_error(e) from e

    @app.post("/profile")
    def profile_for(listener: ListenerIn):
        try:
            return rec.profile(listener.build()).to_dict()
        except PostRadioError as e:
            raise http_error(e) from e

    # long runs as background jobs: POST returns 202 + a job id at once; poll GET /jobs/{id}
    def _check_radio(req: RadioRequest) -> None:
        seeds = (req.artist, req.track, req.artist_mbid, req.recording_mbid, req.genres)
        if sum(bool(x) for x in seeds) != 1:
            raise HTTPException(422, "give exactly one of artist, track, artist_mbid, recording_mbid, genres")

    @app.post("/jobs/radio", status_code=202)
    def radio_job(req: RadioRequest):
        _check_radio(req)
        li = req.listener.build() if req.listener else None
        args = req.model_dump(exclude={"listener"})
        return jobs.submit("radio", lambda progress, cancel: rec.generate(**args, keep=not req.dry, listener=li,
                                                                          progress=progress, cancel=cancel))

    @app.post("/jobs/flow", status_code=202)
    def flow_job(req: FlowRequest):
        li = req.listener.build() if req.listener else None
        args = req.model_dump(exclude={"listener"})
        return jobs.submit("flow", lambda progress, cancel: rec.flow(**args, keep=not req.dry, listener=li,
                                                                     progress=progress, cancel=cancel))

    @app.post("/jobs/profile", status_code=202)
    def profile_job(listener: ListenerIn):
        li = listener.build()
        return jobs.submit("profile", lambda progress, cancel: rec.profile(li, progress=progress, cancel=cancel))

    @app.get("/jobs/{job_id}")
    def job(job_id: str):
        got = jobs.get(job_id)
        if got is None:
            raise HTTPException(404, "unknown or expired job")
        return got

    @app.delete("/jobs/{job_id}")
    def cancel_job(job_id: str):
        got = jobs.cancel(job_id)
        if got is None:
            raise HTTPException(404, "unknown or expired job")
        return got

    @app.get("/deep-cuts")
    def deep_cuts(user: str | None = None, kind: str = "exploration"):
        try:
            return rec.deep_cuts(user, kind, keep=True).to_dict()
        except PostRadioError as e:
            raise http_error(e) from e

    @app.get("/results/{result_id}")
    def result(result_id: str):
        res = rec.get(result_id)
        if res is None:
            raise HTTPException(404, "expired or unknown result")
        return res.to_dict()

    @app.post("/results/{result_id}/save")
    def save(result_id: str, req: SaveRequest):
        try:
            return {"saved_as": rec.save(result_id, req.name, req.overwrite).stem}
        except PostRadioError as e:
            raise http_error(e) from e
        except FileExistsError as e:
            raise HTTPException(409, f"{e} exists; pass overwrite=true") from e

    @app.post("/feedback")
    def import_feedback(body: FeedbackImport):
        try:
            return rec.import_feedback(body.model_dump(exclude_none=True))
        except PostRadioError as e:
            raise http_error(e) from e

    @app.get("/feedback/batches")
    def feedback_batches():
        return rec.feedback_batches()

    @app.get("/saved")
    def saved():
        return rec.list_saved()

    @app.get("/saved/{name}")
    def load_saved(name: str):
        try:
            return rec.load_saved(name)
        except PostRadioError as e:
            raise http_error(e) from e

    return app
