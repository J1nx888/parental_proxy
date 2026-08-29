#!/usr/bin/env python3
"""Milestone 7/8: the PolicyClass concept from RoadMap.md -- what
interception POLICY applies to a device, independent of whether it's
being ARP-spoofed at all (spoofing scope is
controller/desired_state.py's job; this is a different axis, per
docs/design/phase3-technical-design.md section 5).

Matches phase3/nftables-manager/internal/policy's four sets 1:1 --
PolicyClass here uses RoadMap.md's own naming (AUTHENTICATED/PREAUTH/
BYPASS/QUARANTINE); to_set_name() bridges to the nftables set names
(authenticated/unauthenticated/bypass/quarantine, matching
controller/policy_state.py's JSON keys and the Go side's DesiredPolicy
JSON tags exactly -- keep all three in sync if any changes).
"""
from __future__ import annotations

import enum


class PolicyClass(enum.Enum):
    AUTHENTICATED = "AUTHENTICATED"
    PREAUTH = "PREAUTH"
    BYPASS = "BYPASS"
    QUARANTINE = "QUARANTINE"


_SET_NAMES = {
    PolicyClass.AUTHENTICATED: "authenticated",
    PolicyClass.PREAUTH: "unauthenticated",
    PolicyClass.BYPASS: "bypass",
    PolicyClass.QUARANTINE: "quarantine",
}


def to_set_name(policy_class: PolicyClass) -> str:
    """The nftables set name (minus the _v4 suffix, matching
    phase3/nftables-manager's Go DesiredPolicy JSON field names) this
    PolicyClass corresponds to."""
    return _SET_NAMES[policy_class]


def classify_device(device_row) -> PolicyClass:
    """Classify one `devices` row into its PolicyClass.

    Precedence (highest first), matching the nftables prerouting
    chain's own evaluation order (phase3/nftables-manager's
    policy.AllSetNames) so a device's classification here is never
    contradicted by which rule actually fires in the kernel:

      1. BYPASS       -- `ignored` devices (the admin's own laptop, or
                          the gateway/Beelink itself entered as
                          ignored -- see controller/desired_state.py's
                          own note on why `ignored` stands in for
                          bypass_v4).
      2. QUARANTINE   -- `quarantined_at` is set (operator-triggered
                          isolation; NULL means not quarantined -- no
                          dashboard control exists to set this yet,
                          see db.py's schema comment).
      3. AUTHENTICATED / PREAUTH -- `is_authenticated` (the eventual
                          captive-portal gate's flag; defaults to 1
                          today since there's no login gate yet to fail).

    `device_row` needs `ignored`, `quarantined_at`, and
    `is_authenticated` keys -- works with a sqlite3.Row or any
    Mapping-like object providing those.
    """
    if device_row["ignored"]:
        return PolicyClass.BYPASS
    if device_row["quarantined_at"]:
        return PolicyClass.QUARANTINE
    if device_row["is_authenticated"]:
        return PolicyClass.AUTHENTICATED
    return PolicyClass.PREAUTH
