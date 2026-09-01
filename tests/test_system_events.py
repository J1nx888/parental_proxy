"""common/system_events.py: the operational failure/recovery event log
that gives the dashboard's Events page (2026-09-01, added ahead of G1
real-network testing) something to show besides docker-compose-logs-only
visibility."""
from __future__ import annotations

import pytest

import system_events


def test_log_event_inserts_a_row(conn):
    system_events.log_event(conn, "adguard_sync", "error", "adguard sync failed: boom")
    row = conn.execute("SELECT * FROM system_events").fetchone()
    assert row["source"] == "adguard_sync"
    assert row["severity"] == "error"
    assert row["message"] == "adguard sync failed: boom"
    assert row["detail"] is None
    assert row["ts"]


def test_log_event_stores_optional_detail(conn):
    system_events.log_event(conn, "category_fetch", "error", "fetch failed", detail="Traceback: ...")
    row = conn.execute("SELECT detail FROM system_events").fetchone()
    assert row["detail"] == "Traceback: ..."


def test_log_event_rejects_an_invalid_severity(conn):
    with pytest.raises(ValueError, match="severity"):
        system_events.log_event(conn, "adguard_sync", "critical", "oops")
    assert conn.execute("SELECT COUNT(*) c FROM system_events").fetchone()["c"] == 0


def test_failure_recovery_callbacks_logs_every_failure_occurrence(conn):
    # failure_recovery_callbacks() opens its OWN connection internally
    # (db.get_conn(), per its own docstring) rather than accepting one
    # from the caller -- the `conn` fixture fixture already monkeypatches
    # db.DB_PATH to a real temp file, so db.get_conn() here opens a
    # genuinely separate connection to that SAME file, exactly like
    # production does. Reading back through the `conn` fixture's own
    # connection object confirms the write is really visible, not just
    # trusting the callback didn't raise.
    on_error, on_success = system_events.failure_recovery_callbacks("adguard_sync")
    on_error(RuntimeError("first"))
    on_error(RuntimeError("second"))

    rows = conn.execute("SELECT severity, message FROM system_events ORDER BY id").fetchall()
    assert len(rows) == 2
    assert all(r["severity"] == "error" for r in rows)
    assert "first" in rows[0]["message"]
    assert "second" in rows[1]["message"]


def test_failure_recovery_callbacks_logs_recovery_only_after_a_failure(conn):
    on_error, on_success = system_events.failure_recovery_callbacks("adguard_sync")

    # No prior failure -- an ordinary successful cycle must NOT log
    # anything at all (this is the whole point of "key failures/
    # recoveries only," not a firehose of every routine success).
    on_success()
    assert conn.execute("SELECT COUNT(*) c FROM system_events").fetchone()["c"] == 0

    on_error(RuntimeError("boom"))
    on_success()  # the actual recovery
    on_success()  # a second, ordinary success -- must not log again

    rows = conn.execute("SELECT severity FROM system_events ORDER BY id").fetchall()
    assert [r["severity"] for r in rows] == ["error", "recovery"]


def test_failure_recovery_callbacks_are_independent_per_source(conn):
    a_error, a_success = system_events.failure_recovery_callbacks("adguard_sync")
    b_error, b_success = system_events.failure_recovery_callbacks("category_fetch")

    a_error(RuntimeError("a failed"))
    b_success()  # b was never failing -- must not log a spurious recovery

    rows = conn.execute("SELECT source, severity FROM system_events").fetchall()
    assert [(r["source"], r["severity"]) for r in rows] == [("adguard_sync", "error")]
