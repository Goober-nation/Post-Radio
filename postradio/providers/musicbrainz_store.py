"""Local MusicBrainz artist store: a SQLite file built from the official MusicBrainz JSON dump (`artist.tar.xz`).
Every artist with its name, aliases, disambiguation, country and top genres (restricted to the genre graph),
plus a normalised name/alias index and a genre -> artists table. The `MusicBrainz` adapter reads it first and
falls back to the API, so name resolution and genres need no requests or rate limits when it's installed.

Part of the MusicBrainz adapter: only this module and `musicbrainz.py` see MusicBrainz payloads.
Licence: MusicBrainz genres and tags are CC BY-NC-SA 3.0 supplementary data; the store carries those terms."""
import json
import logging
import re
import sqlite3
import tarfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from pathlib import Path

_log = logging.getLogger("postradio").info  # default for log= (prose for people)

STORE_FILE = "mb_artists.sqlite"  # in storage.data_dir
INDEX_FILE = "genre_artists.json"  # in storage.data_dir: genre -> artists without the full store
TOP_GENRES = 5  # low-ranked single votes are noise (Nirvana and BTS end up in "hip hop" otherwise)
BATCH = 50_000

SCHEMA = """
CREATE TABLE artists(mbid TEXT PRIMARY KEY, name TEXT, sort_name TEXT, disambiguation TEXT, type TEXT,
                     country TEXT, genres TEXT, votes INTEGER) WITHOUT ROWID;
CREATE TABLE names(name_norm TEXT, mbid TEXT, PRIMARY KEY(name_norm, mbid)) WITHOUT ROWID;
CREATE TABLE artist_genres(genre TEXT, mbid TEXT, votes INTEGER, pos INTEGER, PRIMARY KEY(genre, mbid)) WITHOUT ROWID;
"""
_PUNCT = re.compile(r"[\W_]+")


