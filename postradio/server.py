"""Optional HTTP wrapper around api.Recommender (the CLI's `serve`). Same operations as the library."""
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .api import Recommender
from .brand import DISPLAY_NAME
from .errors import (
    ConfigError,
    DataError,
    InvalidInput,
    NotFound,
    PostRadioError,
    SourceAuthError,
    SourceUnavailable,
)

STATUS = [(NotFound, 404), (InvalidInput, 422), (SourceAuthError, 422), (SourceUnavailable, 503),
          (ConfigError, 500), (DataError, 500)]


def http_error(e: Exception) -> HTTPException:
    """One mapping from Post-Radio errors to HTTP codes, for every endpoint."""
    code = next((c for cls, c in STATUS if isinstance(e, cls)), 500)
    return HTTPException(code, str(e))


class RadioRequest(BaseModel):
    artist: str | None = None
    track: str | None = None
    artist_mbid: str | None = None
    recording_mbid: str | None = None
    genres: list[str] | None = None
    genre_op: Literal["or", "and"] = "or"
    unheard: Literal["none", "tracks", "artists"] | None = None  # None = config default
    explore: float | None = Field(None, ge=0, le=1)
    popularity: float | None = Field(None, ge=0, le=1)
    n: int | None = Field(None, ge=1, le=500)
    per_artist: int | None = Field(None, ge=1)
    rng_seed: int | None = None
    auto_popularity: bool = False  # ignore configured default popularity; follow explore
    mix: dict[str, float] | None = None  # per-run stream multipliers 0..1, e.g. {"deezer-top": 0.2}
    dry: bool = False  # preview the artist graph only; nothing is stored


class FlowRequest(BaseModel):
    """A playlist from the configured likes (Yandex playlist, Navidrome, LB loves, imported loves); no seed."""
    explore: float | None = Field(None, ge=0, le=1)
    popularity: float | None = Field(None, ge=0, le=1)
    n: int | None = Field(None, ge=1, le=500)
    per_artist: int | None = Field(None, ge=1)
    stops: int = Field(5, ge=1, le=20)
    rng_seed: int | None = None
    dry: bool = False  # preview the profile and genre path only; nothing is stored


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

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        rec.close()  # release worker threads and the cache connection with the server

    app = FastAPI(title=DISPLAY_NAME, lifespan=lifespan)

    # plain `def` handlers run in FastAPI's threadpool, so blocking provider I/O is fine
    @app.post("/radio")
    def radio(req: RadioRequest):
        seeds = (req.artist, req.track, req.artist_mbid, req.recording_mbid, req.genres or None)
        if sum(x is not None for x in seeds) != 1:
            raise HTTPException(422, "give exactly one of artist, track, artist_mbid, recording_mbid, genres")
        try:
            return rec.generate(**req.model_dump(), keep=not req.dry)
        except PostRadioError as e:
            raise http_error(e) from e

    @app.post("/flow")
    def flow(req: FlowRequest):
        try:
            return rec.flow(**req.model_dump(), keep=not req.dry)
        except PostRadioError as e:
            raise http_error(e) from e

    @app.get("/profile")
    def profile():
        try:
            return rec.profile()
        except PostRadioError as e:
            raise http_error(e) from e

    @app.get("/deep-cuts")
    def deep_cuts(user: str | None = None, kind: str = "exploration"):
        try:
            return rec.deep_cuts(user, kind, keep=True)
        except PostRadioError as e:
            raise http_error(e) from e

    @app.get("/results/{result_id}")
    def result(result_id: str):
        res = rec.get(result_id)
        if res is None:
            raise HTTPException(404, "expired or unknown result")
        return res

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
