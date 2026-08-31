#!/usr/bin/env python3
"""Phase 3 identity model (Milestone 4): recording observed MAC<->IPv4
bindings and the network/identity event log that feeds them.

See db.py's device_bindings/network_events schema comments and
docs/design/phase3-technical-design.md section 7 for the design this
implements. Nothing in the proxy/dashboard enforcement path reads any
of this yet -- the one consumer today is controller/desired_state.py,
which itself isn't wired into a running interception layer yet (see
RoadMap.md's Milestone 3/4 status).
"""
from __future__ import annotations

import json
import sqlite3

import db


def record_binding(
    conn: sqlite3.Connection,
    mac_address: str,
    ipv4_address: str,
    source: str,
    seen_at: str | None = None,
    confidence: float = 1.0,
) -> None:
    """Record one MAC<->IP observation.

    Idempotent for a repeated (mac, ip) pair -- just bumps last_seen_at.
    Handles the two conflict shapes the v2 roadmap's "MAC/IP conflict
    handling" requirement calls for, deactivating whichever prior
    binding is now stale and logging a network_events row for each:

      - IP reassigned: this ipv4_address was actively bound to a
        DIFFERENT mac_address (e.g. a departed device's DHCP lease
        reused by a new one).
      - Device moved: this mac_address was actively bound to a
        DIFFERENT ipv4_address (e.g. a normal DHCP lease renewal).

    Never auto-associates a brand-new binding with a `devices` row
    beyond reusing whatever device_id (if any) an existing binding for
    the same mac_address already carries, or a `devices` row whose
    mac_address already matches -- a MAC seen for the very first time
    gets a pending (device_id NULL) binding, requiring a human
    association, per the roadmap's "never auto-merge devices solely by
    hostname/vendor" rule.
    """
    seen_at = seen_at or db.now_iso()

    existing = conn.execute(
        "SELECT id FROM device_bindings WHERE mac_address = ? AND ipv4_address = ?",
        (mac_address, ipv4_address),
    ).fetchone()
    if existing is not None:
        conn.execute(
            "UPDATE device_bindings SET last_seen_at = ?, source = ?, confidence = ?, active = 1 "
            "WHERE id = ?",
            (seen_at, source, confidence, existing["id"]),
        )
        return

    # Conflict 1: someone else currently holds this IP.
    ip_conflict = conn.execute(
        "SELECT id, mac_address, device_id FROM device_bindings "
        "WHERE ipv4_address = ? AND active = 1",
        (ipv4_address,),
    ).fetchone()
    if ip_conflict is not None:
        conn.execute("UPDATE device_bindings SET active = 0 WHERE id = ?", (ip_conflict["id"],))
        _record_event(
            conn,
            "ip_reassigned",
            device_id=ip_conflict["device_id"],
            mac_address=ip_conflict["mac_address"],
            ipv4_address=ipv4_address,
            source=source,
            observed_at=seen_at,
            payload={"new_mac_address": mac_address},
        )

    # Conflict 2: this MAC currently holds a different IP.
    mac_conflict = conn.execute(
        "SELECT id, ipv4_address, device_id FROM device_bindings "
        "WHERE mac_address = ? AND active = 1",
        (mac_address,),
    ).fetchone()
    device_id = mac_conflict["device_id"] if mac_conflict is not None else None
    if mac_conflict is not None and mac_conflict["ipv4_address"] != ipv4_address:
        conn.execute("UPDATE device_bindings SET active = 0 WHERE id = ?", (mac_conflict["id"],))
        _record_event(
            conn,
            "ip_changed",
            device_id=device_id,
            mac_address=mac_address,
            ipv4_address=mac_conflict["ipv4_address"],
            source=source,
            observed_at=seen_at,
            payload={"new_ipv4_address": ipv4_address},
        )

    if device_id is None:
        device_id = _existing_device_id_for_mac(conn, mac_address)

    conn.execute(
        "INSERT INTO device_bindings "
        "(device_id, mac_address, ipv4_address, first_seen_at, last_seen_at, source, confidence, active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (device_id, mac_address, ipv4_address, seen_at, seen_at, source, confidence),
    )
    if device_id is None:
        _record_event(
            conn,
            "binding_pending_association",
            device_id=None,
            mac_address=mac_address,
            ipv4_address=ipv4_address,
            source=source,
            observed_at=seen_at,
            payload=None,
        )


