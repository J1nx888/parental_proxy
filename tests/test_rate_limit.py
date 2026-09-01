"""common/rate_limit.py -- the shared, in-memory sliding-window limiter
used by both dashboard/dashboard.py's admin login and
dashboard/captive_portal_server.py's kid-login/portal-admin-action forms.
Pure logic, no DB/HTTP involved.
"""
from __future__ import annotations

import time

import pytest

from rate_limit import RateLimiter


def test_a_fresh_key_is_not_limited():
    limiter = RateLimiter(max_attempts=5, window_seconds=60.0)
    assert limiter.is_limited("1.2.3.4") is False


def test_becomes_limited_after_max_attempts():
    limiter = RateLimiter(max_attempts=3, window_seconds=60.0)
    key = "1.2.3.4"
    for _ in range(3):
        limiter.record_failure(key)
    assert limiter.is_limited(key) is True


def test_stays_unlimited_one_short_of_the_max():
    limiter = RateLimiter(max_attempts=3, window_seconds=60.0)
    key = "1.2.3.4"
    for _ in range(2):
        limiter.record_failure(key)
    assert limiter.is_limited(key) is False


def test_clear_resets_a_limited_key():
    limiter = RateLimiter(max_attempts=3, window_seconds=60.0)
    key = "1.2.3.4"
    for _ in range(3):
        limiter.record_failure(key)
    assert limiter.is_limited(key) is True

    limiter.clear(key)

    assert limiter.is_limited(key) is False


def test_clearing_one_key_does_not_affect_another():
    limiter = RateLimiter(max_attempts=2, window_seconds=60.0)
    for _ in range(2):
        limiter.record_failure("1.2.3.4")
        limiter.record_failure("5.6.7.8")

    limiter.clear("1.2.3.4")

    assert limiter.is_limited("1.2.3.4") is False
    assert limiter.is_limited("5.6.7.8") is True, "clearing one key must never clear a different one"


def test_failures_outside_the_window_expire():
    limiter = RateLimiter(max_attempts=2, window_seconds=0.05)
    key = "1.2.3.4"
    limiter.record_failure(key)
    limiter.record_failure(key)
    assert limiter.is_limited(key) is True

    time.sleep(0.1)

    assert limiter.is_limited(key) is False, "attempts older than window_seconds must no longer count"


def test_a_fresh_failure_after_the_window_does_not_immediately_relimit():
    # Only 1 failure survives the window this time (the other two expired),
    # so it alone must not be enough to trip a max_attempts=2 limiter.
    limiter = RateLimiter(max_attempts=2, window_seconds=0.05)
    key = "1.2.3.4"
    limiter.record_failure(key)
    limiter.record_failure(key)
    time.sleep(0.1)
    limiter.record_failure(key)

    assert limiter.is_limited(key) is False


def test_two_limiter_instances_never_share_state():
    # dashboard.py and captive_portal_server.py each hold their OWN
    # module-level instance deliberately (see rate_limit.py's own
    # docstring) -- a flood against one surface must never exhaust a
    # completely different surface's budget.
    a = RateLimiter(max_attempts=1, window_seconds=60.0)
    b = RateLimiter(max_attempts=1, window_seconds=60.0)
    a.record_failure("1.2.3.4")
    assert a.is_limited("1.2.3.4") is True
    assert b.is_limited("1.2.3.4") is False


@pytest.mark.parametrize("max_attempts", [1, 2, 5, 10])
def test_max_attempts_boundary_is_exact(max_attempts):
    limiter = RateLimiter(max_attempts=max_attempts, window_seconds=60.0)
    key = "1.2.3.4"
    for _ in range(max_attempts - 1):
        limiter.record_failure(key)
    assert limiter.is_limited(key) is False
    limiter.record_failure(key)
    assert limiter.is_limited(key) is True
