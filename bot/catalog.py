"""Service layer: keeps the local cache up to date and answers program queries."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from . import config, geo, http, imdb, scraper
from .db import Database, is_fresh

log = logging.getLogger(__name__)


class Catalog:
    def __init__(self, db: Database):
        self.db = db
        self._program_lock = asyncio.Lock()
        self._cinema_lock = asyncio.Lock()
        self._movie_locks: dict[str, asyncio.Lock] = {}
        self._prefetch_task: asyncio.Task | None = None
        self._bg_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ program
    async def ensure_program(self, force: bool = False) -> bool:
        """Refresh the screening list if stale. Returns True when data is available."""
        if not force and self.db.program_fresh():
            return True
        async with self._program_lock:
            if not force and self.db.program_fresh():
                return True
            try:
                r = await http.get(config.PROGRAM_URL, delay=0)
                r.raise_for_status()
                rows = scraper.parse_program(r.text)
            except Exception as e:  # noqa: BLE001
                log.warning("program refresh failed: %s", e)
                return self.db.screening_count() > 0
            if not rows:
                log.warning("program page parsed to 0 screenings, keeping old data")
                return self.db.screening_count() > 0
            self.db.replace_screenings(rows)
            log.info("program refreshed: %d screenings", len(rows))
            # cinema mapping/geocoding and detail prefetch run in the background
            self._bg(self.ensure_cinemas(venues={r["venue_slug"]: r["venue_name"] for r in rows}))
            self.start_prefetch()
            return True

    def _bg(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    # ------------------------------------------------------------------ cinemas
    async def ensure_cinemas(self, venues: dict[str, str] | None = None, force: bool = False) -> None:
        async with self._cinema_lock:
            if force or not self.db.cinemas_fresh():
                try:
                    r = await http.get(config.CINEMAS_URL, delay=0)
                    r.raise_for_status()
                    cinemas = scraper.parse_cinemas(r.text)
                    if cinemas:
                        self.db.save_cinemas(cinemas)
                        log.info("cinemas refreshed: %d", len(cinemas))
                except Exception as e:  # noqa: BLE001
                    log.warning("cinema refresh failed: %s", e)
            cinemas = self.db.all_cinemas()
            if venues is None:
                venues = {
                    r["venue_slug"]: r["venue_name"]
                    for r in self.db._rows("SELECT DISTINCT venue_slug, venue_name FROM screenings")
                }
            known = self.db.get_venue_map()
            missing = {k: v for k, v in venues.items() if not known.get(k)}
            if missing and cinemas:
                self.db.set_venue_map(scraper.match_venues(missing, cinemas))
            # geocode cinemas that have an address but no coordinates yet (retry failures monthly)
            for c in cinemas:
                if c["address"] and c["lat"] is None and not is_fresh(c["geocoded_at"], timedelta(days=30)):
                    res = await geo.geocode_address(self.db, c["name"], c["address"])
                    self.db.set_cinema_coords(c["id"], res[0] if res else None, res[1] if res else None)

    # ------------------------------------------------------------------ movies
    def _lock_for(self, slug: str) -> asyncio.Lock:
        return self._movie_locks.setdefault(slug, asyncio.Lock())

    async def ensure_movie(self, slug: str, url: str | None = None) -> dict | None:
        if self.db.movie_fresh(slug):
            return self.db.get_movie(slug)
        async with self._lock_for(slug):
            if self.db.movie_fresh(slug):
                return self.db.get_movie(slug)
            url = url or f"{config.BASE_URL}/movies/{slug}/"
            try:
                r = await http.get(url)
                r.raise_for_status()
                data = scraper.parse_movie(r.text, url)
                self.db.save_movie(slug, data, ok=bool(data.get("title")))
            except Exception as e:  # noqa: BLE001
                log.warning("movie fetch failed %s: %s", slug, e)
                old = self.db.get_movie(slug)
                if old:
                    return old
                self.db.save_movie(slug, {"url": url}, ok=False)
            return self.db.get_movie(slug)

    async def ensure_metadata(self, slug: str) -> dict | None:
        movie = await self.ensure_movie(slug)
        return await imdb.get_metadata(self.db, slug, movie)

    async def enrich(self, slugs: list[str]) -> tuple[dict[str, dict], dict[str, dict]]:
        """Fetch movie details + IMDb metadata for the given slugs (mostly cache hits)."""
        movies: dict[str, dict] = {}
        meta: dict[str, dict] = {}
        for slug in slugs:
            m = await self.ensure_movie(slug)
            if m:
                movies[slug] = m
            md = await imdb.get_metadata(self.db, slug, m)
            if md:
                meta[slug] = md
        return movies, meta

    # ------------------------------------------------------------------ prefetch
    def start_prefetch(self) -> None:
        if self._prefetch_task is None or self._prefetch_task.done():
            self._prefetch_task = asyncio.create_task(self._prefetch())

    async def _prefetch(self) -> None:
        """Warm the cache for all movies playing in the next PREFETCH_DAYS days."""
        try:
            await imdb.ensure_ratings_dataset(self.db)
            until = (datetime.now(config.TZ).date() + timedelta(days=config.PREFETCH_DAYS)).isoformat()
            slugs = self.db.upcoming_movie_slugs(until)
            todo = [s for s in slugs if not (self.db.movie_fresh(s) and self.db.imdb_fresh(s))]
            if not todo:
                return
            log.info("prefetching %d movies", len(todo))
            for slug in todo:
                await self.ensure_metadata(slug)
            log.info("prefetch done")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("prefetch failed")

    # ------------------------------------------------------------------ queries
    def screenings(self, user: dict, day: date, not_before: datetime | None = None,
                   not_after: datetime | None = None, from_hour: int | None = None) -> list[dict]:
        rows = self.db.screenings_for_day(day.isoformat(), user.get("bundesland"), user.get("languages") or [])
        out = []
        for r in rows:
            start = datetime.fromisoformat(r["start_utc"]).astimezone(config.TZ)
            if not_before and start < not_before:
                continue
            if not_after and start > not_after:
                continue
            if from_hour is not None and start.hour < from_hour:
                continue
            r["start_local"] = start
            out.append(r)
        return out

    def cinema_info(self, venue_slug: str, user: dict | None = None) -> dict:
        c = self.db.cinema_for_venue(venue_slug) or {}
        info = {
            "name": c.get("name"),
            "address": c.get("address"),
            "website": c.get("website"),
            "lat": c.get("lat"),
            "lon": c.get("lon"),
            "distance_km": None,
        }
        if user and user.get("home_lat") is not None and c.get("lat") is not None:
            info["distance_km"] = geo.haversine_km(user["home_lat"], user["home_lon"], c["lat"], c["lon"])
        return info