def _existing_device_id_for_mac(conn: sqlite3.Connection, mac_address: str) -> int | None:
    row = conn.execute("SELECT id FROM devices WHERE mac_address = ?", (mac_address,)).fetchone()
    return row["id"] if row is not None else None


def active_binding_ip(conn: sqlite3.Connection, device_id: int) -> str | None:
    """The most-recently-seen active IPv4 address bound to device_id, or
    None if it has no active binding right now."""
    row = conn.execute(
        "SELECT ipv4_address FROM device_bindings "
        "WHERE device_id = ? AND active = 1 ORDER BY last_seen_at DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    return row["ipv4_address"] if row is not None else None


def touch_binding_by_ip(
    conn: sqlite3.Connection,
    ipv4_address: str,
    seen_at: str,
    source: str = "adguard",
) -> bool:
    """Refreshes last_seen_at for the currently-ACTIVE device_bindings
    row for this IP -- WITHOUT creating a new binding and WITHOUT
    touching its mac_address/device_id/confidence -- for sources that
    only ever observe an IP, never a MAC (AdGuard's query log is the
    only caller today: DNS queries carry no link-layer information), so
    they can never discover a brand-new binding on their own, only
    confirm an already-known one is still actively in use (RoadMap.md's
    discovery precedence: "AdGuard query-log observations (confirms
    active IP usage)"). Contrast with record_binding above, which
    always has a MAC and can create/reassign bindings.

    Only updates if `seen_at` is strictly newer than the binding's
    current last_seen_at -- a page of query-log history can include
    entries a previous poll already accounted for, and this must never
    regress last_seen_at backward. Callers must pass `seen_at` already
    normalized to this project's own db.now_iso() format (see
    common/adguard_client.py's normalize_query_log_time) -- comparing
    differently-shaped ISO8601 timestamps as plain strings is unsafe.

    Returns whether it actually updated anything (no active binding for
    this IP at all, or seen_at wasn't newer, both return False) -- purely
    informational for the caller's own logging/counting, not required
    for correctness.
    """
    row = conn.execute(
        "SELECT id, last_seen_at FROM device_bindings WHERE ipv4_address = ? AND active = 1",
        (ipv4_address,),
    ).fetchone()
    if row is None or seen_at <= row["last_seen_at"]:
        return False
    conn.execute(
        "UPDATE device_bindings SET last_seen_at = ?, source = ? WHERE id = ?",
        (seen_at, source, row["id"]),
    )
    return True


def record_network_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    device_id: int | None = None,
    mac_address: str | None = None,
    ipv4_address: str | None = None,
    source: str = "unknown",
    observed_at: str | None = None,
    payload: dict | None = None,
) -> None:
    """Public entry point for callers (e.g. a future discovery daemon)
    to log a network/identity event directly, distinct from the
    conflict events record_binding() logs on its own."""
    _record_event(
        conn,
        event_type,
        device_id=device_id,
        mac_address=mac_address,
        ipv4_address=ipv4_address,
        source=source,
        observed_at=observed_at or db.now_iso(),
        payload=payload,
    )


def _record_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    device_id: int | None,
    mac_address: str | None,
    ipv4_address: str | None,
    source: str,
    observed_at: str,
    payload: dict | None,
) -> None:
    conn.execute(
        "INSERT INTO network_events "
        "(event_type, device_id, mac_address, ipv4_address, source, observed_at, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            event_type,
            device_id,
            mac_address,
            ipv4_address,
            source,
            observed_at,
            json.dumps(payload) if payload is not None else None,
        ),
    )
