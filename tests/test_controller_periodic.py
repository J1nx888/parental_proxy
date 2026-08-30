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
