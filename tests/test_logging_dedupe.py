"""common/logging_util.py: deduped access-log writer."""
from __future__ import annotations

import db
import logging_util


def _rows(conn):
    return conn.execute("SELECT * FROM access_log ORDER BY id").fetchall()


def test_first_call_inserts_a_row(conn):
    logging_util.log_access(
        conn, user_id=1, username="kid1", domain="crunchyroll.com",
        path=None, allowed=True, reason="global_domain",
    )
    rows = _rows(conn)
    assert len(rows) == 1
    assert rows[0]["username"] == "kid1"
    assert rows[0]["allowed"] == 1


def test_identical_call_within_window_is_suppressed(conn):
    kwargs = dict(
        user_id=1, username="kid1", domain="crunchyroll.com",
        path=None, allowed=False, reason="not_authenticated",
    )
    logging_util.log_access(conn, **kwargs)
    logging_util.log_access(conn, **kwargs)
    logging_util.log_access(conn, **kwargs)
    assert len(_rows(conn)) == 1


def test_different_series_id_is_not_deduped(conn):
    base = dict(user_id=1, username="kid1", domain="crunchyroll.com", path="/watch/x", allowed=False)
    logging_util.log_access(conn, reason="show_not_approved", series_id="AAA111", **base)
    logging_util.log_access(conn, reason="show_not_approved", series_id="BBB222", **base)
    assert len(_rows(conn)) == 2


def test_different_series_id_none_vs_value_is_not_deduped(conn):
    base = dict(user_id=1, username="kid1", domain="crunchyroll.com", path="/", allowed=True)
    logging_util.log_access(conn, reason="global_domain", series_id=None, **base)
    logging_util.log_access(conn, reason="show_approved", series_id="AAA111", **base)
    assert len(_rows(conn)) == 2


def test_different_allowed_value_is_not_deduped(conn):
    base = dict(user_id=1, username="kid1", domain="crunchyroll.com", path=None)
    logging_util.log_access(conn, allowed=True, reason="global_domain", **base)
    logging_util.log_access(conn, allowed=False, reason="user_domain", **base)
    assert len(_rows(conn)) == 2


def test_window_expiry_allows_a_new_row(conn):
    kwargs = dict(
        user_id=1, username="kid1", domain="crunchyroll.com",
        path=None, allowed=False, reason="not_authenticated",
    )
    logging_util.log_access(conn, **kwargs)
    assert len(_rows(conn)) == 1

    # Backdate the existing row past the dedupe window.
    stale_ts = db.iso_secs_ago(logging_util.DEDUPE_WINDOW_SECONDS + 60)
    conn.execute("UPDATE access_log SET ts = ?", (stale_ts,))
    conn.commit()

    logging_util.log_access(conn, **kwargs)
    assert len(_rows(conn)) == 2
