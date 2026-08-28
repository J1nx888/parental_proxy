#!/usr/bin/env python3
"""Shared SQLite access for the parental proxy: schema, connections, seeding.

Every component (basic_auth_helper, sni_helper, authz_helper, dashboard)
opens the same database file on the shared volume. WAL mode + a busy
timeout let multiple processes read/write concurrently without needing a
separate database server.
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.environ.get("PP_DB_PATH", "/config/parental_proxy.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- Every domain the proxy knows about, and how to treat it.
--   mode: 'splice'  = host-only check via SNI, never decrypted
--         'bump'    = fully decrypted, path/show-level rules apply
--         'trusted' = always spliced, never checked or logged
--   kind: 'generic' or 'crunchyroll' (crunchyroll gets the show-level
--         resolution layer on top of the normal domain/path checks)
--   is_global: 1 = every user gets this automatically (infra deps);
--              0 = each user needs an explicit assignment (user_domains)
CREATE TABLE IF NOT EXISTS domains (
    id         INTEGER PRIMARY KEY,
    pattern    TEXT UNIQUE NOT NULL,
    mode       TEXT NOT NULL CHECK (mode IN ('splice', 'bump', 'trusted')),
    kind       TEXT NOT NULL DEFAULT 'generic' CHECK (kind IN ('generic', 'crunchyroll')),
    is_global  INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_domains (
    id        INTEGER PRIMARY KEY,
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    UNIQUE(user_id, domain_id)
);

-- Allowed URL paths for a bump-mode domain. Global per domain (not
-- per-user) -- this is defense-in-depth against unrecognized endpoints,
-- same idea as v1's allowed_paths.txt, not something that varies by kid.
CREATE TABLE IF NOT EXISTS domain_paths (
    id        INTEGER PRIMARY KEY,
    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    pattern   TEXT NOT NULL,
    UNIQUE(domain_id, pattern)
);

CREATE TABLE IF NOT EXISTS user_shows (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    series_id   TEXT NOT NULL,
    series_name TEXT NOT NULL,
    UNIQUE(user_id, series_id)
);

-- Crunchyroll CMS object-id -> parent series-id resolution cache.
CREATE TABLE IF NOT EXISTS series_cache (
    object_id  TEXT PRIMARY KEY,
    series_id  TEXT,
    expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS access_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    user_id     INTEGER,
    username    TEXT NOT NULL,
    domain      TEXT NOT NULL,
    path        TEXT,
    series_id   TEXT,
    series_name TEXT,
    allowed     INTEGER NOT NULL,
    reason      TEXT
);

CREATE INDEX IF NOT EXISTS idx_access_log_ts ON access_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_access_log_dedupe ON access_log(username, domain, allowed, series_id, ts DESC);
"""


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection | None = None) -> None:
    owns_conn = conn is None
    conn = conn or get_conn()
    try:
        conn.executescript(SCHEMA)
    finally:
        if owns_conn:
            conn.close()


# ==========================================================
# SETTINGS
# ==========================================================

def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def set_setting_if_absent(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
        (key, value),
    )


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


def iso_secs_ago(seconds: float) -> str:
    """UTC ISO-8601 timestamp `seconds` in the past, same format as now_iso().
    Comparable lexicographically against now_iso() values (fixed-width UTC)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - seconds)) + "Z"
