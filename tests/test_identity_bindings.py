"""common/identity.py: device_bindings observation recording, MAC/IP
conflict handling, and the network_events log."""
from __future__ import annotations

import json

import db
import identity

MAC_A = "aa:bb:cc:dd:ee:01"
MAC_B = "aa:bb:cc:dd:ee:02"
IP_1 = "192.168.1.21"
IP_2 = "192.168.1.22"


def _add_device(conn, mac_address, ignored=0):
    conn.execute(
        "INSERT INTO devices (mac_address, ignored, created_at) VALUES (?, ?, ?)",
        (mac_address, ignored, db.now_iso()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM devices WHERE mac_address = ?", (mac_address,)).fetchone()


def _bindings(conn):
    return conn.execute("SELECT * FROM device_bindings").fetchall()


def _events(conn):
    return conn.execute("SELECT * FROM network_events ORDER BY id").fetchall()


# ------------------------------------------------------------- new bindings

def test_new_mac_creates_pending_binding_and_event(conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink", seen_at="2026-08-29T00:00:00Z")

    rows = _bindings(conn)
    assert len(rows) == 1
    assert rows[0]["device_id"] is None
    assert rows[0]["mac_address"] == MAC_A
    assert rows[0]["ipv4_address"] == IP_1
    assert rows[0]["first_seen_at"] == "2026-08-29T00:00:00Z"
    assert rows[0]["last_seen_at"] == "2026-08-29T00:00:00Z"
    assert rows[0]["active"] == 1

    events = _events(conn)
    assert len(events) == 1
    assert events[0]["event_type"] == "binding_pending_association"
    assert events[0]["device_id"] is None
    assert events[0]["mac_address"] == MAC_A


def test_known_mac_associates_device_id_without_a_pending_event(conn):
    device = _add_device(conn, MAC_A)
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")

    rows = _bindings(conn)
    assert len(rows) == 1
    assert rows[0]["device_id"] == device["id"]
    assert _events(conn) == []  # already associated -- nothing pending to log


def test_repeated_observation_updates_last_seen_only(conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink", seen_at="2026-08-29T00:00:00Z")
    identity.record_binding(conn, MAC_A, IP_1, source="snapshot", seen_at="2026-08-29T00:05:00Z")

    rows = _bindings(conn)
    assert len(rows) == 1, "a repeated (mac, ip) observation must not create a second row"
    assert rows[0]["first_seen_at"] == "2026-08-29T00:00:00Z"
    assert rows[0]["last_seen_at"] == "2026-08-29T00:05:00Z"
    assert rows[0]["source"] == "snapshot"


# --------------------------------------------------------- conflict handling

def test_ip_reassigned_deactivates_old_binding_and_logs_event(conn):
    """IP_1 held by MAC_A, then a completely different MAC shows up
    claiming IP_1 (e.g. MAC_A's lease expired and DHCP handed the
    address to a new device)."""
    device_a = _add_device(conn, MAC_A)
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink", seen_at="2026-08-29T00:00:00Z")

    identity.record_binding(conn, MAC_B, IP_1, source="rtnetlink", seen_at="2026-08-29T01:00:00Z")

    rows = {r["mac_address"]: r for r in _bindings(conn)}
    assert rows[MAC_A]["active"] == 0, "the old MAC_A/IP_1 binding must be deactivated"
    assert rows[MAC_B]["active"] == 1
    assert rows[MAC_B]["device_id"] is None  # MAC_B itself is still unassociated

    # Two events, not one: the conflict itself ("ip_reassigned"), plus
    # MAC_B's own "binding_pending_association" since MAC_B has never
    # been seen before and isn't associated with any devices row either
    # -- the two are independent facts about this single observation.
    events = {e["event_type"]: e for e in _events(conn)}
    assert set(events) == {"ip_reassigned", "binding_pending_association"}

    reassigned = events["ip_reassigned"]
    assert reassigned["device_id"] == device_a["id"]
    assert reassigned["mac_address"] == MAC_A
    assert reassigned["ipv4_address"] == IP_1
    assert json.loads(reassigned["payload_json"]) == {"new_mac_address": MAC_B}

    pending = events["binding_pending_association"]
    assert pending["mac_address"] == MAC_B
    assert pending["ipv4_address"] == IP_1


def test_device_ip_changed_deactivates_old_binding_and_logs_event(conn):
    """Same MAC, new IP -- an ordinary DHCP lease renewal."""
    device = _add_device(conn, MAC_A)
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink", seen_at="2026-08-29T00:00:00Z")

    identity.record_binding(conn, MAC_A, IP_2, source="rtnetlink", seen_at="2026-08-29T02:00:00Z")

    rows = {r["ipv4_address"]: r for r in _bindings(conn)}
    assert rows[IP_1]["active"] == 0
    assert rows[IP_2]["active"] == 1
    assert rows[IP_2]["device_id"] == device["id"], (
        "the new binding must inherit the device_id from the superseded one"
    )

    events = _events(conn)
    assert len(events) == 1
    assert events[0]["event_type"] == "ip_changed"
    assert events[0]["device_id"] == device["id"]
    assert events[0]["ipv4_address"] == IP_1  # the OLD ip, per the event's own semantics
    assert json.loads(events[0]["payload_json"]) == {"new_ipv4_address": IP_2}


def test_both_conflicts_at_once_are_each_handled_independently(conn):
    """MAC_A was on IP_1; MAC_B was on IP_2. Now MAC_A is observed on
    IP_2 -- both MAC_A's old binding and MAC_B's old binding must be
    deactivated, with two separate events."""
    _add_device(conn, MAC_A)
    _add_device(conn, MAC_B)
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink", seen_at="2026-08-29T00:00:00Z")
    identity.record_binding(conn, MAC_B, IP_2, source="rtnetlink", seen_at="2026-08-29T00:01:00Z")

    identity.record_binding(conn, MAC_A, IP_2, source="rtnetlink", seen_at="2026-08-29T03:00:00Z")

    active = {r["mac_address"]: r for r in _bindings(conn) if r["active"] == 1}
    assert set(active) == {MAC_A}
    assert active[MAC_A]["ipv4_address"] == IP_2

    event_types = {e["event_type"] for e in _events(conn)}
    assert event_types == {"ip_reassigned", "ip_changed"}


# ------------------------------------------------------------- active_binding_ip

def test_active_binding_ip_returns_none_when_no_binding(conn):
    device = _add_device(conn, MAC_A)
    assert identity.active_binding_ip(conn, device["id"]) is None


def test_active_binding_ip_returns_the_freshest_active_binding(conn):
    device = _add_device(conn, MAC_A)
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink", seen_at="2026-08-29T00:00:00Z")
    # Manually insert a second (theoretical) simultaneously-active binding
    # with a later last_seen_at, to test the ORDER BY behavior directly
    # rather than relying on record_binding's own conflict handling (which
    # would normally deactivate the older one itself).
    conn.execute(
        "INSERT INTO device_bindings "
        "(device_id, mac_address, ipv4_address, first_seen_at, last_seen_at, source, active) "
        "VALUES (?, ?, ?, ?, ?, 'snapshot', 1)",
        (device["id"], MAC_A, IP_2, "2026-08-29T01:00:00Z", "2026-08-29T01:00:00Z"),
    )

    assert identity.active_binding_ip(conn, device["id"]) == IP_2


# ------------------------------------------------------------- network_events

def test_record_network_event_stores_arbitrary_payload_as_json(conn):
    identity.record_network_event(
        conn,
        "device_seen",
        mac_address=MAC_A,
        ipv4_address=IP_1,
        source="snapshot",
        payload={"band": "5ghz", "attached_to": "satellite-1"},
    )
    row = _events(conn)[0]
    assert row["event_type"] == "device_seen"
    assert json.loads(row["payload_json"]) == {"band": "5ghz", "attached_to": "satellite-1"}


# ------------------------------------------------------------- touch_binding_by_ip
# (Milestone 4's AdGuard query-log discovery source -- see
# controller/adguard_discovery.py, which is the one real caller.)

def test_touch_binding_by_ip_refreshes_last_seen_at_for_an_active_binding(conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink", seen_at="2026-08-29T00:00:00Z")

    updated = identity.touch_binding_by_ip(conn, IP_1, "2026-08-29T01:00:00Z")

    assert updated is True
    row = _bindings(conn)[0]
    assert row["last_seen_at"] == "2026-08-29T01:00:00Z"
    assert row["source"] == "adguard"
    assert row["mac_address"] == MAC_A  # unchanged -- this call never touches identity


def test_touch_binding_by_ip_does_not_regress_last_seen_at_backward(conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink", seen_at="2026-08-29T02:00:00Z")

    updated = identity.touch_binding_by_ip(conn, IP_1, "2026-08-29T01:00:00Z")

    assert updated is False
    row = _bindings(conn)[0]
    assert row["last_seen_at"] == "2026-08-29T02:00:00Z"
    assert row["source"] == "rtnetlink"  # unchanged -- the stale touch never applied


def test_touch_binding_by_ip_is_a_no_op_when_seen_at_is_not_strictly_newer(conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink", seen_at="2026-08-29T00:00:00Z")

    updated = identity.touch_binding_by_ip(conn, IP_1, "2026-08-29T00:00:00Z")

    assert updated is False


def test_touch_binding_by_ip_returns_false_when_no_active_binding_exists_for_the_ip(conn):
    assert identity.touch_binding_by_ip(conn, "192.168.1.99", "2026-08-29T00:00:00Z") is False


def test_touch_binding_by_ip_ignores_an_inactive_binding_for_the_same_ip(conn):
    # IP_1 gets reassigned from MAC_A to MAC_B -- record_binding's own
    # conflict handling deactivates MAC_A's binding for IP_1.
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink", seen_at="2026-08-29T00:00:00Z")
    identity.record_binding(conn, MAC_B, IP_1, source="rtnetlink", seen_at="2026-08-29T01:00:00Z")

    updated = identity.touch_binding_by_ip(conn, IP_1, "2026-08-29T02:00:00Z")

    assert updated is True
    rows = {row["mac_address"]: row for row in _bindings(conn)}
    assert rows[MAC_A]["active"] == 0
    assert rows[MAC_A]["last_seen_at"] == "2026-08-29T00:00:00Z"  # untouched
    assert rows[MAC_B]["active"] == 1
    assert rows[MAC_B]["last_seen_at"] == "2026-08-29T02:00:00Z"  # this is the one that got touched
