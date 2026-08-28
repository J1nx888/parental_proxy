#!/usr/bin/env python3
"""Access-log writer, shared by the SNI and HTTP-layer authz helpers.

Dedupes: the same (username, domain, allowed) combination is only logged
once per DEDUPE_WINDOW_SECONDS, so one browsing session doesn't produce
dozens of near-identical rows (repeated TLS connections, page assets,
polling requests, etc). The reporting page stays readable, and "who
accessed what and when" still reflects genuinely new activity.
"""
from __future__ import annotations

import sqlite3
import time

from db import now_iso

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
    cutoff = time.time() - DEDUPE_WINDOW_SECONDS
    cutoff_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cutoff)) + "Z"
    # series_id is part of the dedupe key (via IS NOT DISTINCT FROM, so two
    # NULLs still match) so two different blocked shows on the same domain
    # each get their own row instead of collapsing into one.
    recent = conn.execute(
        "SELECT 1 FROM access_log "
        "WHERE username = ? AND domain = ? AND allowed = ? AND ts >= ? "
        "AND series_id IS ? "
        "LIMIT 1",
        (username, domain, 1 if allowed else 0, cutoff_iso, series_id),
    ).fetchone()
    if recent is not None:
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
