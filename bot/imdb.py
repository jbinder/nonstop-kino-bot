"""Movie metadata: IMDb id resolution (keyless suggestion API), OMDb (optional key) and the
IMDb ratings dataset as keyless fallback for ratings. Everything is cached in the database."""
from __future__ import annotations

import asyncio
import gzip
import io
import logging
import re
import unicodedata
from urllib.parse import quote

from . import config, http
from .db import Database

log = logging.getLogger(__name__)

SUGGEST_URL = "https://v3.sg.media-imdb.com/suggestion/{first}/{query}.json"
OMDB_URL = "https://www.omdbapi.com/"
RATINGS_DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"

_PAREN_RE = re.compile(r"\(([^()]*)\)")
_NOISE_RE = re.compile(
    r"\b(70\s*mm|35\s*mm|imax|3d|director'?s cut|ov|omu|omdu|omeu|df|preview|premiere|q&a|"
    r"kino für die kleinsten|kinderkino)\b",
    re.I,
)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def title_candidates(title: str) -> list[str]:
    """Variants of a nonstopkino title to try: original title in parentheses, full, stripped."""
    cands: list[str] = []
    base = _NOISE_RE.sub(" ", title)
    for inner in _PAREN_RE.findall(base):
        if len(inner.strip()) > 1:
            cands.append(inner.strip())
    outside = _PAREN_RE.sub(" ", base)
    cands.append(outside)
    if ":" in outside:
        cands.append(outside.split(":", 1)[0])
        cands.append(outside.split(":", 1)[1])
    if " - " in outside:
        cands.append(outside.split(" - ", 1)[0])
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        c = re.sub(r"\s+", " ", c).strip(" -:–,")
        if c and _norm(c) and _norm(c) not in seen:
            seen.add(_norm(c))
            out.append(c)
    return out


_IMDB_IMG_RE = re.compile(r"\._V1_[^.]*\.(jpg|png)$")


def sized_poster(url: str | None, width: int = 800) -> str | None:
    """IMDb/Amazon image URLs are full-size originals (often >10 MB); ask for a resized copy."""
    if url and "media-amazon.com" in url:
        return _IMDB_IMG_RE.sub(f"._V1_UX{width}_.\\1", url)
    return url


async def suggest(query: str) -> list[dict]:
    q = _norm(query)[:60]
    if not q:
        return []
    first = q[0] if q[0].isalnum() else "x"
    url = SUGGEST_URL.format(first=first, query=quote(q))
    try:
        r = await http.get(url, delay=0.3)
        if r.status_code != 200:
            return []
        return r.json().get("d", []) or []
    except Exception as e:  # noqa: BLE001
        log.warning("imdb suggest failed for %r: %s", query, e)
        return []


def _score(cand: dict, variants: list[str], year: int | None, cast: str | None) -> float:
    score = 0.0
    q = cand.get("q") or ""
    if not cand.get("id", "").startswith("tt"):
        return -1
    if q in ("feature", "TV movie", "short", "TV special", "video"):
        score += 1
    elif q:
        score -= 2
    cy = cand.get("y")
    if year and cy:
        if cy == year:
            score += 3
        elif abs(cy - year) == 1:
            score += 1.5
        elif abs(cy - year) >= 3:
            score -= 3
    ctitle = _norm(cand.get("l") or "")
    if ctitle and any(ctitle == _norm(v) for v in variants):
        score += 3
    elif ctitle and any(ctitle in _norm(v) or _norm(v) in ctitle for v in variants):
        score += 1
    if cast and cand.get("s"):
        wanted = {_norm(n) for n in cast.split(",") if n.strip()}
        stars = {_norm(n) for n in cand["s"].split(",") if n.strip()}
        if wanted & stars:
            score += 3
    return score


async def resolve_imdb_id(title: str, year: int | None, cast: str | None) -> dict | None:
    """Best IMDb suggestion for a nonstopkino movie, or None."""
    variants = title_candidates(title)
    best: dict | None = None
    best_score = 0.0
    for v in variants[:3]:
        for cand in await suggest(v):
            s = _score(cand, variants, year, cast)
            if s > best_score:
                best, best_score = cand, s
        if best_score >= 6:
            break
    if best is None or best_score < 3.5:
        return None
    img = (best.get("i") or {}).get("imageUrl")
    return {
        "imdb_id": best["id"],
        "imdb_title": best.get("l"),
        "imdb_year": best.get("y"),
        "poster_url": sized_poster(img),
    }


