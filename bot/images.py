"""Poster / backdrop cache on disk plus Telegram file_id reuse."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from . import config, http
from .db import Database

log = logging.getLogger(__name__)

_EXT_BY_TYPE = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _path_for(url: str, content_type: str | None) -> Path:
    h = hashlib.sha1(url.encode()).hexdigest()[:20]
    ext = _EXT_BY_TYPE.get((content_type or "").split(";")[0].strip(), ".jpg")
    return config.IMAGE_DIR / f"{h}{ext}"


async def get_image_path(db: Database, url: str) -> Path | None:
    """Local path to the image at url, downloading it once."""
    row = db.get_image(url)
    if row and row["path"] and Path(row["path"]).is_file():
        return Path(row["path"])
    config.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        r = await http.get(url, delay=0.2)
        r.raise_for_status()
        if not r.headers.get("content-type", "").startswith("image/"):
            raise ValueError(f"not an image: {r.headers.get('content-type')}")
        if len(r.content) > 9_500_000:  # Telegram photo upload limit is 10 MB
            raise ValueError("image too large")
    except Exception as e:  # noqa: BLE001
        log.warning("image download failed %s: %s", url, e)
        return None
    path = _path_for(url, r.headers.get("content-type"))
    path.write_bytes(r.content)
    db.save_image(url, str(path), row["telegram_file_id"] if row else None)
    return path


def get_file_id(db: Database, url: str) -> str | None:
    row = db.get_image(url)
    return row["telegram_file_id"] if row else None


def remember_file_id(db: Database, url: str, file_id: str) -> None:
    if db.get_image(url) is None:
        db.save_image(url, None, file_id)
    else:
        db.set_image_file_id(url, file_id)
