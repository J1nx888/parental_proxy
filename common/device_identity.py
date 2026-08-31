#!/usr/bin/env python3
"""Device-based identity resolution for Squid's intercept mode.

Replaces %LOGIN (Squid's per-request Basic-Auth challenge via
`proxy_auth`) as the identity signal the SNI/authz helper scripts key
their decisions on. This is the concrete piece of RoadMap.md's "Squid:
explicit-proxy-with-login -> transparent intercept" section (locked
2026-08-30): an intercepted connection has no CONNECT handshake for
Squid to answer a 407 challenge with, so per-request login is gone
entirely -- the client's own source IP (`%>a`, still available to an
intercepted connection) is the only identity signal left. Resolving it
means reusing the exact same device_bindings data the DNS tier's own
identity model already relies on (see common/identity.py), rather than
inventing a second, proxy-specific identity mechanism.

Used by both proxy/sni_helper.py and proxy/authz_helper.py, replacing
each of their previous `login` parameters.
"""
from __future__ import annotations

import sqlite3


def resolve_user_for_device(conn: sqlite3.Connection, device: sqlite3.Row | None) -> sqlite3.Row | None:
    """The `users` row for `device`'s own user_id, or None if the device
    itself is None, has no user_id at all (unassigned), or is assigned to
    a group instead of a person -- a group/device-only assignment is still
    a real, enforceable identity (see common/matching.py's
    device_domain_reason()), it just has no single `users` row of its own.

    **Replaces the old resolve_user(conn, client_ip) (removed 2026-08-31,
    see device_domain_reason()'s own docstring for the bug this was part
    of)**: that function INNER JOINed straight from device_bindings to
    users through devices.user_id in one query, so a group-assigned device
    resolved to None -- indistinguishable from "never seen at all" -- and
    every caller treated that None as "deny everything," even though the
    device itself was perfectly well identified. Callers now resolve the
    *device* first via resolve_device() above (which has no such blind
    spot -- it matches on device_bindings alone) and pass it here
    separately, so "no user" and "no identity at all" are never conflated
    again.
    """
    if device is None or device["user_id"] is None:
        return None
    return conn.execute("SELECT * FROM users WHERE id = ?", (device["user_id"],)).fetchone()


def log_identity_fields(
    device: sqlite3.Row | None, user: sqlite3.Row | None
) -> tuple[int | None, str, int | None]:
    """(user_id, username, device_id) for logging_util.log_access(), in
    priority order: a real resolved user; else the device's own label (or
    MAC address if unlabeled, matching how the dashboard's device
    comboboxes already display an unlabeled device -- see dashboard.py's
    _entity_combo) as a synthetic username, with device_id set so the
    Report page can still filter/act on this row by device or group even
    with no user_id; else the pre-existing "(unauthenticated)" placeholder,
    unchanged, for the genuinely-never-seen case (device itself is None --
    no active device_bindings row at all)."""
    if user is not None:
        return user["id"], user["username"], (device["id"] if device is not None else None)
    if device is not None:
        return None, device["label"] or device["mac_address"], device["id"]
    return None, "(unauthenticated)", None


def resolve_device(conn: sqlite3.Connection, client_ip: str) -> sqlite3.Row | None:
    """The `devices` row for whoever currently holds this source IP, or
    None if there's no active device_bindings row for it at all.

    Unlike resolve_user_for_device() above, this resolves straight from
    client_ip (not from an already-resolved device), and returns the
    device itself regardless of whether it has a user_id assigned --
    dashboard/captive_portal_server.py (Phase 4 milestone 3) needs the
    device_id itself to actually grant access (flipping
    is_authenticated), not just whichever user, if any, already owns
    it. A device with device_id NULL never matches here at all (the
    JOIN requires a real devices row) -- see common/identity.py's
    record_binding docstring for why that should be rare going forward
    (Phase 4 milestone 1 auto-creates one for a genuinely new MAC).
    """
    return conn.execute(
        """
        SELECT d.* FROM device_bindings b
        JOIN devices d ON d.id = b.device_id
        WHERE b.ipv4_address = ? AND b.active = 1
        ORDER BY b.last_seen_at DESC LIMIT 1
        """,
        (client_ip,),
    ).fetchone()
