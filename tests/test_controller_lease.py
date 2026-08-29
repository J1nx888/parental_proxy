"""controller/lease.py: the heartbeat pacer that keeps the worker's
lease (phase3/arp-worker/internal/worker/lease.go) alive. Timing-based
tests with generous margins -- short intervals over a longer observation
window, same approach used by the Go worker's own lease tests."""
from __future__ import annotations

import threading
import time

from lease import HeartbeatPacer


def test_pacer_calls_send_heartbeat_repeatedly():
    calls = []
    lock = threading.Lock()

    def send():
        with lock:
            calls.append(time.monotonic())

    pacer = HeartbeatPacer(interval=0.02, send_heartbeat=send)
    pacer.start()
    time.sleep(0.15)
    pacer.stop()

    with lock:
        count = len(calls)
    assert count >= 4, f"expected several heartbeats in 150ms at a 20ms interval, got {count}"


def test_pacer_stops_promptly():
    pacer = HeartbeatPacer(interval=0.05, send_heartbeat=lambda: None)
    pacer.start()
    time.sleep(0.02)
    started_stop = time.monotonic()
    pacer.stop()
    elapsed = time.monotonic() - started_stop
    assert elapsed < 0.5, f"stop() took {elapsed:.3f}s, expected it to return promptly"


def test_pacer_no_heartbeats_after_stop():
    calls = []
    lock = threading.Lock()

    def send():
        with lock:
            calls.append(1)

    pacer = HeartbeatPacer(interval=0.02, send_heartbeat=send)
    pacer.start()
    time.sleep(0.05)
    pacer.stop()
    with lock:
        count_at_stop = len(calls)

    time.sleep(0.08)
    with lock:
        count_after_wait = len(calls)

    assert count_after_wait == count_at_stop, (
        "pacer kept calling send_heartbeat after stop() returned"
    )


def test_pacer_reports_send_errors_via_on_error_without_dying():
    errors = []
    lock = threading.Lock()

    def send():
        raise RuntimeError("boom")

    def on_error(exc):
        with lock:
            errors.append(exc)

    pacer = HeartbeatPacer(interval=0.02, send_heartbeat=send, on_error=on_error)
    pacer.start()
    time.sleep(0.1)
    pacer.stop()

    with lock:
        got = len(errors)
    assert got >= 2, f"expected the pacer to keep running (and reporting) after send errors, got {got}"


def test_pacer_stop_before_start_is_safe():
    pacer = HeartbeatPacer(interval=0.05, send_heartbeat=lambda: None)
    pacer.stop()  # must not raise even though start() was never called
