#!/usr/bin/env python3
"""Pattern matching helpers shared by the Squid helper scripts."""
from __future__ import annotations

import functools
import ipaddress
import re
import sqlite3


@functools.lru_cache(maxsize=1024)
def _domain_regex(pattern: str) -> re.Pattern[str] | None:
    """Compile a stored domain pattern as an anchored domain-suffix match.

    A stored pattern (e.g. ``crunchyroll\\.com``) matches that host and any
    subdomain of it, but NOT ``evilcrunchyroll.com`` and NOT
    ``crunchyroll.com.attacker.example`` -- the match has to begin on a
    label boundary and run to the end of the hostname. Without this,
    ``re.search`` treated every pattern as an unanchored substring and any
    FQDN containing an allowed string (``evil-jsdelivr.net`` vs the seeded
    ``jsdelivr\\.net``) slipped through the allowlist.

    Returns None for a pattern that isn't a valid regex; find_domain then
    skips it rather than raising.
    """
    try:
        return re.compile(r"(?:^|\.)(?:" + pattern + r")\Z", re.IGNORECASE)
    except re.error:
        return None


@functools.lru_cache(maxsize=2048)
def _path_regex(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        return None


def find_domain(conn: sqlite3.Connection, hostname: str) -> sqlite3.Row | None:
    """Return the first domains row whose pattern matches hostname, or None.

    Patterns are regexes anchored as a domain suffix (see _domain_regex).
    Rows are checked in insertion order (id ASC); first match wins.
    """
    hostname = (hostname or "").strip().rstrip(".").lower()
    if not hostname:
        return None
    for row in conn.execute("SELECT * FROM domains ORDER BY id"):
        rx = _domain_regex(row["pattern"])
        if rx is not None and rx.search(hostname):
            return row
    return None


def path_allowed(conn: sqlite3.Connection, domain_id: int, path: str) -> bool:
    path = path or "/"
    for row in conn.execute(
        "SELECT pattern FROM domain_paths WHERE domain_id = ?", (domain_id,)
    ):
        rx = _path_regex(row["pattern"])
        if rx is not None and rx.search(path):
            return True
    return False


def user_has_domain(conn: sqlite3.Connection, user_id: int, domain_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM user_domains WHERE user_id = ? AND domain_id = ?",
        (user_id, domain_id),
    ).fetchone()
    return row is not None


def group_has_domain(conn: sqlite3.Connection, group_id: int, domain_id: int) -> bool:
    """group_domains mirrors user_domains exactly. Consulted by proxy
    enforcement via device_domain_reason() below (fixed 2026-08-31 -- this
    function existed since the v2 roadmap groundwork but was never actually
    called from any enforcement path until then; see that function's own
    docstring for the bug this closed)."""
    row = conn.execute(
        "SELECT 1 FROM group_domains WHERE group_id = ? AND domain_id = ?",
        (group_id, domain_id),
    ).fetchone()
    return row is not None


def device_has_domain(conn: sqlite3.Connection, device_id: int, domain_id: int) -> bool:
    """device_domains grants one specific device access directly,
    independent of any user/group assignment. Consulted by proxy
    enforcement via device_domain_reason() below (fixed 2026-08-31, same
    as group_has_domain() above)."""
    row = conn.execute(
        "SELECT 1 FROM device_domains WHERE device_id = ? AND domain_id = ?",
        (device_id, domain_id),
    ).fetchone()
    return row is not None


def device_domain_reason(conn: sqlite3.Connection, device: sqlite3.Row, domain: sqlite3.Row) -> str | None:
    """The specific reason `device` is authorized for `domain`, or None if
    it isn't authorized by any axis. Checked in this order: is_global, then
    per-user (if device has a user_id), then per-group (if device has a
    group_id), then per-device direct assignment -- the first thing that
    matches wins.

    **Fixed 2026-08-31 -- a real bug, found while scoping tighter Squid/
    AdGuard integration (see RoadMap.md's dated entry)**: proxy/authz_helper.py
    and proxy/sni_helper.py used to resolve identity via
    device_identity.resolve_user() (an INNER JOIN on devices.user_id) and
    then only ever check `is_global or user_has_domain(...)` inline -- so a
    device assigned to a GROUP (user_id NULL) resolved to no identity at
    all and was denied everything before even reaching a domain check, and
    even a device that DID resolve to a user could never benefit from a
    group_domains or device_domains grant, because nothing ever called
    group_has_domain()/device_has_domain() at all. This function is the
    single shared replacement for that old inline check, used by both proxy
    helpers (which now resolve the *device* first, see
    device_identity.resolve_device()) and controller/adguard_sync.py's
    build_splice_deny_rules() -- one place that knows all four axes, so
    Squid and AdGuard can never drift out of sync on what "authorized"
    means again.

    Returns a reason string (not a bare bool) so callers get their log
    line's reason for free, matching the existing "global_domain"/
    "user_domain" vocabulary and extending it with "group_domain"/
    "device_domain" for the two axes this fix newly wires up.
    """
    if domain["is_global"]:
        return "global_domain"
    if device["user_id"] is not None and user_has_domain(conn, device["user_id"], domain["id"]):
        return "user_domain"
    if device["group_id"] is not None and group_has_domain(conn, device["group_id"], domain["id"]):
        return "group_domain"
    if device_has_domain(conn, device["id"], domain["id"]):
        return "device_domain"
    return None


def user_has_show(conn: sqlite3.Connection, user_id: int, series_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM user_shows WHERE user_id = ? AND series_id = ?",
        (user_id, series_id.upper()),
    ).fetchone()
    return row is not None


def get_user_by_username(conn: sqlite3.Connection, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()


def ip_in_configured_lan(conn: sqlite3.Connection, ip_str: str) -> bool:
    """Check a client IP against the (dashboard-editable) local_network
    setting -- space-separated CIDRs, e.g. "192.168.1.0/24 10.0.0.0/24".

    An empty setting means the operator has disabled the LAN check (access
    is then controlled by the per-person proxy login alone). This matters
    under Docker bridge / Docker Desktop, where the proxy sees an internal
    gateway address rather than the real client IP and every request would
    otherwise be rejected as outside_lan.
    """
    from db import get_setting  # local import: callers that don't need db stay light

    raw = (get_setting(conn, "local_network") or "").strip()
    if not raw:
        return True  # check disabled
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for cidr in raw.split():
        try:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False
