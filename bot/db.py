"""SQLite persistence for users and all cached data (lives in the mounted data dir)."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id       INTEGER PRIMARY KEY,
    bundesland    TEXT,
    languages     TEXT NOT NULL DEFAULT '[]',
    notify_time   TEXT NOT NULL DEFAULT '08:00',
    notify_enabled INTEGER NOT NULL DEFAULT 1,
    home_lat      REAL,
    home_lon      REAL,
    home_label    TEXT,
    setup_done    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS screenings (
    id          TEXT PRIMARY KEY,
    movie_slug  TEXT NOT NULL,
    title       TEXT NOT NULL,
    start_utc   TEXT NOT NULL,
    date_local  TEXT NOT NULL,
    time_local  TEXT NOT NULL,
    bundesland  TEXT NOT NULL,
    venue_slug  TEXT NOT NULL,
    venue_name  TEXT NOT NULL,
    language    TEXT NOT NULL,
    highlights  TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL,
    image_url   TEXT
);
CREATE INDEX IF NOT EXISTS idx_screenings_date ON screenings(date_local, bundesland);
CREATE INDEX IF NOT EXISTS idx_screenings_movie ON screenings(movie_slug);
CREATE TABLE IF NOT EXISTS movies (
    slug            TEXT PRIMARY KEY,
    title           TEXT,
    director        TEXT,
    "cast"          TEXT,
    year            INTEGER,
    duration_min    INTEGER,
    spoken_language TEXT,
    description     TEXT,
    backdrop_url    TEXT,
    trailer_url     TEXT,
    url             TEXT,
    fetched_at      TEXT NOT NULL,
    ok              INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cinemas (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    address     TEXT,
    website     TEXT,
    image_url   TEXT,
    lat         REAL,
    lon         REAL,
    geocoded_at TEXT,
    fetched_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS venue_map (
    venue_slug TEXT PRIMARY KEY,
    cinema_id  TEXT
);
CREATE TABLE IF NOT EXISTS imdb (
    movie_slug  TEXT PRIMARY KEY,
    imdb_id     TEXT,
    imdb_title  TEXT,
    imdb_year   INTEGER,
    rating      REAL,
    votes       INTEGER,
    genre       TEXT,
    poster_url  TEXT,
    plot        TEXT,
    runtime_min INTEGER,
    source      TEXT,
    fetched_at  TEXT NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS images (
    url              TEXT PRIMARY KEY,
    path             TEXT,
    telegram_file_id TEXT,
    fetched_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS geocache (
    query      TEXT PRIMARY KEY,
    lat        REAL,
    lon        REAL,
    label      TEXT,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ratings (
    tconst TEXT PRIMARY KEY,
    rating REAL NOT NULL,
    votes  INTEGER NOT NULL
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_fresh(fetched_at: str | None, ttl: timedelta) -> bool:
    dt = parse_iso(fetched_at)
    return dt is not None and utcnow() - dt < ttl


class Database:
    def __init__(self, path=config.DB_PATH):
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(imdb)")}
        if "country" not in cols:
            self._conn.execute("ALTER TABLE imdb ADD COLUMN country TEXT")
            # let cached OMDb entries be refreshed lazily so they pick up the country
            self._conn.execute("UPDATE imdb SET fetched_at='2000-01-01T00:00:00+00:00' WHERE source='omdb'")

    # ---- generic helpers -------------------------------------------------
    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            return [dict(r) for r in cur.fetchall()]

    def _row(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))

    # ---- meta -------------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        r = self._row("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else None

    def set_meta(self, key: str, value: str | None) -> None:
        self._exec(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # ---- users ------------------------------------------------------------
    def get_user(self, chat_id: int) -> dict | None:
        r = self._row("SELECT * FROM users WHERE chat_id=?", (chat_id,))
        if r:
            r["languages"] = json.loads(r["languages"] or "[]")
        return r

    def ensure_user(self, chat_id: int) -> dict:
        u = self.get_user(chat_id)
        if u:
            return u
        now = iso(utcnow())
        self._exec(
            "INSERT INTO users(chat_id, notify_time, created_at, updated_at) VALUES(?,?,?,?)",
            (chat_id, config.DEFAULT_NOTIFY_TIME, now, now),
        )
        return self.get_user(chat_id)  # type: ignore[return-value]

    def update_user(self, chat_id: int, **fields: Any) -> dict:
        self.ensure_user(chat_id)
        if "languages" in fields:
            fields["languages"] = json.dumps(list(fields["languages"]))
        fields["updated_at"] = iso(utcnow())
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE users SET {cols} WHERE chat_id=?", (*fields.values(), chat_id))
        return self.get_user(chat_id)  # type: ignore[return-value]

    def delete_user(self, chat_id: int) -> None:
        self._exec("DELETE FROM users WHERE chat_id=?", (chat_id,))

    def all_users(self) -> list[dict]:
        users = self._rows("SELECT * FROM users")
        for u in users:
            u["languages"] = json.loads(u["languages"] or "[]")
        return users

    # ---- screenings -------------------------------------------------------
    def replace_screenings(self, rows: list[dict]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM screenings")
                self._conn.executemany(
                    """INSERT OR REPLACE INTO screenings
                       (id, movie_slug, title, start_utc, date_local, time_local, bundesland,
                        venue_slug, venue_name, language, highlights, url, image_url)
                       VALUES (:id,:movie_slug,:title,:start_utc,:date_local,:time_local,:bundesland,
                               :venue_slug,:venue_name,:language,:highlights,:url,:image_url)""",
                    rows,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self.set_meta("program_fetched_at", iso(utcnow()))

    def program_fresh(self) -> bool:
        return is_fresh(self.get_meta("program_fetched_at"), timedelta(hours=config.PROGRAM_TTL_HOURS))

    def program_age(self) -> timedelta | None:
        dt = parse_iso(self.get_meta("program_fetched_at"))
        return utcnow() - dt if dt else None

    def screenings_for_day(self, date_local: str, bundesland: str | None, languages: list[str]) -> list[dict]:
        sql = "SELECT * FROM screenings WHERE date_local=?"
        params: list[Any] = [date_local]
        if bundesland:
            sql += " AND bundesland=?"
            params.append(bundesland)
        if languages:
            sql += f" AND language IN ({','.join('?' * len(languages))})"
            params.extend(languages)
        sql += " ORDER BY start_utc, title"
        return self._rows(sql, params)

    def screenings_for_movie(self, slug: str, from_date_local: str) -> list[dict]:
        return self._rows(
            "SELECT * FROM screenings WHERE movie_slug=? AND date_local>=? ORDER BY start_utc",
            (slug, from_date_local),
        )

    def upcoming_movie_slugs(self, until_date_local: str) -> list[str]:
        rows = self._rows(
            "SELECT DISTINCT movie_slug FROM screenings WHERE date_local<=? ORDER BY date_local",
            (until_date_local,),
        )
        return [r["movie_slug"] for r in rows]

    def screening_count(self) -> int:
        r = self._row("SELECT COUNT(*) AS n FROM screenings")
        return r["n"] if r else 0

    # ---- movies -----------------------------------------------------------
    def get_movie(self, slug: str) -> dict | None:
        return self._row("SELECT * FROM movies WHERE slug=?", (slug,))

    def movie_fresh(self, slug: str) -> bool:
        m = self.get_movie(slug)
        if not m:
            return False
        ttl = timedelta(days=config.MOVIE_TTL_DAYS) if m["ok"] else timedelta(days=1)
        return is_fresh(m["fetched_at"], ttl)

    def save_movie(self, slug: str, data: dict, ok: bool = True) -> None:
        self._exec(
            """INSERT OR REPLACE INTO movies
               (slug,title,director,"cast",year,duration_min,spoken_language,description,
                backdrop_url,trailer_url,url,fetched_at,ok)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                slug, data.get("title"), data.get("director"), data.get("cast"), data.get("year"),
                data.get("duration_min"), data.get("spoken_language"), data.get("description"),
                data.get("backdrop_url"), data.get("trailer_url"), data.get("url"),
                iso(utcnow()), 1 if ok else 0,
            ),
        )

    # ---- cinemas ----------------------------------------------------------
    def cinemas_fresh(self) -> bool:
        return is_fresh(self.get_meta("cinemas_fetched_at"), timedelta(days=config.CINEMA_TTL_DAYS))

    def all_cinemas(self) -> list[dict]:
        return self._rows("SELECT * FROM cinemas")

    def get_cinema(self, cinema_id: str) -> dict | None:
        return self._row("SELECT * FROM cinemas WHERE id=?", (cinema_id,))

    def save_cinemas(self, rows: list[dict]) -> None:
        now = iso(utcnow())
        with self._lock:
            old_addr = {c["id"]: c["address"] for c in self.all_cinemas()}
            for c in rows:
                self._conn.execute(
                    """INSERT INTO cinemas(id,name,address,website,image_url,fetched_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET name=excluded.name, address=excluded.address,
                         website=excluded.website, image_url=excluded.image_url, fetched_at=excluded.fetched_at""",
                    (c["id"], c["name"], c.get("address"), c.get("website"), c.get("image_url"), now),
                )
                # a changed address must be geocoded again
                if c["id"] in old_addr and old_addr[c["id"]] != c.get("address"):
                    self._conn.execute(
                        "UPDATE cinemas SET lat=NULL, lon=NULL, geocoded_at=NULL WHERE id=?", (c["id"],)
                    )
        self.set_meta("cinemas_fetched_at", now)

    def set_cinema_coords(self, cinema_id: str, lat: float | None, lon: float | None) -> None:
        self._exec(
            "UPDATE cinemas SET lat=?, lon=?, geocoded_at=? WHERE id=?",
            (lat, lon, iso(utcnow()), cinema_id),
        )

    def get_venue_map(self) -> dict[str, str]:
        return {r["venue_slug"]: r["cinema_id"] for r in self._rows("SELECT * FROM venue_map")}

    def set_venue_map(self, mapping: dict[str, str | None]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO venue_map(venue_slug,cinema_id) VALUES(?,?)", list(mapping.items())
            )

    def cinema_for_venue(self, venue_slug: str) -> dict | None:
        return self._row(
            "SELECT c.* FROM venue_map v JOIN cinemas c ON c.id=v.cinema_id WHERE v.venue_slug=?",
            (venue_slug,),
        )

    # ---- imdb -------------------------------------------------------------
    def get_imdb(self, slug: str) -> dict | None:
        return self._row("SELECT * FROM imdb WHERE movie_slug=?", (slug,))

    def imdb_fresh(self, slug: str) -> bool:
        r = self.get_imdb(slug)
        if not r:
            return False
        ttl = timedelta(days=config.IMDB_TTL_DAYS if r["ok"] else config.IMDB_FAIL_TTL_DAYS)
        if r["ok"] and config.OMDB_API_KEY and r["source"] in ("imdb-suggest", "imdb-dataset"):
            return False  # an OMDb key was added later: upgrade the entry (genre, country, poster)
        return is_fresh(r["fetched_at"], ttl)

    def save_imdb(self, slug: str, data: dict, ok: bool) -> None:
        self._exec(
            """INSERT OR REPLACE INTO imdb
               (movie_slug,imdb_id,imdb_title,imdb_year,rating,votes,genre,poster_url,plot,runtime_min,source,fetched_at,ok,country)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                slug, data.get("imdb_id"), data.get("imdb_title"), data.get("imdb_year"), data.get("rating"),
                data.get("votes"), data.get("genre"), data.get("poster_url"), data.get("plot"),
                data.get("runtime_min"), data.get("source"), iso(utcnow()), 1 if ok else 0, data.get("country"),
            ),
        )

    # ---- ratings dataset --------------------------------------------------
    def ratings_dataset_fresh(self) -> bool:
        return is_fresh(self.get_meta("ratings_fetched_at"), timedelta(days=config.RATINGS_DATASET_TTL_DAYS))

    def replace_ratings(self, rows: Iterable[tuple[str, float, int]]) -> int:
        n = 0
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.execute("DELETE FROM ratings")
                batch: list[tuple[str, float, int]] = []
                for row in rows:
                    batch.append(row)
                    if len(batch) >= 20000:
                        self._conn.executemany("INSERT INTO ratings VALUES(?,?,?)", batch)
                        n += len(batch)
                        batch.clear()
                if batch:
                    self._conn.executemany("INSERT INTO ratings VALUES(?,?,?)", batch)
                    n += len(batch)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self.set_meta("ratings_fetched_at", iso(utcnow()))
        return n

    def get_rating(self, tconst: str) -> tuple[float, int] | None:
        r = self._row("SELECT rating, votes FROM ratings WHERE tconst=?", (tconst,))
        return (r["rating"], r["votes"]) if r else None

    def ratings_count(self) -> int:
        r = self._row("SELECT COUNT(*) AS n FROM ratings")
        return r["n"] if r else 0

    # ---- images -----------------------------------------------------------
    def get_image(self, url: str) -> dict | None:
        return self._row("SELECT * FROM images WHERE url=?", (url,))

    def save_image(self, url: str, path: str | None, file_id: str | None = None) -> None:
        self._exec(
            """INSERT INTO images(url,path,telegram_file_id,fetched_at) VALUES(?,?,?,?)
               ON CONFLICT(url) DO UPDATE SET path=excluded.path,
                 telegram_file_id=COALESCE(excluded.telegram_file_id, images.telegram_file_id),
                 fetched_at=excluded.fetched_at""",
            (url, path, file_id, iso(utcnow())),
        )

    def set_image_file_id(self, url: str, file_id: str) -> None:
        self._exec("UPDATE images SET telegram_file_id=? WHERE url=?", (file_id, url))

    def clear_image_file_id(self, url: str) -> None:
        self._exec("UPDATE images SET telegram_file_id=NULL WHERE url=?", (url,))

    # ---- geocache ---------------------------------------------------------
    def get_geocache(self, query: str) -> dict | None:
        return self._row("SELECT * FROM geocache WHERE query=?", (query,))

    def save_geocache(self, query: str, lat: float | None, lon: float | None, label: str | None) -> None:
        self._exec(
            "INSERT OR REPLACE INTO geocache(query,lat,lon,label,fetched_at) VALUES(?,?,?,?,?)",
            (query, lat, lon, label, iso(utcnow())),
        )
