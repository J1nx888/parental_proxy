#!/usr/bin/env python3
"""Milestone 4/9 (ongoing): populates device_bindings from a periodic
`ip neigh show` snapshot -- the "periodic ip neigh snapshot (missed-
event reconciliation)" source from
docs/design/phase3-technical-design.md's discovery precedence order
(rtnetlink-first for lowest latency, this snapshot second as a
catch-all for anything missed).

This module implements ONLY the snapshot half of that precedence list
-- a live rtnetlink-event listener (the higher-precedence, lower-
latency source) is NOT built here; that needs real netlink socket
programming (e.g. via a package like pyroute2) that deserves its own
pass rather than being rushed alongside this. AdGuard query-log
correlation and active rate-limited ARP scanning (the remaining two
sources in that precedence list) also aren't built.

Nothing wires this into a running process yet -- it's deliberately
just the observation+recording piece, independently testable and usable
on its own (e.g. from a cron-style loop, or eventually folded into a
dedicated discovery daemon alongside the rtnetlink listener).
"""
from __future__ import annotations

import re
import sqlite3
import subprocess

import identity

# Matches a line like:
#   192.168.1.21 dev enp1s0 lladdr aa:bb:cc:dd:ee:01 REACHABLE
# `ip neigh show`'s output format is stable/documented (iproute2's
# `ip-neighbour(8)`), not guessed -- confirmed against real output on
# the smoke-test VM while writing this.
_NEIGH_LINE_RE = re.compile(
    r"^(?P<ip>\S+)\s+dev\s+(?P<dev>\S+)\s+lladdr\s+(?P<mac>[0-9a-fA-F:]+)\s+(?P<state>\S+)\s*$"
)

# States worth trusting as "this binding is currently real."
# FAILED/INCOMPLETE entries have no lladdr at all (the kernel never
# resolved a MAC for them), so they're already excluded by the regex
# not matching -- this set is the second filter, for entries that DO
# have an lladdr but are in a state that doesn't warrant recording
# (there currently are none excluded this way; kept as an explicit
# allowlist rather than a blocklist so a future, unexpected state
# value fails closed -- gets skipped -- rather than silently trusted).
_TRUSTED_STATES = {"REACHABLE", "STALE", "DELAY", "PROBE", "PERMANENT", "NOARP"}


def parse_ip_neigh_output(output: str) -> list[tuple[str, str, str]]:
    """Parses `ip neigh show` output into (ip, mac, state) tuples,
    skipping lines that don't match the expected shape (e.g. FAILED/
    INCOMPLETE entries, which carry no lladdr at all) or whose state
    isn't in _TRUSTED_STATES.
    """
    results = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _NEIGH_LINE_RE.match(line)
        if not m:
            continue
        state = m.group("state").upper()
        if state not in _TRUSTED_STATES:
            continue
        results.append((m.group("ip"), m.group("mac").lower(), state))
    return results


def run_ip_neigh_show() -> str:
    """Runs the real `ip neigh show` command. Split out from
    parse_ip_neigh_output() specifically so the parsing logic is unit
    testable against fixed sample output without needing to actually
    run a subprocess or depend on the test machine's real neighbor
    table having any particular content.
    """
    result = subprocess.run(
        ["ip", "neigh", "show"], capture_output=True, text=True, timeout=5, check=True
    )
    return result.stdout


def snapshot_once(conn: sqlite3.Connection) -> int:
    """Runs one real `ip neigh show` snapshot and records every
    trusted entry as an observed binding (see
    common/identity.py's record_binding). Returns the number of
    bindings recorded (not necessarily the number of NEW rows --
    record_binding is idempotent for an already-seen (mac, ip) pair).
    """
    entries = parse_ip_neigh_output(run_ip_neigh_show())
    for ip, mac, _state in entries:
        identity.record_binding(conn, mac, ip, source="snapshot")
    return len(entries)
