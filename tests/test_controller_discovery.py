"""controller/discovery.py: the periodic `ip neigh show` snapshot
discovery source."""
from __future__ import annotations

import shutil
import threading
import time

import pytest

import discovery

requires_ip_command = pytest.mark.skipif(
    shutil.which("ip") is None, reason="the `ip` command (iproute2) is not available on this platform"
)


# A representative real-world `ip neigh show` output, covering the
# shapes that matter: a normal reachable entry, a stale one (still
# trusted -- just not recently confirmed), a permanent/static one, and
# two shapes that must be SKIPPED (FAILED/INCOMPLETE carry no lladdr at
# all, matching iproute2's real documented behavior).
SAMPLE_OUTPUT = """\
192.168.1.1 dev enp1s0 lladdr aa:bb:cc:dd:ee:00 REACHABLE
192.168.1.21 dev enp1s0 lladdr aa:bb:cc:dd:ee:01 STALE
192.168.1.5 dev enp1s0 lladdr aa:bb:cc:dd:ee:05 PERMANENT
192.168.1.99 dev enp1s0  FAILED
192.168.1.100 dev enp1s0  INCOMPLETE
"""


def test_parse_extracts_only_trusted_entries():
    entries = discovery.parse_ip_neigh_output(SAMPLE_OUTPUT)
    assert entries == [
        ("192.168.1.1", "aa:bb:cc:dd:ee:00", "REACHABLE"),
        ("192.168.1.21", "aa:bb:cc:dd:ee:01", "STALE"),
        ("192.168.1.5", "aa:bb:cc:dd:ee:05", "PERMANENT"),
    ]


def test_parse_lowercases_mac_addresses():
    entries = discovery.parse_ip_neigh_output(
        "192.168.1.1 dev enp1s0 lladdr AA:BB:CC:DD:EE:00 REACHABLE\n"
    )
    assert entries[0][1] == "aa:bb:cc:dd:ee:00"


def test_parse_skips_blank_lines_and_whitespace():
    output = "\n  \n192.168.1.1 dev enp1s0 lladdr aa:bb:cc:dd:ee:00 REACHABLE\n\n"
    assert discovery.parse_ip_neigh_output(output) == [
        ("192.168.1.1", "aa:bb:cc:dd:ee:00", "REACHABLE"),
    ]


def test_parse_empty_output_returns_empty_list():
    assert discovery.parse_ip_neigh_output("") == []


def test_parse_ignores_an_unrecognized_state():
    """An explicit allowlist (not a blocklist) means a future/unexpected
    state value is skipped rather than silently trusted."""
    output = "192.168.1.1 dev enp1s0 lladdr aa:bb:cc:dd:ee:00 SOMENEWSTATE\n"
    assert discovery.parse_ip_neigh_output(output) == []


def test_snapshot_once_records_every_trusted_entry(conn, monkeypatch):
    monkeypatch.setattr(discovery, "run_ip_neigh_show", lambda: SAMPLE_OUTPUT)

    count = discovery.snapshot_once(conn)

    assert count == 3
    rows = conn.execute("SELECT mac_address, ipv4_address FROM device_bindings").fetchall()
    got = {(r["mac_address"], r["ipv4_address"]) for r in rows}
    assert got == {
        ("aa:bb:cc:dd:ee:00", "192.168.1.1"),
        ("aa:bb:cc:dd:ee:05", "192.168.1.5"),
        ("aa:bb:cc:dd:ee:01", "192.168.1.21"),
    }


def test_snapshot_once_is_idempotent_across_repeated_runs(conn, monkeypatch):
    monkeypatch.setattr(discovery, "run_ip_neigh_show", lambda: SAMPLE_OUTPUT)

    discovery.snapshot_once(conn)
    discovery.snapshot_once(conn)

    count = conn.execute("SELECT COUNT(*) AS c FROM device_bindings").fetchone()["c"]
    assert count == 3, "a repeated snapshot of the same neighbor table must not create duplicate bindings"


@requires_ip_command
def test_snapshot_once_against_the_real_ip_neigh_command(conn):
    """A light real-subprocess sanity check (no mocking) -- confirms
    run_ip_neigh_show() + the regex actually work against whatever the
    real `ip neigh show` command on this machine prints, not just the
    hand-written sample above. Content is unpredictable (depends on the
    test machine's real neighbor table) so this only asserts it runs
    without raising and returns a sane count, not specific values."""
    count = discovery.snapshot_once(conn)
    assert count >= 0


# ============================================================
# run_loop -- wiring snapshot_once() into a background PeriodicTask
# ============================================================
#
# run_loop() deliberately opens its OWN connection internally, lazily,
# the first time its background thread actually runs -- see its own
# docstring for why (a sqlite3.Connection is only usable from the thread
# that created it, so it can't be handed one built on the test's main
# thread). The `conn` fixture's monkeypatched db.DB_PATH is what lets
# that internal db.get_conn() call land on the same on-disk test DB file
# these tests then read back from via `conn` -- no connection object
# needs to be passed around here at all.

def test_run_loop_calls_snapshot_repeatedly(conn, monkeypatch):
    monkeypatch.setattr(discovery, "run_ip_neigh_show", lambda: SAMPLE_OUTPUT)
    task = discovery.run_loop(interval=0.02)
    try:
        time.sleep(0.15)
    finally:
        task.stop()

    rows = conn.execute("SELECT COUNT(*) AS c FROM device_bindings").fetchone()
    assert rows["c"] == 3, "expected the snapshot's trusted entries to have been recorded"


def test_run_loop_stops_promptly(conn, monkeypatch):
    monkeypatch.setattr(discovery, "run_ip_neigh_show", lambda: SAMPLE_OUTPUT)
    task = discovery.run_loop(interval=0.05)
    time.sleep(0.02)
    started_stop = time.monotonic()
    task.stop()
    elapsed = time.monotonic() - started_stop
    assert elapsed < 0.5, f"stop() took {elapsed:.3f}s, expected it to return promptly"


def test_run_loop_reports_snapshot_errors_via_on_error_without_dying(conn, monkeypatch):
    def _boom():
        raise RuntimeError("ip command not found")

    monkeypatch.setattr(discovery, "run_ip_neigh_show", _boom)

    errors = []
    lock = threading.Lock()

    def on_error(exc):
        with lock:
            errors.append(exc)

    task = discovery.run_loop(interval=0.02, on_error=on_error)
    try:
        time.sleep(0.1)
    finally:
        task.stop()

    with lock:
        got = len(errors)
    assert got >= 2, f"expected the loop to keep retrying (and reporting) after failures, got {got}"
