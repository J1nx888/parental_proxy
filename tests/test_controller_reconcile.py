"""controller/reconcile.py: idempotent reconciliation logic."""
from __future__ import annotations

from ipc_client import Target
from reconcile import AppliedState, DesiredState, reconcile

GW = Target(ip="192.168.1.1", mac="aa:bb:cc:dd:ee:01")
T1 = Target(ip="192.168.1.21", mac="aa:bb:cc:dd:ee:22")
T2 = Target(ip="192.168.1.22", mac="aa:bb:cc:dd:ee:23")


def test_first_reconcile_always_produces_generation_one():
    desired = DesiredState(gateway=GW, targets=(T1,))
    assert reconcile(None, desired) == 1


def test_unchanged_desired_state_is_a_no_op():
    desired = DesiredState(gateway=GW, targets=(T1, T2))
    applied = AppliedState(generation=5, desired=desired)
    assert reconcile(applied, desired) is None


def test_target_order_does_not_matter():
    """A DB read returning the same devices in a different row order must
    not spuriously trigger a new generation -- see
    DesiredState.target_key's doc comment."""
    applied = AppliedState(generation=5, desired=DesiredState(gateway=GW, targets=(T1, T2)))
    reordered = DesiredState(gateway=GW, targets=(T2, T1))
    assert reconcile(applied, reordered) is None


def test_added_target_bumps_generation():
    applied = AppliedState(generation=5, desired=DesiredState(gateway=GW, targets=(T1,)))
    desired = DesiredState(gateway=GW, targets=(T1, T2))
    assert reconcile(applied, desired) == 6


def test_removed_target_bumps_generation():
    applied = AppliedState(generation=5, desired=DesiredState(gateway=GW, targets=(T1, T2)))
    desired = DesiredState(gateway=GW, targets=(T1,))
    assert reconcile(applied, desired) == 6


def test_full_duplex_change_bumps_generation():
    applied = AppliedState(
        generation=5, desired=DesiredState(gateway=GW, targets=(T1,), full_duplex=False)
    )
    desired = DesiredState(gateway=GW, targets=(T1,), full_duplex=True)
    assert reconcile(applied, desired) == 6


def test_gateway_mac_change_bumps_generation():
    """Same gateway IP, different MAC -- e.g. the router itself was
    replaced/rebooted with a new NIC. Must not be treated as unchanged
    just because the IP matches."""
    other_gw = Target(ip="192.168.1.1", mac="ff:ff:ff:ff:ff:ff")
    applied = AppliedState(generation=5, desired=DesiredState(gateway=GW, targets=(T1,)))
    desired = DesiredState(gateway=other_gw, targets=(T1,))
    assert reconcile(applied, desired) == 6


def test_generation_increments_from_current_not_reset():
    applied = AppliedState(generation=42, desired=DesiredState(gateway=GW, targets=(T1,)))
    desired = DesiredState(gateway=GW, targets=(T2,))
    assert reconcile(applied, desired) == 43
