"""Local loves/hates store: user-supplied JSON batches appended to a JSONL log, each stamped with its
import date. Nothing is rewritten or deleted; per item the newest opinion wins, and only the newest
`depth` opinions are read.

Import format (every key optional; an item is a name string or an object):
{
  "loves": {"artists": ["Radiohead", {"name": "Pixies", "mbid": "..."}],
            "albums":  [{"artist": "Blur", "title": "13"}],
            "tracks":  [{"artist": "Muse", "title": "Hysteria", "recording_mbid": "..."}]},
  "hates": {...same shape...}
}
"""
import json
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

from ..models import Feedback, norm
from . import FeedbackProvider

KINDS = ("artists", "albums", "tracks")
ALBUM_WEIGHT = 0.5  # an album opinion is a half-strength opinion about its artist (same as Navidrome)


class JsonFeedbackStore(FeedbackProvider):
    name = "json"
    _lock = threading.Lock()

    def __init__(self, path: str | Path, depth: int = 5000):
        self.path, self.depth = Path(path), depth

    # ---------------------------------------------------------------- write
    def import_batch(self, data: dict, now: datetime | None = None) -> dict:
        rows = list(self._rows(data))
        if not rows:
            raise ValueError("no opinions found: expected {'loves'|'hates': {'artists'|'albums'|'tracks': [...]}}")
        stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
        batch = {"batch": uuid.uuid4().hex[:10], "imported_at": stamp}
        lines = "".join(json.dumps({**batch, **r}, ensure_ascii=False) + "\n" for r in rows)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as f:  # one append per batch
            f.write(lines)
        counts = {f"{side}_{k}": sum(1 for r in rows if r["score"] == (1 if side == "loves" else -1) and r["kind"] == k)
                  for side in ("loves", "hates") for k in KINDS}
        return {**batch, "items": len(rows), **{k: v for k, v in counts.items() if v}}

    @staticmethod
    def _rows(data: dict):
        if not isinstance(data, dict) or not set(data) <= {"loves", "hates"}:
            raise ValueError("top level must be an object with 'loves' and/or 'hates'")
        for side, score in (("loves", 1), ("hates", -1)):
            section = data.get(side) or {}
            if not isinstance(section, dict) or not set(section) <= set(KINDS):
                raise ValueError(f"'{side}' must be an object with keys {KINDS}")
            for kind in KINDS:
                for item in section.get(kind) or []:
                    if isinstance(item, str):
                        item = {"name": item} if kind == "artists" else None
                    if not isinstance(item, dict):
                        raise ValueError(f"{side}.{kind}: items must be objects (artists may be plain names)")
                    artist = item.get("artist") or item.get("name")
                    title = item.get("title")
                    if not artist or (kind != "artists" and not title):
                        raise ValueError(f"{side}.{kind}: each item needs 'artist'/'name'" +
                                         ("" if kind == "artists" else " and 'title'"))
                    yield {"kind": kind[:-1], "artist": artist, "title": title if kind != "artists" else None,
                           "score": score, "artist_mbid": item.get("artist_mbid") or item.get("mbid"),
                           "recording_mbid": item.get("recording_mbid")}

    # ---------------------------------------------------------------- read
    def _latest(self) -> list[dict]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        newest: dict[tuple, dict] = {}
        for line in reversed(lines[-self.depth:]):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            newest.setdefault((r["kind"], norm(r["artist"]), norm(r.get("title") or "")), r)
        return list(newest.values())

    def load(self) -> list[Feedback]:
        out = []
        for r in self._latest():
            if r["kind"] == "track":
                out.append(Feedback(r["artist"], r["title"], float(r["score"]), self.name,
                                    artist_mbid=r.get("artist_mbid"), recording_mbid=r.get("recording_mbid")))
            else:
                w = ALBUM_WEIGHT if r["kind"] == "album" else 1.0
                out.append(Feedback(r["artist"], None, w * r["score"], self.name, artist_mbid=r.get("artist_mbid")))
        return out

    def batches(self) -> list[dict]:
        """Every batch ever imported (oldest first) with its date and size."""
        if not self.path.exists():
            return []
        seen: dict[str, dict] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            b = seen.setdefault(r["batch"], {"batch": r["batch"], "imported_at": r["imported_at"], "items": 0})
            b["items"] += 1
        return list(seen.values())
