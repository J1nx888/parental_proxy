#!/usr/bin/env python3
"""Crunchyroll object-id -> parent series-id resolution, cached in SQLite.

Same idea as v1's cr_cache.py, but backed by the shared database instead of
a JSON file, since multiple proxy helper processes need consistent access.
"""
from __future__ import annotations

import sqlite3
import time

import cr_api

POSITIVE_TTL_SECONDS = 30 * 24 * 60 * 60
NEGATIVE_TTL_SECONDS = 60 * 60
_MISSING = "__missing__"

_resolver: cr_api.SeriesResolver | None = None


def _get_resolver() -> cr_api.SeriesResolver:
    global _resolver
    if _resolver is None:
        _resolver = cr_api.SeriesResolver()
    return _resolver


def _cache_get(conn: sqlite3.Connection, object_id: str) -> tuple[bool, str | None]:
    """Return (hit, series_id). A negative cache hit returns (True, None)."""
    object_id = object_id.upper()
    row = conn.execute(
        "SELECT series_id, expires_at FROM series_cache WHERE object_id = ?",
        (object_id,),
    ).fetchone()
    if row is None:
        return False, None
    if time.time() >= row["expires_at"]:
        conn.execute("DELETE FROM series_cache WHERE object_id = ?", (object_id,))
        return False, None
    series_id = row["series_id"]
    if series_id == _MISSING:
        return True, None
    return True, series_id


def _cache_put(conn: sqlite3.Connection, object_id: str, series_id: str | None) -> None:
    object_id = object_id.upper()
    series_id = series_id.upper() if isinstance(series_id, str) else None
    ttl = POSITIVE_TTL_SECONDS if series_id is not None else NEGATIVE_TTL_SECONDS
    conn.execute(
        "INSERT INTO series_cache (object_id, series_id, expires_at) VALUES (?, ?, ?) "
        "ON CONFLICT(object_id) DO UPDATE SET series_id = excluded.series_id, expires_at = excluded.expires_at",
        (object_id, series_id or _MISSING, time.time() + ttl),
    )


def resolve_series_ids(
    conn: sqlite3.Connection, object_ids: tuple[str, ...]
) -> dict[str, str | None] | None:
    """Resolve object IDs to parent series IDs, cache-first. None on failure
    (callers must fail closed on None, same contract as v1)."""
    resolved: dict[str, str | None] = {}
    unknown: list[str] = []

    for object_id in object_ids:
        hit, series_id = _cache_get(conn, object_id)
        if hit:
            resolved[object_id] = series_id
        else:
            unknown.append(object_id)

    if unknown:
        try:
            fetched = _get_resolver().resolve(unknown)
        except cr_api.ResolutionError:
            return None
        for object_id in unknown:
            series_id = fetched.get(object_id)
            _cache_put(conn, object_id, series_id)
            resolved[object_id] = series_id

    return resolved
