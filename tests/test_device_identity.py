"""common/device_identity.py: source-IP-based identity resolution for
Squid's intercept mode (resolve_user) and, since Phase 4 milestone 3,
the captive-portal login server (resolve_device). No dedicated test
file existed for this module before now -- resolve_user only had
incidental coverage via tests/test_helpers_protocol.py's sni/authz
helper tests; added direct coverage for both functions here while
adding resolve_device.
"""
from __future__ import annotations

import db
import identity
from device_identity import resolve_device, resolve_user

MAC_A = "aa:bb:cc:dd:ee:01"
IP_1 = "192.168.1.21"


def _add_user(conn, username="kid1"):
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash, created_at) VALUES (?,?,?,?)",
        (username, username, "unused-hash", db.now_iso()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


# ============================================================
# resolve_device
# ============================================================

def test_resolve_device_returns_none_with_no_active_binding(conn):
    assert resolve_device(conn, IP_1) is None


def test_resolve_device_returns_the_device_row(conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")

    device = resolve_device(conn, IP_1)

    assert device is not None
    assert device["mac_address"] == MAC_A


def test_resolve_device_works_with_no_user_assigned(conn):
    """Unlike resolve_user, this must still find the device even though
    nobody owns it yet -- that's the whole point (the captive-portal
    login is often the FIRST time a device gets a user at all)."""
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")

    device = resolve_device(conn, IP_1)

    assert device is not None
    assert device["user_id"] is None


def test_resolve_device_ignores_an_inactive_binding(conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink", seen_at="2026-08-30T00:00:00Z")
    identity.record_binding(conn, "aa:bb:cc:dd:ee:02", IP_1, source="rtnetlink", seen_at="2026-08-31T00:00:00Z")

    device = resolve_device(conn, IP_1)

    assert device["mac_address"] == "aa:bb:cc:dd:ee:02"


# ============================================================
# resolve_user (pre-existing function, previously untested in
# isolation -- see this file's own module docstring)
# ============================================================

def test_resolve_user_returns_none_with_no_active_binding(conn):
    assert resolve_user(conn, IP_1) is None


def test_resolve_user_returns_none_when_the_device_has_no_user(conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")
    assert resolve_user(conn, IP_1) is None


def test_resolve_user_returns_the_owning_user(conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")
    device_id = conn.execute("SELECT device_id FROM device_bindings WHERE ipv4_address = ?", (IP_1,)).fetchone()["device_id"]
    user = _add_user(conn)
    conn.execute("UPDATE devices SET user_id = ? WHERE id = ?", (user["id"], device_id))
    conn.commit()

    resolved = resolve_user(conn, IP_1)

    assert resolved["username"] == "kid1"
