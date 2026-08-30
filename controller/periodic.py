#!/usr/bin/env python3
"""Generic "call this on a fixed interval, in the background, until
stopped" primitive.

Factored out of controller/lease.py's HeartbeatPacer (Milestone 3) when
controller/discovery.py's discovery loop (Milestone 4) needed the exact
same thread-lifecycle/error-handling shape: a background thread that
must stop promptly, and that reports -- rather than dies from -- a
failing callback. HeartbeatPacer is now a thin, same-interface subclass
of this; see its own docstring.
"""
from __future__ import annotations

import threading
from typing import Callable


class PeriodicTask:
    """Calls `task()` on a background thread every `interval` seconds,
    until `stop()` is called.

    `task` is expected to raise on failure -- this class does not retry
    beyond "try again next interval," and does not reconnect/repair
    anything on the caller's behalf. A raised exception is reported via
    `on_error` (if given) rather than propagating, so one bad cycle never
    kills the background thread.
    """

    def __init__(
        self,
        interval: float,
        task: Callable[[], None],
        on_error: Callable[[Exception], None] | None = None,
        *,
        thread_name: str = "periodic-task",
    ) -> None:
        self._interval = interval
        self._task = task
        self._on_error = on_error
        self._thread_name = thread_name
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name=self._thread_name)
        self._thread.start()

    def stop(self) -> None:
        """Signals the task to stop and waits for the background thread
        to actually exit. Safe to call even if start() was never
        called."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 5 + 1)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._task()
            except Exception as exc:  # noqa: BLE001 -- deliberately broad: any
                # failure here must not kill the loop silently: it gets
                # reported via on_error and the loop keeps ticking so a
                # transient failure doesn't permanently stop the task.
                if self._on_error is not None:
                    self._on_error(exc)
