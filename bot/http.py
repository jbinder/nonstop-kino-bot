"""Shared async HTTP client with a small per-host politeness delay."""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit

import httpx

from . import config

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
_host_locks: dict[str, asyncio.Lock] = {}
_host_last: dict[str, float] = {}


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers={"User-Agent": config.USER_AGENT, "Accept-Language": "de-AT,de;q=0.9,en;q=0.8"},
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            http2=False,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def get(url: str, *, delay: float | None = None, **kwargs) -> httpx.Response:
    """GET with a per-host minimum spacing between requests (default FETCH_DELAY_SECONDS)."""
    host = urlsplit(url).netloc
    lock = _host_locks.setdefault(host, asyncio.Lock())
    min_gap = config.FETCH_DELAY_SECONDS if delay is None else delay
    async with lock:
        wait = _host_last.get(host, 0.0) + min_gap - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            log.debug("GET %s", url)
            resp = await client().get(url, **kwargs)
        finally:
            _host_last[host] = time.monotonic()
    return resp
