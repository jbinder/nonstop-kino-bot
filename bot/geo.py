"""Geocoding via Nominatim (cached) and distance calculation."""
from __future__ import annotations

import logging
import math
import re

from . import http
from .db import Database

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"


async def geocode(db: Database, query: str, country_bias: str = "at") -> tuple[float, float, str] | None:
    """Return (lat, lon, label) for an address, using the persistent cache first."""
    key = " ".join(query.split()).lower()
    cached = db.get_geocache(key)
    if cached:
        if cached["lat"] is None:
            return None
        return cached["lat"], cached["lon"], cached["label"] or query
    try:
        # Nominatim usage policy: max 1 req/s, identifiable User-Agent.
        r = await http.get(
            NOMINATIM_URL,
            delay=1.1,
            params={"format": "jsonv2", "limit": 1, "q": query, "countrycodes": country_bias, "addressdetails": 0},
        )
        r.raise_for_status()
        results = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("geocoding failed for %r: %s", query, e)
        return None  # not cached: try again next time
    if not results:
        # Retry without the country bias (e.g. "Bratislava") before giving up.
        if country_bias:
            res = await geocode(db, query, country_bias="")
            if res:
                db.save_geocache(key, res[0], res[1], res[2])
            else:
                db.save_geocache(key, None, None, None)
            return res
        db.save_geocache(key, None, None, None)
        return None
    hit = results[0]
    lat, lon = float(hit["lat"]), float(hit["lon"])
    label = hit.get("display_name") or query
    db.save_geocache(key, lat, lon, label)
    return lat, lon, label


_POSTAL_RE = re.compile(r"\b(\d{4})\s+([A-Za-zÄÖÜäöüß .-]+?)\s*$")


def address_candidates(name: str | None, address: str) -> list[str]:
    """Progressively simpler queries for a cinema address, most specific first."""
    cands = [address]
    cleaned = re.sub(r"\([^)]*\)", " ", address)          # "(Eingang Domgasse)"
    cleaned = re.sub(r"^[^,-]*\s-\s", "", cleaned)         # "Open Air Augarten - Obere ..."
    cleaned = re.sub(r"\s*/\s*[^,]+", "", cleaned)         # "Stadtpark 1 / Schillerstraße 3"
    cleaned = re.sub(r"\s+", " ", cleaned).replace(" ,", ",").strip(" ,")
    cands.append(cleaned)
    parts = [p.strip() for p in cleaned.split(",") if p.strip()]
    m = _POSTAL_RE.search(cleaned)
    city = f"{m.group(1)} {m.group(2)}" if m else (parts[-1] if len(parts) > 1 else "")
    if len(parts) > 2 and city:
        # "Schottenring 5, Heßgasse 7, 1010 Wien" -> first street only; "Kino X, Salzgasse 25" -> last street
        cands.append(f"{parts[0]}, {city}")
        cands.append(f"{parts[-2]}, {city}")
    if name and city:
        cands.append(f"{name}, {city}")
    if name:
        cands.append(name)
    if city:
        cands.append(city)  # approximate fallback: town centre
    out, seen = [], set()
    for c in cands:
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


async def geocode_address(db: Database, name: str | None, address: str) -> tuple[float, float, str] | None:
    for q in address_candidates(name, address):
        res = await geocode(db, q)
        if res:
            if q != address:
                log.info("geocoded %r via fallback %r", address, q)
            return res
    log.warning("could not geocode %r", address)
    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def format_km(km: float | None) -> str:
    if km is None:
        return ""
    if km < 1:
        return f"{int(round(km * 1000, -1))} m"
    if km < 10:
        return f"{km:.1f} km"
    return f"{km:.0f} km"


def maps_link(lat: float | None, lon: float | None, address: str | None) -> str | None:
    if lat is not None and lon is not None:
        return f"https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}"
    if address:
        from urllib.parse import quote_plus

        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"
    return None
