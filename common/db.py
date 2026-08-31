#!/usr/bin/env python3
"""Shared SQLite access for the parental proxy: schema, connections, seeding.

Every component (sni_helper, authz_helper, dashboard) opens the same
database file on the shared volume. WAL mode + a busy timeout let multiple
processes read/write concurrently without needing a separate database
server.
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

-- A named category of shared devices (e.g. "TVs", "IoT", "Gaming
-- Computers") that gets its own domain allow-list, for devices that don't
-- belong to any one person. Parallel concept to `users`, not a kind of
-- user -- a device is assigned to at most one user OR one group (see
-- `devices` below), never both.
CREATE TABLE IF NOT EXISTS groups (
    id         INTEGER PRIMARY KEY,
    name       TEXT UNIQUE NOT NULL,
    created_at TEXT NOT NULL
);

-- A group's domain allow-list -- the group-level equivalent of
-- `user_domains`. Same shape, same meaning: a row here grants every
-- device in this group access to this domain, independent of the
-- domain's own `is_global` flag.
CREATE TABLE IF NOT EXISTS group_domains (
    id        INTEGER PRIMARY KEY,
    group_id  INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    UNIQUE(group_id, domain_id)
);

-- v2 roadmap groundwork (no enforcement reads any of this yet -- Phase
-- 3+): a physical device on the network, identified by MAC address.
--   user_id / group_id: at most one of these is set (enforced below) --
--       a device belongs to a specific person, OR to a shared-device
--       group ("TVs", "IoT"), OR neither. `ignored` is a third,
--       independent state (see below), not a third value of this pair.
--   ignored: "never do anything with this device, ever" -- a stronger,
--       distinct statement from just being unassigned. An unassigned
--       device is still a known device with no policy decided yet;
--       an ignored one (the admin's own laptop, a guest's phone) is
--       deliberately outside the whole system, for good.
--   bump_enabled: whether this device is one of the small, deliberately
--       curated set allowed to use SSL-Bump (path/show-level rules) at
--       all. A domain's 'bump' mode only actually bumps traffic from a
--       bump_enabled device; every other device gets that domain's
--       DNS/splice-level (whole-domain) treatment instead -- bump-tier is
--       device-driven, not domain-driven (see the v2 roadmap).
--   bypass_login: exempts a device that can't complete a login flow at
--       all (smart TV, Echo, thermostat) from the eventual captive-portal
--       gate, falling back to its user/group/ignored assignment above
--       instead of a personal login. Orthogonal to that assignment --
--       a bypass_login device can still belong to a user or a group.
--   is_authenticated: the eventual captive-portal gate's per-device flag.
--       Defaults to 1 (authenticated) for every device today, since
--       there's no login gate yet to fail -- added now, ahead of that
--       feature, specifically so building it later is additive (one more
--       rule keyed on this flag) rather than a schema change.
--   last_seen_at: when this device was last observed on the network.
--       NULL means "never observed" -- nothing populates this column yet
--       (that needs the Phase 3 interception layer or an equivalent
--       mechanism), so every device's is NULL today. Deliberately kept
--       distinct from "definitely stale": the stale-device cleanup below
--       only ever matches a real, old timestamp, never a NULL one, so
--       turning that feature on can't mass-delete every device just
--       because none of them have been seen yet.
--   quarantined_at: Milestone 8's operator-triggered isolation state
--       (the QUARANTINE policy class -- see common/policy_class.py).
--       NULL means not quarantined (the default for every device).
--       Nothing sets this yet -- no dashboard control exists to
--       trigger it -- this column exists so the policy-classification
--       logic and the nftables quarantine_v4 set have something real
--       to read once that control is built.
CREATE TABLE IF NOT EXISTS devices (
    id               INTEGER PRIMARY KEY,
    mac_address      TEXT UNIQUE NOT NULL,
    label            TEXT,
    user_id          INTEGER REFERENCES users(id) ON DELETE SET NULL,
    group_id         INTEGER REFERENCES groups(id) ON DELETE SET NULL,
    ignored          INTEGER NOT NULL DEFAULT 0,
    last_known_ip    TEXT,
    last_seen_at     TEXT,
    bump_enabled     INTEGER NOT NULL DEFAULT 0,
    bypass_login     INTEGER NOT NULL DEFAULT 0,
    is_authenticated INTEGER NOT NULL DEFAULT 1,
    quarantined_at   TEXT,
    created_at       TEXT NOT NULL,
    CHECK (user_id IS NULL OR group_id IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);
CREATE INDEX IF NOT EXISTS idx_devices_group ON devices(group_id);

-- A single device's own domain allow-list -- for granting one specific
-- device access directly, without needing to create a whole group for
-- it. Independent of (and can coexist with) that device's user_id/
-- group_id assignment -- e.g. a device assigned to a kid can still get
-- an extra domain granted just to it, on top of whatever that kid has.
CREATE TABLE IF NOT EXISTS device_domains (
    id        INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    UNIQUE(device_id, domain_id)
);

-- Phase 3 identity model (Milestone 4): every observed MAC<->IPv4
-- pairing, feeding the interception controller's desired-state
-- computation (controller/desired_state.py). A device's IP can change
-- (DHCP lease renewal) and an IP can be reassigned to a different MAC
-- over time (a departed device's lease reused by a new one) -- this
-- table keeps every observed pairing rather than overwriting in place,
-- so `active` distinguishes "this is the binding to trust right now"
-- from stale history, and last_seen_at lets a consumer judge freshness
-- rather than trusting a silently-clobbered single row.
--   source: where this observation came from -- 'rtnetlink' (kernel
--       neighbor-table events, lowest latency), 'snapshot' (periodic
--       `ip neigh` poll, catches anything rtnetlink missed), 'adguard'
--       (DNS query-log correlation, confirms active IP usage),
--       'bettercap' (optional enrichment only, never load-bearing --
--       see the v2 roadmap notes), 'active_scan' (rate-limited direct
--       ARP probe, only for stale/onboarding devices).
--   confidence: 1.0 for a direct observation (rtnetlink/snapshot);
--       lower for inferred/enrichment-only sources. Not consumed by
--       anything yet -- reserved for when multiple simultaneous
--       "active" bindings for one device need to be ranked.
--   device_id: nullable, deliberately -- a MAC never seen before gets
--       a binding row immediately (so nothing is silently dropped),
--       but is NOT auto-associated to a `devices` row from network
--       data alone (hostname/vendor guessing is exactly the
--       auto-merge the v2 roadmap rules out). A NULL device_id is a
--       pending binding awaiting a human association; ON DELETE SET
--       NULL means deleting a `devices` row later returns its
--       bindings to pending rather than discarding the observation.
CREATE TABLE IF NOT EXISTS device_bindings (
    id            INTEGER PRIMARY KEY,
    device_id     INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    mac_address   TEXT NOT NULL,
    ipv4_address  TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    source        TEXT NOT NULL CHECK (source IN ('rtnetlink', 'snapshot', 'adguard', 'bettercap', 'active_scan')),
    confidence    REAL NOT NULL DEFAULT 1.0,
    active        INTEGER NOT NULL DEFAULT 1,
    UNIQUE(mac_address, ipv4_address)
);

CREATE INDEX IF NOT EXISTS idx_device_bindings_device ON device_bindings(device_id);
CREATE INDEX IF NOT EXISTS idx_device_bindings_ip_active ON device_bindings(ipv4_address, active);

-- Singleton row (see the CHECK) tracking the interception layer's own
-- runtime state: what generation the controller wants applied vs what
-- the worker has actually confirmed, and whether the system is
-- currently degraded. Written by the controller (see
-- controller/health.py, Milestone 6); read by the dashboard for an
-- eventual "interception health" view (nothing reads it yet -- see the
-- v2 roadmap notes on that future dashboard scope).
--   desired_policy_json: Milestone 7's DesiredPolicy blob, computed by
--       controller/policy_state.py from devices/device_bindings and
--       read directly by phase3/nftables-manager (Go) -- following
--       this project's own "one shared database, live reads" pattern
--       (see docs/project.md) instead of a new IPC protocol between
--       the two processes. NULL means "nothing computed yet",
--       distinct from an explicit empty policy.
--   nft_mode / nft_last_healthy_at / nft_fail_reason: nftables-manager's
--       OWN health, deliberately separate columns from mode/
--       last_healthy_at/fail_open_reason above (which track the
--       controller<->ARP-worker pipeline) so the two subsystems never
--       clobber each other's status in this shared singleton row.
CREATE TABLE IF NOT EXISTS interception_runtime (
    singleton_id        INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    desired_generation   INTEGER NOT NULL DEFAULT 0,
    applied_generation   INTEGER NOT NULL DEFAULT 0,
    mode                 TEXT NOT NULL DEFAULT 'stopped' CHECK (mode IN ('stopped', 'running', 'repair_only', 'fail_open')),
    last_healthy_at      TEXT,
    fail_open_reason     TEXT,
    desired_policy_json  TEXT,
    nft_mode             TEXT NOT NULL DEFAULT 'stopped' CHECK (nft_mode IN ('stopped', 'running', 'fail_open')),
    nft_last_healthy_at  TEXT,
    nft_fail_reason      TEXT
);

-- Normalized network/identity-layer event log (device seen/lost, a
-- binding created or superseded by a MAC/IP conflict, etc.) -- distinct
-- from access_log, which is proxy-layer allow/deny decisions. This is
-- the "outbox events" RoadMap.md's Milestone 4 refers to.
CREATE TABLE IF NOT EXISTS network_events (
    id           INTEGER PRIMARY KEY,
    event_type   TEXT NOT NULL,
    device_id    INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    mac_address  TEXT,
    ipv4_address TEXT,
    source       TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_network_events_observed ON network_events(observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_network_events_device ON network_events(device_id);

CREATE TABLE IF NOT EXISTS access_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    user_id     INTEGER,
    -- Added 2026-08-31 alongside common/matching.py's device_domain_reason()
    -- fix: which `devices` row made this request, when known. Lets the
    -- Report page filter/act on a row by device or group even when it has
    -- no user_id at all (a group- or device-assigned identity). No
    -- REFERENCES clause, matching user_id's own existing convention on
    -- this table.
    device_id   INTEGER,
    username    TEXT NOT NULL,
    domain      TEXT NOT NULL,
    path        TEXT,
    series_id   TEXT,
    series_name TEXT,
    allowed     INTEGER NOT NULL,
    reason      TEXT,
    -- Set when the kid-facing /blocked page's "Request approval" button is
    -- used against this row; cleared again once an admin acts on it via
    -- approve_from_report(). NULL = no outstanding request.
    approval_requested_at TEXT
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
        _migrate(conn)
    finally:
        if owns_conn:
            conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Schema changes made after the initial release, applied to databases
    that already exist -- the `CREATE TABLE IF NOT EXISTS` statements above
    only cover a brand-new database, SQLite has no `ALTER TABLE ... ADD
    COLUMN IF NOT EXISTS`, and this project has no versioned migration
    system. Each check here is independently idempotent (safe to run on
    every startup, on any existing database, in any order)."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(access_log)")}
    if "approval_requested_at" not in columns:
        conn.execute("ALTER TABLE access_log ADD COLUMN approval_requested_at TEXT")
    if "device_id" not in columns:
        conn.execute("ALTER TABLE access_log ADD COLUMN device_id INTEGER")

    device_columns = {row["name"] for row in conn.execute("PRAGMA table_info(devices)")}
    if "last_seen_at" not in device_columns:
        conn.execute("ALTER TABLE devices ADD COLUMN last_seen_at TEXT")
    if "quarantined_at" not in device_columns:
        conn.execute("ALTER TABLE devices ADD COLUMN quarantined_at TEXT")

    # interception_runtime is itself a new (Milestone 4) table, so an
    # existing pre-Milestone-4 database won't have it at all yet --
    # CREATE TABLE IF NOT EXISTS above handles that case. This only
    # covers the Milestone 6/7 columns added to interception_runtime
    # after ITS initial release, same idempotent pattern as devices
    # above. No CHECK constraint on the migrated nft_mode column (SQLite's
    # ADD COLUMN support for inline CHECK is version-fragile) -- unlike
    # a fresh database, an existing one won't enforce it at the schema
    # level; application code is still expected to only write the three
    # valid values.
    runtime_columns = {row["name"] for row in conn.execute("PRAGMA table_info(interception_runtime)")}
    if runtime_columns and "desired_policy_json" not in runtime_columns:
        conn.execute("ALTER TABLE interception_runtime ADD COLUMN desired_policy_json TEXT")
    if runtime_columns and "nft_mode" not in runtime_columns:
        conn.execute("ALTER TABLE interception_runtime ADD COLUMN nft_mode TEXT NOT NULL DEFAULT 'stopped'")
    if runtime_columns and "nft_last_healthy_at" not in runtime_columns:
        conn.execute("ALTER TABLE interception_runtime ADD COLUMN nft_last_healthy_at TEXT")
    if runtime_columns and "nft_fail_reason" not in runtime_columns:
        conn.execute("ALTER TABLE interception_runtime ADD COLUMN nft_fail_reason TEXT")


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
