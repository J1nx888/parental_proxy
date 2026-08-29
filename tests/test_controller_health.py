"""controller/health.py: interception_runtime health reporting for the
controller<->ARP-worker pipeline (Milestone 6)."""
from __future__ import annotations

from health import report_fail_open, report_healthy


def _row(conn):
    return conn.execute("SELECT * FROM interception_runtime WHERE singleton_id = 1").fetchone()


def test_report_healthy_creates_singleton_row(conn):
    report_healthy(conn, applied_generation=5)
    row = _row(conn)
    assert row["mode"] == "running"
    assert row["applied_generation"] == 5
    assert row["fail_open_reason"] is None
    assert row["last_healthy_at"] is not None


def test_report_healthy_upserts_not_duplicates(conn):
    report_healthy(conn, applied_generation=1)
    report_healthy(conn, applied_generation=2)
    count = conn.execute("SELECT COUNT(*) AS c FROM interception_runtime").fetchone()["c"]
    assert count == 1
    assert _row(conn)["applied_generation"] == 2


def test_report_fail_open_sets_mode_and_reason(conn):
    report_healthy(conn, applied_generation=3)
    report_fail_open(conn, "worker connection lost")
    row = _row(conn)
    assert row["mode"] == "fail_open"
    assert row["fail_open_reason"] == "worker connection lost"
    assert row["applied_generation"] == 3, "fail_open must not clobber the last-known-good generation"


def test_report_healthy_after_fail_open_clears_reason(conn):
    report_fail_open(conn, "transient error")
    report_healthy(conn, applied_generation=7)
    row = _row(conn)
    assert row["mode"] == "running"
    assert row["fail_open_reason"] is None