def name_key(name: str) -> str:
    """Casefold, drop diacritics and punctuation, so "Sainté", "SAINTE" and "sainte." meet; scripts stay as-is
    (ЛСП stays лсп). All-punctuation names ("!!!") keep their symbols."""
    s = unicodedata.normalize("NFKD", name.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    out = _PUNCT.sub(" ", s).strip()
    return out or name.casefold().strip()


# ---------------------------------------------------------------- build (from the dump)
def top_genres(a: dict, graph: set[str]) -> list[tuple[str, int]]:
    """Top graph genres by votes; tags only when the artist has no graph genres, since genres are the curated
    subset. Ties break by name so builds are reproducible."""
    for field in ("genres", "tags"):
        got = [(g["name"], int(g.get("count") or 0)) for g in a.get(field) or [] if g.get("name") in graph]
        if got:
            return sorted(got, key=lambda x: (-x[1], x[0]))[:TOP_GENRES]
    return []


def artist_row(a: dict, graph: set[str]) -> tuple[tuple, list[tuple[str, int]], set[str]]:
    """One dump line -> (artists row, [(genre, votes)], normalised names incl. sort name and aliases)."""
    gs = top_genres(a, graph)
    names = {a.get("name") or "", a.get("sort-name") or ""} | {x.get("name") or "" for x in a.get("aliases") or []}
    art = (a["id"], a.get("name"), a.get("sort-name"), a.get("disambiguation") or None, a.get("type"),
           a.get("country"), json.dumps([g for g, _ in gs], ensure_ascii=False), sum(c for _, c in gs))
    return art, gs, {name_key(n) for n in names if n}


def insert(con: sqlite3.Connection, artists: Iterable[dict], graph: set[str]) -> tuple[int, int]:
    """Insert dump artists in batches; returns (artists, artists with graph genres). Malformed lines skipped."""
    n = tagged = 0
    arts, nms, ags = [], [], []

    def flush():
        con.executemany("INSERT OR REPLACE INTO artists VALUES (?,?,?,?,?,?,?,?)", arts)
        con.executemany("INSERT OR IGNORE INTO names VALUES (?,?)", nms)
        con.executemany("INSERT OR REPLACE INTO artist_genres VALUES (?,?,?,?)", ags)
        arts.clear(), nms.clear(), ags.clear()

    for a in artists:
        try:
            art, gs, names = artist_row(a, graph)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        n += 1
        tagged += bool(gs)
        arts.append(art)
        nms.extend((x, art[0]) for x in names)
        ags.extend((g, art[0], c, i) for i, (g, c) in enumerate(gs))
        if len(arts) >= BATCH:
            flush()
    flush()
    return n, tagged


def import_dump(archive: Path, out: Path, graph: set[str], log: Callable[[str], None] = _log) -> dict:
    """Stream `artist.tar.xz` (nothing extracted to disk) into a fresh store at `out`. ~3 M artists; minutes."""
    tmp = out.with_suffix(".tmp")
    tmp.unlink(missing_ok=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
    t0, n, tagged = time.time(), 0, 0
    with tarfile.open(archive, "r|xz") as tar:
        for member in tar:
            if member.name != "mbdump/artist":
                continue
            def lines(f):
                for i, line in enumerate(f, 1):
                    if i % 500_000 == 0:
                        log(f"{i:,} artists, {time.time() - t0:.0f}s")
                    try:
                        yield json.loads(line)
                    except ValueError:
                        continue

            n, tagged = insert(con, lines(tar.extractfile(member)), graph)
    con.commit()
    (names_n,) = con.execute("SELECT count(*) FROM names").fetchone()
    con.execute("VACUUM")
    con.close()
    tmp.replace(out)
    stats = {"artists": n, "with_genres": tagged, "names": names_n, "seconds": round(time.time() - t0),
             "bytes": out.stat().st_size}
    log(f"store: {n:,} artists, {tagged:,} with graph genres, {names_n:,} names, {stats['seconds']}s, "
        f"{stats['bytes'] / 1e6:.0f} MB")
    return stats


# ---------------------------------------------------------------- read
class ArtistStore:
    """Read-only view of the store. One connection shared across threads behind a lock (lookups are
    sub-millisecond, so the lock never matters)."""

    def __init__(self, path: Path | str):
        if str(path) == ":memory:":
            self.con = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            self.con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.lock = threading.Lock()

    @classmethod
    def open(cls, data_dir: Path | None) -> "ArtistStore | None":
        p = data_dir / STORE_FILE if data_dir else None
        return cls(p) if p and p.is_file() else None

    def resolve(self, name: str, limit: int = 10) -> list[dict]:
        """Candidates for a name or alias, best-tagged first (an artist with genre votes is more likely the one
        people mean), exact spelling breaking ties."""
        with self.lock:
            rows = self.con.execute(
                "SELECT a.* FROM names n JOIN artists a ON a.mbid = n.mbid WHERE n.name_norm = ? "
                "GROUP BY a.mbid ORDER BY a.votes DESC, a.name = ? DESC, a.mbid LIMIT ?",
                (name_key(name), name, limit)).fetchall()
        return [dict(r) | {"genres": json.loads(r["genres"] or "[]")} for r in rows]

    def artist(self, mbid: str) -> dict | None:
        with self.lock:
            r = self.con.execute("SELECT * FROM artists WHERE mbid = ?", (mbid,)).fetchone()
        return dict(r) | {"genres": json.loads(r["genres"] or "[]")} if r else None

    def genres(self, mbid: str) -> list[str]:
        a = self.artist(mbid)
        return a["genres"] if a else []

    def genre_artists(self, genre: str, limit: int) -> list[tuple[str, str]]:
        """(mbid, name) of the artists carrying `genre` most strongly: its votes, then its rank among the
        artist's genres, then the artist's total votes."""
        with self.lock:
            rows = self.con.execute(
                "SELECT ag.mbid, a.name FROM artist_genres ag JOIN artists a ON a.mbid = ag.mbid "
                "WHERE ag.genre = ? ORDER BY ag.votes DESC, ag.pos, a.votes DESC, ag.mbid LIMIT ?",
                (genre, limit)).fetchall()
        return [(r[0], r[1]) for r in rows]

    def genre_counts(self) -> tuple[dict[str, int], int]:
        """({genre: artists carrying it}, artists with any genre). Computed once (~1 s on the full store)."""
        if getattr(self, "_counts", None) is None:
            with self.lock:
                rows = self.con.execute("SELECT genre, COUNT(*) FROM artist_genres GROUP BY genre").fetchall()
                total = self.con.execute("SELECT COUNT(DISTINCT mbid) FROM artist_genres").fetchone()[0]
            self._counts = ({r[0]: r[1] for r in rows}, total)
        return self._counts

    def known_genres(self) -> set[str]:
        with self.lock:
            return {r[0] for r in self.con.execute("SELECT DISTINCT genre FROM artist_genres")}

    def genre_index(self, genres: Iterable[str], cap: int) -> dict[str, list[list[str]]]:
        """The small genre -> artists index (INDEX_FILE) for users without the store."""
        out = {}
        for g in sorted(genres):
            if rows := self.genre_artists(g, cap):
                out[g] = [list(r) for r in rows]
        return out

    def close(self) -> None:
        with self.lock:
            self.con.close()


class GenreIndex:
    """genre_artists.json: genre -> [[mbid, name], ...] strongest first; the store's genre table, capped."""

    def __init__(self, genres: dict[str, list[list[str]]]):
        self.genres = genres

    @classmethod
    def open(cls, data_dir: Path | None) -> "GenreIndex | None":
        p = data_dir / INDEX_FILE if data_dir else None
        if not (p and p.is_file()):
            return None
        data = json.loads(p.read_text())
        return cls(data.get("genres") or {})

    def genre_artists(self, genre: str, limit: int) -> list[tuple[str, str]]:
        return [(r[0], r[1]) for r in (self.genres.get(genre) or [])[:limit] if len(r) >= 2]

    def known_genres(self) -> set[str]:
        return set(self.genres)
