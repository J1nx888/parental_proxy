#!/usr/bin/env python3
"""Crunchyroll object-id -> parent series-id resolution, cached in SQLite.

Same idea as v1's cr_cache.py, but backed by the shared database instead of
a JSON file, since multiple proxy helper processes need consistent access.

An object-id's parent series never changes, so a positive cache entry stays
correct forever. When the Crunchyroll API is unreachable (network down, or
the hardcoded anonymous client id in cr_api.py has been rotated) we serve
those positive entries even past their TTL rather than break playback of
shows that were already resolved. Only a request with no positive history
at all fails closed.
"""
from __future__ import annotations

import sqlite3
import time

import cr_api
import db as db_mod

POSITIVE_TTL_SECONDS = 30 * 24 * 60 * 60
NEGATIVE_TTL_SECONDS = 60 * 60
_MISSING = "__missing__"
_RESOLVER_ERROR_KEY = "cr_resolver_last_error"

_resolver: cr_api.SeriesResolver | None = None


def _get_resolver() -> cr_api.SeriesResolver:
    global _resolver
    if _resolver is None:
        _resolver = cr_api.SeriesResolver()
    return _resolver


def _cache_get(
    conn: sqlite3.Connection, object_id: str, *, allow_stale: bool = False
) -> tuple[bool, str | None]:
    """Return (hit, series_id). A negative cache hit returns (True, None).

    allow_stale keeps an expired *positive* entry usable (see module docs);
    an expired negative entry is never served stale -- we re-check instead
    of blocking a show forever on a lookup that might now succeed.
    """
    object_id = object_id.upper()
    row = conn.execute(
        "SELECT series_id, expires_at FROM series_cache WHERE object_id = ?",
        (object_id,),
    ).fetchone()
    if row is None:
        return False, None
    expired = time.time() >= row["expires_at"]
    series_id = row["series_id"]

    if series_id == _MISSING:
        if expired:
            return False, None
        return True, None

    if expired and not allow_stale:
        # Report a miss but leave the row in place -- resolve_series_ids()
        # checks every id this way *before* ever calling the resolver, so
        # deleting here would destroy the positive entry the allow_stale
        # fallback is supposed to serve if that resolver call then fails
        # (see module docstring / S2.6). A successful re-resolve overwrites
        # this row anyway via _cache_put's ON CONFLICT UPDATE.
        return False, None
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


def _record_resolver_error(conn: sqlite3.Connection, exc: Exception) -> None:
    try:
        db_mod.set_setting(conn, _RESOLVER_ERROR_KEY, f"{db_mod.now_iso()} {exc}"[:500])
    except Exception:
        pass


def _clear_resolver_error(conn: sqlite3.Connection) -> None:
    try:
        if db_mod.get_setting(conn, _RESOLVER_ERROR_KEY) is not None:
            conn.execute("DELETE FROM settings WHERE key = ?", (_RESOLVER_ERROR_KEY,))
    except Exception:
        pass


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
        except cr_api.ResolutionError as exc:
            _record_resolver_error(conn, exc)
            # Fall back to any still-valid-or-stale positive cache entry.
            for object_id in unknown:
                hit, series_id = _cache_get(conn, object_id, allow_stale=True)
                if hit and series_id is not None:
                    resolved[object_id] = series_id
                else:
                    return None
            return resolved

        _clear_resolver_error(conn)
        for object_id in unknown:
            series_id = fetched.get(object_id)
            _cache_put(conn, object_id, series_id)
            resolved[object_id] = series_id

    return resolved
