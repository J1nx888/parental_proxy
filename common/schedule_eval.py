#!/usr/bin/env python3
"""Phase 8: time-window evaluation for `schedules` rows.

`schedule_is_active()` is pure (no DB access) and does the actual
day-of-week/time-of-day/time-zone arithmetic -- kept separate from
`is_full_lockout_active()` (which does need the DB, to enumerate
`lockout_all` schedules and check who they target) so the tricky part is
independently unit-testable without a database at all.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

# Index matches Python's datetime.weekday() (Monday=0 .. Sunday=6) --
# see schedule_is_active()'s use of this below.
_DAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse_hhmm(value: str) -> tuple[int, int]:
    """Parses a `schedules.start_time`/`end_time` value ("HH:MM"). Assumes
    well-formed input -- the dashboard's add/update routes are responsible
    for rejecting a malformed value before it ever reaches the database,
    same boundary-validation discipline as add_domain()'s regex check."""
    hour_str, _, minute_str = value.partition(":")
    return int(hour_str), int(minute_str)


def schedule_is_active(schedule_row: sqlite3.Row, now_utc: datetime) -> bool:
    """True if `now_utc` falls within `schedule_row`'s
    days_of_week/start_time/end_time window, evaluated in the schedule's
    OWN `time_zone` (never the server's local time, never bare UTC --
    "bedtime 21:00" means 21:00 in the household's zone). `now_utc` may be
    naive (assumed UTC, matching this project's own now_iso() convention)
    or tz-aware.

    Handles the overnight-wraparound case (`end_time < start_time`, e.g.
    bedtime "21:00" to "06:00") explicitly: such a window is active either
    during today's evening leg (today is a scheduled day, now is at or
    after start_time) OR during today's early-morning leg carried over
    from LAST night (yesterday was a scheduled day, now is still before
    end_time) -- so a Monday-night bedtime scheduled for "mon" alone still
    covers the Tuesday-morning hours before it ends, without needing "tue"
    listed too.
    """
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=ZoneInfo("UTC"))
    local = now_utc.astimezone(ZoneInfo(schedule_row["time_zone"]))

    days = {d.strip().lower() for d in schedule_row["days_of_week"].split(",") if d.strip()}
    start_h, start_m = _parse_hhmm(schedule_row["start_time"])
    end_h, end_m = _parse_hhmm(schedule_row["end_time"])
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    now_minutes = local.hour * 60 + local.minute
    today_code = _DAY_CODES[local.weekday()]

    if start_minutes == end_minutes:
        # Fixed 2026-09-02, a real bug found by code review: equal
        # start/end times (e.g. "00:00" to "00:00") is the natural way
        # an admin would type "block all day" -- but the same-day
        # branch below evaluates `start_minutes <= now_minutes <
        # end_minutes`, which is X <= now < X for any X, a range no
        # integer ever satisfies. That silently made a full-day
        # lockout schedule NEVER activate, on any day, with no error
        # anywhere to reveal why. Treated as "active all day on a
        # scheduled day" instead, matching what an admin who typed this
        # almost certainly meant.
        return today_code in days

    if start_minutes < end_minutes:
        # Same-day window: active only on a scheduled day, only inside
        # [start, end).
        return today_code in days and start_minutes <= now_minutes < end_minutes

    # Overnight window -- see docstring above for the two-leg logic.
    yesterday_code = _DAY_CODES[(local.weekday() - 1) % 7]
    evening_leg = today_code in days and now_minutes >= start_minutes
    morning_leg = yesterday_code in days and now_minutes < end_minutes
    return evening_leg or morning_leg


def is_full_lockout_active(conn: sqlite3.Connection, device: sqlite3.Row, now_utc: datetime) -> bool:
    """True if any `lockout_all=1` schedule is both currently active and
    targets `device` right now. Used by controller/policy_state.py's
    compute_desired_policy() as a pure computed overlay onto the nftables
    QUARANTINE set -- see that module's own comment on why this
    deliberately never writes devices.quarantined_at (a manual operator
    quarantine and a scheduled bedtime lockout stay on independent axes,
    the same separation `bump_eligible()` already established for bump vs.
    base classification)."""
    import matching  # local import: keeps schedule_is_active() usable with zero DB dependency

    for row in conn.execute("SELECT * FROM schedules WHERE lockout_all = 1"):
        if schedule_is_active(row, now_utc) and matching.schedule_applies_to_device(conn, device, row):
            return True
    return False
