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


def resolve_user(conn: sqlite3.Connection, client_ip: str) -> sqlite3.Row | None:
    """The `users` row for whoever this source IP currently belongs to,
    or None if identity can't be resolved.

    None covers three distinct cases, deliberately not told apart here
    -- every one of them means "treat this like the old unauthenticated
    %LOGIN case," so a caller doesn't need to: no active
    device_bindings row for this IP at all (never seen, or a stale
    binding); the bound device belongs to a group instead of a user
    (device_bindings mirrors what device_bindings.device_id points to,
    but common/matching.py's group-based checks aren't wired into any
    proxy enforcement path yet -- see its own docstrings); or the
    device has neither (unassigned).

    In practice this is only ever reached for a bump_v4 device in the
    first place -- nftables only redirects bump-enabled, already-
    authenticated devices to Squid's intercept ports at all (see
    phase3/nftables-manager/internal/nft/knftables_adapter.go) -- but
    this function makes no assumption about that; a resolution failure
    here is simply "no identity," exactly like an empty/absent %LOGIN
    used to be.

    A device with more than one simultaneously-active binding (see
    common/identity.py's own notes on how that can briefly happen)
    resolves to the most-recently-seen one, matching
    common/identity.py's own active_binding_ip().
    """
    return conn.execute(
        """
        SELECT u.* FROM device_bindings b
        JOIN devices d ON d.id = b.device_id
        JOIN users u ON u.id = d.user_id
        WHERE b.ipv4_address = ? AND b.active = 1
        ORDER BY b.last_seen_at DESC LIMIT 1
        """,
        (client_ip,),
    ).fetchone()


def resolve_device(conn: sqlite3.Connection, client_ip: str) -> sqlite3.Row | None:
    """The `devices` row for whoever currently holds this source IP, or
    None if there's no active device_bindings row for it at all.

    Unlike resolve_user() above, this returns the device itself
    regardless of whether it already has a user_id assigned --
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
