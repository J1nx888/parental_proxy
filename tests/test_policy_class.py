"""common/policy_class.py: PolicyClass classification precedence."""
from __future__ import annotations

import pytest

from policy_class import PolicyClass, bump_eligible, classify_device, to_set_name


def _row(ignored=0, quarantined_at=None, is_authenticated=1, bump_enabled=0):
    return {
        "ignored": ignored,
        "quarantined_at": quarantined_at,
        "is_authenticated": is_authenticated,
        "bump_enabled": bump_enabled,
    }


def test_ignored_device_is_bypass_regardless_of_other_flags():
    row = _row(ignored=1, quarantined_at="2026-08-29T00:00:00Z", is_authenticated=0)
    assert classify_device(row) == PolicyClass.BYPASS


def test_quarantined_device_is_quarantine():
    row = _row(quarantined_at="2026-08-29T00:00:00Z", is_authenticated=1)
    assert classify_device(row) == PolicyClass.QUARANTINE


def test_authenticated_device_with_no_overrides():
    assert classify_device(_row(is_authenticated=1)) == PolicyClass.AUTHENTICATED


def test_unauthenticated_device_is_preauth():
    assert classify_device(_row(is_authenticated=0)) == PolicyClass.PREAUTH


def test_bypass_beats_quarantine():
    row = _row(ignored=1, quarantined_at="2026-08-29T00:00:00Z")
    assert classify_device(row) == PolicyClass.BYPASS


def test_quarantine_beats_authentication_state():
    row = _row(quarantined_at="2026-08-29T00:00:00Z", is_authenticated=1)
    assert classify_device(row) == PolicyClass.QUARANTINE
    row2 = _row(quarantined_at="2026-08-29T00:00:00Z", is_authenticated=0)
    assert classify_device(row2) == PolicyClass.QUARANTINE


@pytest.mark.parametrize(
    "policy_class,expected",
    [
        (PolicyClass.AUTHENTICATED, "authenticated"),
        (PolicyClass.PREAUTH, "unauthenticated"),
        (PolicyClass.BYPASS, "bypass"),
        (PolicyClass.QUARANTINE, "quarantine"),
    ],
)
def test_to_set_name(policy_class, expected):
    assert to_set_name(policy_class) == expected


def test_bump_eligible_true_for_authenticated_device_with_flag_set():
    assert bump_eligible(_row(is_authenticated=1, bump_enabled=1)) is True


def test_bump_eligible_false_when_flag_not_set():
    assert bump_eligible(_row(is_authenticated=1, bump_enabled=0)) is False


def test_bump_eligible_false_for_preauth_device_even_with_flag_set():
    # A device that hasn't logged in yet has no DNS-tier access at all --
    # it can't be bump-eligible before that, per RoadMap.md's Phase 4 flow.
    assert bump_eligible(_row(is_authenticated=0, bump_enabled=1)) is False


def test_bump_eligible_false_for_bypass_device_even_with_flag_set():
    # An ignored device's traffic must never be forced through Squid --
    # bypass means "outside the whole system, for good."
    assert bump_eligible(_row(ignored=1, bump_enabled=1)) is False


def test_bump_eligible_false_for_quarantined_device_even_with_flag_set():
    row = _row(quarantined_at="2026-08-29T00:00:00Z", bump_enabled=1)
    assert bump_eligible(row) is False
