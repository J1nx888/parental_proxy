#!/usr/bin/env python3
"""Milestone 7: builds the DesiredPolicy JSON blob that
phase3/nftables-manager reads directly from the shared SQLite database
-- following this project's own established "one shared database, live
reads, no separate sync" pattern (see docs/project.md's Key technical
decisions) rather than inventing a new controller<->nftables-manager
IPC protocol.
"""
from __future__ import annotations

import json
import sqlite3

from policy_class import PolicyClass, bump_eligible, classify_device, to_set_name

# The nftables-manager side's fifth, independent set (policy.SetBump) --
# not one of PolicyClass's four mutually-exclusive values, so it isn't
# in to_set_name()'s table. Kept here, next to the JSON key literals
# this module already owns, rather than in policy_class.py, since
# policy_class.py's to_set_name() contract is specifically "PolicyClass
# -> set name" and bump isn't a PolicyClass.
_BUMP_SET_NAME = "bump"


def compute_desired_policy(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """One entry per PolicyClass's nftables set name, each holding the
    IPv4 addresses of every device currently classified into it, PLUS
    an independent `"bump"` entry (RoadMap.md's "two independent axes"
    section, locked 2026-08-30) for devices with bump_eligible() true.
    An IP can legitimately appear in both `"authenticated"` and
    `"bump"` at once -- bump is a refinement layered on top of
    authenticated access, not a fifth exclusive class, so it is
    computed independently rather than via classify_device().

    A device with more than one simultaneously-active binding (see
    common/identity.py's own notes on how that can briefly happen)
    contributes each of its active IPs -- unlike
    controller/desired_state.py's ARP-poisoning target list, there's no
    reason to pick only the freshest one here: every IP currently
    routed through this device's identity should get that device's
    policy. Devices with no active binding contribute nothing -- there's
    no IP to add to any set.
    """
    policy: dict[str, list[str]] = {to_set_name(pc): [] for pc in PolicyClass}
    policy[_BUMP_SET_NAME] = []

    rows = conn.execute(
        """
        SELECT d.ignored, d.quarantined_at, d.is_authenticated, d.bump_enabled,
               d.bypass_login, b.ipv4_address
        FROM devices d
        JOIN device_bindings b ON b.device_id = d.id AND b.active = 1
        """
    ).fetchall()

    for row in rows:
        policy[to_set_name(classify_device(row))].append(row["ipv4_address"])
        if bump_eligible(row):
            policy[_BUMP_SET_NAME].append(row["ipv4_address"])

    for ips in policy.values():
        ips.sort()

    return policy


def write_desired_policy(conn: sqlite3.Connection, policy: dict[str, list[str]]) -> None:
    """Persists the computed policy into interception_runtime's
    singleton row (upserting it into existence on first write --
    interception_runtime has no seed row in db.py's SCHEMA)."""
    payload = json.dumps(policy, sort_keys=True)
    conn.execute(
        "INSERT INTO interception_runtime (singleton_id, desired_policy_json) VALUES (1, ?) "
        "ON CONFLICT(singleton_id) DO UPDATE SET desired_policy_json = excluded.desired_policy_json",
        (payload,),
    )
    conn.commit()
