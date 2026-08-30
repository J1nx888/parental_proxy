#!/usr/bin/env python3
"""Milestone 7/8: the PolicyClass concept from RoadMap.md -- what
interception POLICY applies to a device, independent of whether it's
being ARP-spoofed at all (spoofing scope is
controller/desired_state.py's job; this is a different axis, per
docs/design/phase3-technical-design.md section 5).

Matches phase3/nftables-manager/internal/policy's four mutually-exclusive
sets 1:1 -- PolicyClass here uses RoadMap.md's own naming (AUTHENTICATED/
PREAUTH/BYPASS/QUARANTINE); to_set_name() bridges to the nftables set names
(authenticated/unauthenticated/bypass/quarantine, matching
controller/policy_state.py's JSON keys and the Go side's DesiredPolicy
JSON tags exactly -- keep all three in sync if any changes).

`bump_eligible()` below is a SEPARATE, independent axis added for the
"two independent axes" architecture correction locked in RoadMap.md
2026-08-30: devices.bump_enabled is a per-device, admin-only opt-in for
Squid/SSL-bump refinement, layered on top of an already-authenticated
device -- not a fifth PolicyClass value. A device can be AUTHENTICATED
*and* bump_eligible at once; see bump_eligible()'s own doc comment for
why it is never true for any other PolicyClass.
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


def bump_eligible(device_row) -> bool:
    """Whether this device should be a member of nftables' bump_v4 set
    -- the independent, orthogonal opt-in for Squid/SSL-bump refinement
    (RoadMap.md's "two independent axes" section, locked 2026-08-30).

    Deliberately re-derives PolicyClass instead of trusting
    `device_row["bump_enabled"]` alone: the hard-deny invariant this
    project settled on requires that bump-tier only ever composes with
    an actually-authenticated device, never with BYPASS, QUARANTINE, or
    PREAUTH -- e.g. an ignored (BYPASS) device's traffic must never be
    forced through Squid just because an admin once also checked
    bump_enabled on it, and a not-yet-logged-in (PREAUTH) device can't
    be bump-eligible before it even has DNS-tier access. Same
    `device_row` shape as classify_device() plus `bump_enabled`.
    """
    return bool(device_row["bump_enabled"]) and classify_device(device_row) == PolicyClass.AUTHENTICATED
