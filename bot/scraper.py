"""Parsing of nonstopkino.at pages (program, movie details, cinemas)."""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timezone

from bs4 import BeautifulSoup, FeatureNotFound, Tag

from . import config

log = logging.getLogger(__name__)

_EVENT_ID_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})t(\d{2})(\d{2})(\d{2})-(\d{3})z")
_DURATION_RE = re.compile(r"(\d+)")
_YEAR_RE = re.compile(r"(\d{4})")
_BG_IMAGE_RE = re.compile(r"background-image:\s*url\(['\"]?([^'\")]+)['\"]?\)")


def _text(el: Tag | None) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _soup(html: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:  # pragma: no cover - lxml is in requirements, fallback just in case
        return BeautifulSoup(html, "html.parser")


def movie_slug_from_url(url: str) -> str:
    m = re.search(r"/movies/([^/?#]+)/?", url)
    return m.group(1) if m else url.rstrip("/").rsplit("/", 1)[-1]


# --------------------------------------------------------------------------- program
def parse_program(html: str) -> list[dict]:
    """Return one dict per screening found on /programm/."""
    soup = _soup(html)
    out: list[dict] = []
    seen: set[str] = set()
    for art in soup.select("article.event"):
        try:
            row = _parse_event(art)
        except Exception:  # noqa: BLE001 - one broken card must not kill the refresh
            log.exception("could not parse event %s", art.get("id"))
            continue
        if row is None or row["id"] in seen:
            continue
        seen.add(row["id"])
        out.append(row)
    return out


def _parse_event(art: Tag) -> dict | None:
    link = art.select_one("a.full-card-link")
    if link is None:
        return None
    url = link.get("data-original-link") or link.get("href") or ""
    slug = movie_slug_from_url(url)
    title = _text(art.select_one("h3"))
    date_local = art.get("data-weekday") or ""
    big = _text(art.select_one(".date .big"))
    m_time = re.search(r"(\d{1,2}):(\d{2})", big)
    if not (date_local and m_time):
        return None
    time_local = f"{int(m_time.group(1)):02d}:{m_time.group(2)}"

    # The element id carries the exact UTC timestamp, use it when present.
    start_utc: datetime | None = None
    m_id = _EVENT_ID_RE.match(art.get("id") or "")
    if m_id:
        d, hh, mm, ss, _ms = m_id.groups()
        try:
            start_utc = datetime.fromisoformat(f"{d}T{hh}:{mm}:{ss}").replace(tzinfo=timezone.utc)
        except ValueError:
            start_utc = None
    if start_utc is None:
        local = datetime.fromisoformat(f"{date_local}T{time_local}:00").replace(tzinfo=config.TZ)
        start_utc = local.astimezone(timezone.utc)

    venue_slug = art.get("data-venue") or ""
    loc = art.select_one(".location")
    venue_name = ""
    if loc is not None:
        parts = [p.strip() for p in loc.get_text("\n").split("\n") if p.strip()]
        venue_name = parts[0] if parts else ""
    language = (art.get("data-language") or "").strip()
    highlights = ",".join(sorted({h.strip() for h in (art.get("data-highlight-types") or "").split(",") if h.strip()}))
    img = art.select_one(".image img")
    image_url = img.get("src") if img else None

    return {
        "id": f"{start_utc.strftime('%Y%m%dT%H%M%SZ')}|{venue_slug}|{slug}|{language}",
        "movie_slug": slug,
        "title": title,
        "start_utc": start_utc.isoformat(timespec="seconds"),
        "date_local": date_local,
        "time_local": time_local,
        "bundesland": art.get("data-location") or "",
        "venue_slug": venue_slug,
        "venue_name": venue_name,
        "language": language,
        "highlights": highlights,
        "url": url,
        "image_url": image_url,
    }


# --------------------------------------------------------------------------- movie
def parse_movie(html: str, url: str) -> dict:
    soup = _soup(html)
    data: dict = {"url": url, "title": _text(soup.select_one(".film-description h1")) or None}

    def meta(cls: str) -> str | None:
        el = soup.select_one(f".meta-info p.{cls} .value")
        return _text(el) or None

    data["director"] = meta("director")
    data["cast"] = meta("cast")
    year = meta("releaseYear")
    m = _YEAR_RE.search(year or "")
    data["year"] = int(m.group(1)) if m else None
    dur = meta("duration")
    m = _DURATION_RE.search(dur or "")
    data["duration_min"] = int(m.group(1)) if m else None
    data["spoken_language"] = meta("spokenLanguages")
    desc = soup.select_one(".film-description p.description")
    data["description"] = _text(desc) or None

    hero = soup.select_one("section.trailer-section")
    data["backdrop_url"] = None
    data["trailer_url"] = None
    if hero is not None:
        m = _BG_IMAGE_RE.search(hero.get("style") or "")
        if m:
            data["backdrop_url"] = m.group(1)
        video = hero.select_one("video")
        if video is not None and video.get("src"):
            data["trailer_url"] = video["src"]
        else:
            iframe = hero.select_one("iframe")
            if iframe is not None and iframe.get("src"):
                data["trailer_url"] = iframe["src"]
    return data


# --------------------------------------------------------------------------- cinemas
def parse_cinemas(html: str) -> list[dict]:
    soup = _soup(html)
    out = []
    for art in soup.select("article.kino"):
        cid = art.get("id")
        if not cid:
            continue
        name = _clean_name(art.select_one("h2"))
        address = _text(art.select_one("p.address")) or None
        website = None
        btn = art.select_one("a.button.website")
        if btn is not None:
            website = btn.get("href")
        img = art.select_one(".image img")
        image_url = img.get("src") if img else None
        out.append({"id": cid, "name": name, "address": address, "website": website, "image_url": image_url})
    return out


def _clean_name(el: Tag | None) -> str:
    if el is None:
        return ""
    for wbr in el.find_all("wbr"):
        wbr.unwrap()
    for br in el.find_all("br"):
        br.replace_with(" ")
    return re.sub(r"\s+", " ", el.get_text("")).strip()


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


# Venue slugs from /programm/ that cannot be matched to /kinos/ ids automatically.
VENUE_OVERRIDES: dict[str, str] = {
    "stadtkino-wien": "stadtkino",
    "metro-kinokulturhaus": "metro-kino",
    "filmmuseum": "oesterreichisches-filmmuseum",
    "de-france": "defrance",
    "urania": "urania-kino",
    "filmstudio-villach": "filmstudio-im-stadtkino-villach",
    "open-air-kino-zeughaus": "open-air-kino-im-zeughaus",
    "programmkino": "programmkino-wels",
    "bellaria-kino": "bellaria-kino-bar",
    "neues-volkskino": "neues-volkskino",
}

_STOP = {"kino", "kinos", "das", "im", "in", "der", "die", "am", "wien"}


def _tokens(s: str) -> set[str]:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return {t for t in re.split(r"[^a-z0-9]+", s) if t and t not in _STOP}


def match_venues(venues: dict[str, str], cinemas: list[dict]) -> dict[str, str | None]:
    """Map program venue slug -> cinema id from /kinos/. venues: slug -> display name."""
    by_id = {c["id"]: c for c in cinemas}
    mapping: dict[str, str | None] = {}
    for slug, name in venues.items():
        if slug in VENUE_OVERRIDES and VENUE_OVERRIDES[slug] in by_id:
            mapping[slug] = VENUE_OVERRIDES[slug]
            continue
        if slug in by_id:
            mapping[slug] = slug
            continue
        nslug, nname = normalize(slug), normalize(name)
        best, best_score = None, 0.0
        for c in cinemas:
            cid_n, cname_n = normalize(c["id"]), normalize(c["name"])
            score = 0.0
            if nname and nname == cname_n:
                score = 10
            elif nslug and (nslug == cid_n or cid_n.startswith(nslug) or nslug.startswith(cid_n)):
                score = 8
            else:
                a, b = _tokens(name) | _tokens(slug), _tokens(c["name"]) | _tokens(c["id"])
                if a and b:
                    j = len(a & b) / len(a | b)
                    if j >= 0.5:
                        score = 5 * j
            if score > best_score:
                best, best_score = c["id"], score
        mapping[slug] = best
        if best is None:
            log.warning("no cinema matched for venue %s (%s)", slug, name)
    return mapping
