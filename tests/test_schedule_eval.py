"""Phase 8: common/schedule_eval.py -- pure day/time/timezone evaluation,
plus is_full_lockout_active()'s thin DB-touching wrapper."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import schedule_eval


def _schedule(**overrides):
    row = {
        "days_of_week": "mon,tue,wed,thu,fri",
        "start_time": "08:00",
        "end_time": "15:00",
        "time_zone": "UTC",
        "lockout_all": 0,
        "is_global": 1,
    }
    row.update(overrides)
    return row


def test_same_day_window_active_inside_range():
    # Monday 2026-08-31 is a Monday.
    now = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
    assert schedule_eval.schedule_is_active(_schedule(), now) is True


def test_same_day_window_inactive_before_start():
    now = datetime(2026, 8, 31, 7, 59, tzinfo=timezone.utc)
    assert schedule_eval.schedule_is_active(_schedule(), now) is False


def test_same_day_window_inactive_at_end_boundary():
    # end_time is exclusive.
    now = datetime(2026, 8, 31, 15, 0, tzinfo=timezone.utc)
    assert schedule_eval.schedule_is_active(_schedule(), now) is False


def test_same_day_window_inactive_on_unscheduled_day():
    # 2026-09-05 is a Saturday -- not in "mon,tue,wed,thu,fri".
    now = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)
    assert schedule_eval.schedule_is_active(_schedule(), now) is False


def test_overnight_window_active_in_evening_leg():
    bedtime = _schedule(days_of_week="mon", start_time="21:00", end_time="06:00")
    now = datetime(2026, 8, 31, 22, 0, tzinfo=timezone.utc)  # Monday 22:00
    assert schedule_eval.schedule_is_active(bedtime, now) is True


def test_overnight_window_active_in_morning_leg_next_day():
    bedtime = _schedule(days_of_week="mon", start_time="21:00", end_time="06:00")
    now = datetime(2026, 9, 1, 5, 30, tzinfo=timezone.utc)  # Tuesday 05:30, carried over from Monday
    assert schedule_eval.schedule_is_active(bedtime, now) is True


def test_overnight_window_inactive_after_morning_leg_ends():
    bedtime = _schedule(days_of_week="mon", start_time="21:00", end_time="06:00")
    now = datetime(2026, 9, 1, 6, 0, tzinfo=timezone.utc)  # Tuesday 06:00 -- end is exclusive
    assert schedule_eval.schedule_is_active(bedtime, now) is False


def test_overnight_window_inactive_daytime_with_no_carryover():
    # Sunday was NOT a scheduled day, so Monday 05:00 has no prior-night leg.
    bedtime = _schedule(days_of_week="mon", start_time="21:00", end_time="06:00")
    now = datetime(2026, 8, 31, 5, 0, tzinfo=timezone.utc)  # Monday 05:00
    assert schedule_eval.schedule_is_active(bedtime, now) is False


def test_naive_datetime_treated_as_utc():
    now = datetime(2026, 8, 31, 10, 0)  # no tzinfo
    assert schedule_eval.schedule_is_active(_schedule(), now) is True


def test_timezone_conversion_shifts_the_active_window():
    # 08:00-15:00 in America/Chicago (UTC-5 in August, CDT) is 13:00-20:00 UTC.
    sched = _schedule(time_zone="America/Chicago")
    still_utc_morning = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
    assert schedule_eval.schedule_is_active(sched, still_utc_morning) is False
    now_in_window = datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc)
    assert schedule_eval.schedule_is_active(sched, now_in_window) is True


def test_timezone_can_shift_which_local_calendar_day_it_is():
    # 2026-08-31 22:30 UTC is already 2026-09-01 (Tuesday) 08:30 in
    # Australia/Sydney (UTC+10 in the southern winter) -- a schedule that
    # only fires Tuesdays should be active here even though the UTC
    # instant itself is still Monday.
    sched = _schedule(
        days_of_week="tue", start_time="08:00", end_time="09:00", time_zone="Australia/Sydney"
    )
    now = datetime(2026, 8, 31, 22, 30, tzinfo=timezone.utc)
    assert schedule_eval.schedule_is_active(sched, now) is True


def test_dst_spring_forward_transition_day_still_evaluates():
    # 2027-03-14 is a US DST spring-forward day (America/Chicago) -- just
    # confirming this doesn't raise and evaluates a normal, non-skipped
    # window sanely (02:00-02:30 local is the hour that gets skipped, so
    # deliberately testing a window that doesn't touch it).
    sched = _schedule(days_of_week="sun", start_time="08:00", end_time="09:00", time_zone="America/Chicago")
    now = datetime(2027, 3, 14, 13, 30, tzinfo=timezone.utc)  # 08:30 CDT
    assert schedule_eval.schedule_is_active(sched, now) is True


# --- is_full_lockout_active() -----------------------------------------

def _insert_schedule(conn, name, *, lockout_all=1, is_global=1, days="mon,tue,wed,thu,fri,sat,sun",
                      start="21:00", end="06:00", tz="UTC"):
    conn.execute(
        "INSERT INTO schedules (name, days_of_week, start_time, end_time, time_zone, "
        "lockout_all, is_global, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))",
        (name, days, start, end, tz, int(lockout_all), int(is_global)),
    )
    conn.commit()
    return conn.execute("SELECT id FROM schedules WHERE name = ?", (name,)).fetchone()["id"]


def _insert_device(conn, mac, *, ignored=0):
    conn.execute(
        "INSERT INTO devices (mac_address, ignored, is_authenticated, created_at) "
        "VALUES (?, ?, 1, datetime('now'))",
        (mac, int(ignored)),
    )
    conn.commit()
    return conn.execute("SELECT * FROM devices WHERE mac_address = ?", (mac,)).fetchone()


def test_is_full_lockout_active_true_for_global_schedule_during_window(conn):
    _insert_schedule(conn, "Bedtime")
    device = _insert_device(conn, "aa:bb:cc:dd:ee:01")
    now = datetime(2026, 8, 31, 22, 0, tzinfo=timezone.utc)
    assert schedule_eval.is_full_lockout_active(conn, device, now) is True


def test_is_full_lockout_active_false_outside_window(conn):
    _insert_schedule(conn, "Bedtime", days="mon", start="21:00", end="06:00")
    device = _insert_device(conn, "aa:bb:cc:dd:ee:02")
    now = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)  # Monday noon -- not bedtime
    assert schedule_eval.is_full_lockout_active(conn, device, now) is False


def test_is_full_lockout_active_false_when_lockout_all_is_zero(conn):
    _insert_schedule(conn, "School hours", lockout_all=0, days="mon", start="00:00", end="23:59")
    device = _insert_device(conn, "aa:bb:cc:dd:ee:03")
    now = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
    assert schedule_eval.is_full_lockout_active(conn, device, now) is False
