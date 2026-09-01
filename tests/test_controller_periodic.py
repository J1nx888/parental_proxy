"""controller/periodic.py: the generic background-interval-task
primitive shared by lease.HeartbeatPacer and discovery.run_loop.
lease.HeartbeatPacer's own tests (test_controller_lease.py) already
cover the thread-lifecycle behavior this class provides -- it's now a
thin, same-interface subclass -- so these tests focus on what's
specific to using PeriodicTask directly (a custom thread_name, no
on_error given at all) rather than re-proving the same timing behavior
a second time under a different class name."""
from __future__ import annotations

import threading
import time

from periodic import PeriodicTask


def test_task_runs_on_the_given_thread_name():
    seen_name = {}

    def task():
        seen_name["name"] = threading.current_thread().name

    pt = PeriodicTask(0.02, task, thread_name="my-custom-task")
    pt.start()
    time.sleep(0.05)
    pt.stop()

    assert seen_name.get("name") == "my-custom-task"


def test_task_calls_repeatedly_until_stopped():
    calls = []
    lock = threading.Lock()

    def task():
        with lock:
            calls.append(time.monotonic())

    pt = PeriodicTask(0.02, task)
    pt.start()
    time.sleep(0.15)
    pt.stop()

    with lock:
        count = len(calls)
    assert count >= 4, f"expected several calls in 150ms at a 20ms interval, got {count}"


def test_task_with_no_on_error_swallows_exceptions_without_dying():
    calls = []
    lock = threading.Lock()

    def task():
        with lock:
            calls.append(1)
        raise RuntimeError("boom")

    # No on_error given at all -- must not raise out of the background
    # thread (which would be silently lost anyway) or stop the loop.
    pt = PeriodicTask(0.02, task)
    pt.start()
    time.sleep(0.1)
    pt.stop()

    with lock:
        count = len(calls)
    assert count >= 2, f"expected the loop to keep calling task() despite errors, got {count}"


def test_on_success_fires_after_every_non_raising_cycle():
    successes = []
    lock = threading.Lock()

    def task():
        pass

    def on_success():
        with lock:
            successes.append(1)

    pt = PeriodicTask(0.02, task, on_success=on_success)
    pt.start()
    time.sleep(0.1)
    pt.stop()

    with lock:
        count = len(successes)
    assert count >= 2, f"expected on_success to fire on repeated successful cycles, got {count}"


def test_on_success_is_not_called_when_task_raises():
    successes = []
    lock = threading.Lock()

    def task():
        raise RuntimeError("boom")

    def on_success():
        with lock:
            successes.append(1)

    pt = PeriodicTask(0.02, task, on_success=on_success)
    pt.start()
    time.sleep(0.06)
    pt.stop()

    with lock:
        count = len(successes)
    assert count == 0, "on_success must never fire for a cycle that raised"


def test_on_success_and_on_error_alternate_correctly_across_a_transition():
    """A real regression class: on_success firing for a FAILED cycle (or
    vice versa) would silently corrupt system_events.py's own
    failure/recovery transition tracking."""
    events = []
    lock = threading.Lock()
    should_fail = {"value": True}

    def task():
        if should_fail["value"]:
            raise RuntimeError("boom")

    def on_error(exc):
        with lock:
            events.append(("error", str(exc)))

    def on_success():
        with lock:
            events.append(("success",))

    pt = PeriodicTask(0.02, task, on_error=on_error, on_success=on_success)
    pt.start()
    time.sleep(0.06)
    should_fail["value"] = False
    time.sleep(0.06)
    pt.stop()

    with lock:
        snapshot = list(events)
    assert any(e[0] == "error" for e in snapshot), "expected at least one error while should_fail was True"
    assert any(e[0] == "success" for e in snapshot), "expected at least one success after should_fail flipped"
    # Every entry logged before the flip must be an error, every entry
    # after must be a success -- no interleaving/misattribution.
    first_success_index = next(i for i, e in enumerate(snapshot) if e[0] == "success")
    assert all(e[0] == "error" for e in snapshot[:first_success_index])
    assert all(e[0] == "success" for e in snapshot[first_success_index:])