async def omdb_by_id(imdb_id: str) -> dict | None:
    if not config.OMDB_API_KEY:
        return None
    try:
        r = await http.get(OMDB_URL, delay=0.2, params={"i": imdb_id, "apikey": config.OMDB_API_KEY, "plot": "short"})
        d = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("omdb failed for %s: %s", imdb_id, e)
        return None
    if d.get("Response") != "True":
        log.info("omdb: %s -> %s", imdb_id, d.get("Error"))
        return None

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    votes = None
    if d.get("imdbVotes") and d["imdbVotes"] != "N/A":
        votes = int(d["imdbVotes"].replace(",", ""))
    runtime = None
    m = re.search(r"(\d+)", d.get("Runtime") or "")
    if m:
        runtime = int(m.group(1))
    return {
        "rating": _f(d.get("imdbRating")) if d.get("imdbRating") != "N/A" else None,
        "votes": votes,
        "genre": d.get("Genre") if d.get("Genre") not in (None, "N/A") else None,
        "poster_url": sized_poster(d.get("Poster")) if d.get("Poster") not in (None, "N/A") else None,
        "plot": d.get("Plot") if d.get("Plot") not in (None, "N/A") else None,
        "runtime_min": runtime,
        "country": d.get("Country") if d.get("Country") not in (None, "N/A") else None,
        "source": "omdb",
    }


async def refresh_ratings_dataset(db: Database) -> bool:
    """Download the (~8 MB) IMDb ratings dataset into the ratings table."""
    try:
        r = await http.get(RATINGS_DATASET_URL, delay=0)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning("ratings dataset download failed: %s", e)
        return False

    def _rows():
        with gzip.open(io.BytesIO(r.content), "rt", encoding="utf-8") as fh:
            next(fh)  # header
            for line in fh:
                t, rating, votes = line.rstrip("\n").split("\t")
                yield t, float(rating), int(votes)

    n = await asyncio.to_thread(db.replace_ratings, _rows())
    log.info("ratings dataset imported: %d rows", n)
    return True


async def ensure_ratings_dataset(db: Database) -> None:
    if config.USE_IMDB_DATASET and not config.OMDB_API_KEY and not db.ratings_dataset_fresh():
        await refresh_ratings_dataset(db)


async def get_metadata(db: Database, slug: str, movie: dict | None) -> dict | None:
    """Return cached IMDb metadata for a movie slug, fetching/resolving it if stale."""
    if db.imdb_fresh(slug):
        row = db.get_imdb(slug)
        if not row or not row["ok"]:
            return None
        if row["rating"] is None and row["source"] in ("imdb-suggest", "omdb-miss") and config.USE_IMDB_DATASET:
            # resolved before the ratings dataset was available - fill in now
            rating = db.get_rating(row["imdb_id"])
            if rating:
                db._exec("UPDATE imdb SET rating=?, votes=? WHERE movie_slug=?", (rating[0], rating[1], slug))
                row = db.get_imdb(slug)
        return row
    if not movie or not movie.get("title"):
        return None

    data = await resolve_imdb_id(movie["title"], movie.get("year"), movie.get("cast"))
    if data is None:
        db.save_imdb(slug, {}, ok=False)
        return None

    extra = await omdb_by_id(data["imdb_id"])
    if extra:
        if extra.get("poster_url"):
            data["poster_url"] = extra["poster_url"]
        data.update({k: v for k, v in extra.items() if k != "poster_url"})
    else:
        # "omdb-miss" = key configured but OMDb has nothing for this id (don't retry every time)
        data["source"] = "omdb-miss" if config.OMDB_API_KEY else "imdb-suggest"
        rating = db.get_rating(data["imdb_id"]) if config.USE_IMDB_DATASET else None
        if rating:
            data["rating"], data["votes"] = rating
            if data["source"] == "imdb-suggest":
                data["source"] = "imdb-dataset"
    db.save_imdb(slug, data, ok=True)
    return db.get_imdb(slug)


def refresh_dataset_ratings(db: Database) -> int:
    """After a dataset refresh, update ratings of cached entries that came from the dataset."""
    n = 0
    for row in db._rows("SELECT movie_slug, imdb_id FROM imdb WHERE ok=1 AND source IN ('imdb-suggest','imdb-dataset')"):
        rating = db.get_rating(row["imdb_id"])
        if rating:
            db._exec(
                "UPDATE imdb SET rating=?, votes=?, source='imdb-dataset' WHERE movie_slug=?",
                (rating[0], rating[1], row["movie_slug"]),
            )
            n += 1
    return n
