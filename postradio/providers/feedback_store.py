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

from ..errors import InvalidInput
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
            raise InvalidInput("no opinions found: expected {'loves'|'hates': {'artists'|'albums'|'tracks': [...]}}")
        stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
        batch = {"batch": uuid.uuid4().hex[:10], "imported_at": stamp}
        lines = "".join(json.dumps({**batch, **r}, ensure_ascii=False) + "\n" for r in rows)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:  # one append per batch
                f.write(lines)
            with self.index_path.open("a", encoding="utf-8") as f:  # tiny per-batch index for batches()
                f.write(json.dumps({**batch, "items": len(rows)}) + "\n")
        counts = {f"{side}_{k}": sum(1 for r in rows if r["score"] == (1 if side == "loves" else -1) and r["kind"] == k)
                  for side in ("loves", "hates") for k in KINDS}
        return {**batch, "items": len(rows), **{k: v for k, v in counts.items() if v}}

    @staticmethod
    def _rows(data: dict):
        if not isinstance(data, dict) or not set(data) <= {"loves", "hates"}:
            raise InvalidInput("top level must be an object with 'loves' and/or 'hates'")
        for side, score in (("loves", 1), ("hates", -1)):
            section = data.get(side) or {}
            if not isinstance(section, dict) or not set(section) <= set(KINDS):
                raise InvalidInput(f"'{side}' must be an object with keys {KINDS}")
            for kind in KINDS:
                for item in section.get(kind) or []:
                    if isinstance(item, str):
                        item = {"name": item} if kind == "artists" else None
                    if not isinstance(item, dict):
                        raise InvalidInput(f"{side}.{kind}: items must be objects (artists may be plain names)")
                    artist = item.get("artist") or item.get("name")
                    title = item.get("title")
                    if not artist or (kind != "artists" and not title):
                        raise InvalidInput(f"{side}.{kind}: each item needs 'artist'/'name'" +
                                         ("" if kind == "artists" else " and 'title'"))
                    yield {"kind": kind[:-1], "artist": artist, "title": title if kind != "artists" else None,
                           "score": score, "artist_mbid": item.get("artist_mbid") or item.get("mbid"),
                           "recording_mbid": item.get("recording_mbid")}

    # ---------------------------------------------------------------- read
    @property
    def index_path(self) -> Path:
        return self.path.with_name(self.path.stem + ".batches.jsonl")

    def version(self) -> object:
        st = self.path.stat() if self.path.exists() else None
        return (st.st_size, st.st_mtime_ns) if st else None

    def _tail(self, n: int, block: int = 64 * 1024) -> list[str]:
        """Last n lines, reading backwards in blocks: cost scales with depth, not with log size."""
        if not self.path.exists() or n <= 0:
            return []
        with self.path.open("rb") as f:
            f.seek(0, 2)
            pos, buf = f.tell(), b""
            while pos > 0 and buf.count(b"\n") <= n:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + buf
        return buf.decode("utf-8", errors="replace").splitlines()[-n:]

    def _latest(self) -> list[dict]:
        newest: dict[tuple, dict] = {}
        for line in reversed(self._tail(self.depth)):
            try:
                r = json.loads(line)
            except json.JSONDecodeError:  # e.g. a partial first line from the block boundary
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
        """Every batch ever imported (oldest first) with its date and size, from the small index file."""
        if not self.index_path.exists():
            return []
        out = []
        for line in self.index_path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out
