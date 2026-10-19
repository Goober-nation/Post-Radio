"""Optional HTTP wrapper around pvrecs.api.Recommender (`pvrecs serve`). Same operations as the library."""
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .api import Recommender


class RadioRequest(BaseModel):
    artist: str | None = None
    track: str | None = None
    artist_mbid: str | None = None
    recording_mbid: str | None = None
    genres: list[str] | None = None
    genre_op: Literal["or", "and"] = "or"
    unheard: Literal["tracks", "artists"] | None = None
    explore: float | None = Field(None, ge=0, le=1)
    popularity: float | None = Field(None, ge=0, le=1)
    n: int | None = Field(None, ge=1, le=500)
    per_artist: int | None = Field(None, ge=1)
    rng_seed: int | None = None
    auto_popularity: bool = False  # ignore configured default popularity; follow explore


class SaveRequest(BaseModel):
    name: str | None = None
    overwrite: bool = False


def create_app(rec: Recommender | None = None) -> FastAPI:
    rec = rec or Recommender()
    app = FastAPI(title="pvrecs")

    # plain `def` handlers run in FastAPI's threadpool, so blocking provider I/O is fine
    @app.post("/radio")
    def radio(req: RadioRequest):
        seeds = (req.artist, req.track, req.artist_mbid, req.recording_mbid, req.genres or None)
        if sum(x is not None for x in seeds) != 1:
            raise HTTPException(422, "give exactly one of artist, track, artist_mbid, recording_mbid, genres")
        try:
            return rec.generate(**req.model_dump())
        except ValueError as e:
            raise HTTPException(404, str(e)) from e

    @app.get("/deep-cuts")
    def deep_cuts(user: str | None = None, kind: str = "exploration"):
        try:
            return rec.deep_cuts(user, kind)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

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
        except KeyError as e:
            raise HTTPException(404, str(e)) from e
        except FileExistsError as e:
            raise HTTPException(409, f"{e} exists; pass overwrite=true") from e

    @app.post("/feedback")
    def import_feedback(body: dict):
        try:
            return rec.import_feedback(body)
        except ValueError as e:
            raise HTTPException(422, str(e)) from e

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
        except FileNotFoundError as e:
            raise HTTPException(404, "no such saved playlist") from e

    return app
