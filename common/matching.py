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
