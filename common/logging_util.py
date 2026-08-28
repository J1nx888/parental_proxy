#!/usr/bin/env python3
"""Access-log writer, shared by the SNI and HTTP-layer authz helpers.

Dedupes: the same (username, domain, allowed, series_id) combination is
only logged once per DEDUPE_WINDOW_SECONDS, so one browsing session doesn't
produce dozens of near-identical rows (repeated TLS connections, page
assets, polling requests, etc). The reporting page stays readable, and
"who accessed what and when" still reflects genuinely new activity.
"""
from __future__ import annotations

import sqlite3

from db import iso_secs_ago, now_iso

DEDUPE_WINDOW_SECONDS = 5 * 60


def log_access(
    conn: sqlite3.Connection,
    *,
    user_id: int | None,
    username: str,
    domain: str,
    path: str | None,
    allowed: bool,
    reason: str,
    series_id: str | None = None,
    series_name: str | None = None,
) -> None:
    cutoff_iso = iso_secs_ago(DEDUPE_WINDOW_SECONDS)
    # series_id is part of the dedupe key (SQLite's `IS` is null-safe, so two
    # NULLs still match) so two different blocked shows on the same domain
    # each get their own row instead of collapsing into one.
    recent = conn.execute(
        "SELECT path FROM access_log "
        "WHERE username = ? AND domain = ? AND allowed = ? AND ts >= ? "
        "AND series_id IS ? "
        "ORDER BY id DESC LIMIT 1",
        (username, domain, 1 if allowed else 0, cutoff_iso, series_id),
    ).fetchone()
    if recent is not None:
        # Let a path-bearing entry through even if a path-less one for the
        # same key was already logged in this window -- e.g. the SNI layer
        # logs an unconfigured domain with no path (nothing is decrypted
        # yet), and the HTTP layer later logs the same domain with the real
        # path once it is. The path-less entry is strictly less useful, so
        # it must not block the richer one from ever appearing.
        if not (path is not None and recent["path"] is None):
            return
    conn.execute(
        "INSERT INTO access_log "
        "(ts, user_id, username, domain, path, series_id, series_name, allowed, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            now_iso(),
            user_id,
            username,
            domain,
            path,
            series_id,
            series_name,
            1 if allowed else 0,
            reason,
        ),
    )
