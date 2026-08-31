"""controller/policy_state.py: computing + persisting the DesiredPolicy
JSON blob that phase3/nftables-manager reads directly from the shared
DB."""
from __future__ import annotations

import json

import db
import identity
from policy_state import compute_desired_policy, write_desired_policy


def _add_device(
    conn, mac_address, ignored=0, quarantined_at=None, is_authenticated=1, bump_enabled=0, bypass_login=0
):
    conn.execute(
        "INSERT INTO devices "
        "(mac_address, ignored, quarantined_at, is_authenticated, bump_enabled, bypass_login, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (mac_address, ignored, quarantined_at, is_authenticated, bump_enabled, bypass_login, db.now_iso()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM devices WHERE mac_address = ?", (mac_address,)).fetchone()


def _bind(conn, mac_address, ip):
    identity.record_binding(conn, mac_address, ip, source="rtnetlink")


def test_empty_db_yields_all_empty_sets(conn):
    policy = compute_desired_policy(conn)
    assert policy == {
        "authenticated": [],
        "unauthenticated": [],
        "bypass": [],
        "quarantine": [],
        "bump": [],
    }


def test_authenticated_device_goes_in_authenticated_set(conn):
    _add_device(conn, "aa:bb:cc:dd:ee:01", is_authenticated=1)
    _bind(conn, "aa:bb:cc:dd:ee:01", "192.168.1.21")
    policy = compute_desired_policy(conn)
    assert policy["authenticated"] == ["192.168.1.21"]
    assert policy["unauthenticated"] == []
    assert policy["bump"] == []


def test_unauthenticated_device_goes_in_unauthenticated_set(conn):
    _add_device(conn, "aa:bb:cc:dd:ee:01", is_authenticated=0)
    _bind(conn, "aa:bb:cc:dd:ee:01", "192.168.1.21")
    policy = compute_desired_policy(conn)
    assert policy["unauthenticated"] == ["192.168.1.21"]


def test_bypass_login_device_lands_in_authenticated_set_end_to_end(conn):
    """Regression test for a real bug found 2026-08-31: this query
    never even SELECTed bypass_login, so classify_device() could never
    have honored it regardless of its own logic -- a bypass_login
    device stayed stuck in 'unauthenticated' forever. Full pipeline
    proof, not just the pure classify_device() unit test."""
    _add_device(conn, "aa:bb:cc:dd:ee:01", is_authenticated=0, bypass_login=1)
    _bind(conn, "aa:bb:cc:dd:ee:01", "192.168.1.21")
    policy = compute_desired_policy(conn)
    assert policy["authenticated"] == ["192.168.1.21"]
    assert policy["unauthenticated"] == []


def test_ignored_device_goes_in_bypass_set_even_if_unauthenticated(conn):
    _add_device(conn, "aa:bb:cc:dd:ee:01", ignored=1, is_authenticated=0)
    _bind(conn, "aa:bb:cc:dd:ee:01", "192.168.1.21")
    policy = compute_desired_policy(conn)
    assert policy["bypass"] == ["192.168.1.21"]
    assert policy["unauthenticated"] == []


def test_quarantined_device_goes_in_quarantine_set(conn):
    _add_device(conn, "aa:bb:cc:dd:ee:01", quarantined_at="2026-08-29T00:00:00Z")
    _bind(conn, "aa:bb:cc:dd:ee:01", "192.168.1.21")
    policy = compute_desired_policy(conn)
    assert policy["quarantine"] == ["192.168.1.21"]


def test_bump_enabled_authenticated_device_is_in_both_authenticated_and_bump(conn):
    _add_device(conn, "aa:bb:cc:dd:ee:01", is_authenticated=1, bump_enabled=1)
    _bind(conn, "aa:bb:cc:dd:ee:01", "192.168.1.21")
    policy = compute_desired_policy(conn)
    assert policy["authenticated"] == ["192.168.1.21"]
    assert policy["bump"] == ["192.168.1.21"]


def test_bump_enabled_but_unauthenticated_device_is_excluded_from_bump(conn):
    # Hasn't logged in yet -- no DNS-tier access at all, so it can't be
    # bump-eligible either, regardless of the admin's bump_enabled flag.
    _add_device(conn, "aa:bb:cc:dd:ee:01", is_authenticated=0, bump_enabled=1)
    _bind(conn, "aa:bb:cc:dd:ee:01", "192.168.1.21")
    policy = compute_desired_policy(conn)
    assert policy["unauthenticated"] == ["192.168.1.21"]
    assert policy["bump"] == []


def test_bump_enabled_but_ignored_device_is_excluded_from_bump(conn):
    _add_device(conn, "aa:bb:cc:dd:ee:01", ignored=1, bump_enabled=1)
    _bind(conn, "aa:bb:cc:dd:ee:01", "192.168.1.21")
    policy = compute_desired_policy(conn)
    assert policy["bypass"] == ["192.168.1.21"]
    assert policy["bump"] == []


def test_brand_new_mac_with_no_pre_existing_devices_row_lands_in_unauthenticated(conn):
    """End-to-end proof of the Phase 4 gap fix, 2026-08-31: a MAC with
    NO devices row created ahead of time (unlike every other test in
    this file, which pre-creates one via _add_device) still ends up
    gated in the unauthenticated_v4 set on its very first observation,
    since identity.record_binding() now auto-creates a PREAUTH devices
    row for it -- not silently excluded from every set the way a
    device_id = NULL binding used to be."""
    identity.record_binding(conn, "aa:bb:cc:dd:ee:99", "192.168.1.99", source="rtnetlink")
    policy = compute_desired_policy(conn)
    assert policy["unauthenticated"] == ["192.168.1.99"]
    assert policy["authenticated"] == []
    assert policy["bypass"] == []


def test_device_with_no_binding_is_excluded_from_every_set(conn):
    _add_device(conn, "aa:bb:cc:dd:ee:01")
    policy = compute_desired_policy(conn)
    assert all(ips == [] for ips in policy.values())


def test_output_is_sorted(conn):
    _add_device(conn, "aa:bb:cc:dd:ee:01")
    _add_device(conn, "aa:bb:cc:dd:ee:02")
    _bind(conn, "aa:bb:cc:dd:ee:01", "192.168.1.30")
    _bind(conn, "aa:bb:cc:dd:ee:02", "192.168.1.10")
    policy = compute_desired_policy(conn)
    assert policy["authenticated"] == ["192.168.1.10", "192.168.1.30"]


def test_write_desired_policy_persists_and_upserts(conn):
    policy1 = {"authenticated": ["192.168.1.21"], "unauthenticated": [], "bypass": [], "quarantine": []}
    write_desired_policy(conn, policy1)
    row = conn.execute(
        "SELECT desired_policy_json FROM interception_runtime WHERE singleton_id = 1"
    ).fetchone()
    assert json.loads(row["desired_policy_json"]) == policy1

    policy2 = {"authenticated": [], "unauthenticated": ["192.168.1.99"], "bypass": [], "quarantine": []}
    write_desired_policy(conn, policy2)
    row = conn.execute(
        "SELECT desired_policy_json FROM interception_runtime WHERE singleton_id = 1"
    ).fetchone()
    assert json.loads(row["desired_policy_json"]) == policy2

    count = conn.execute("SELECT COUNT(*) AS c FROM interception_runtime").fetchone()["c"]
    assert count == 1, "expected a single upserted singleton row, not a new row per write"
