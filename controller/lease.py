#!/usr/bin/env python3
"""Drives the controller->worker heartbeat on a fixed interval so the
worker's lease (phase3/arp-worker/internal/worker/lease.go) never
expires while the controller is healthy and connected.
"""
from __future__ import annotations

import threading
from typing import Callable


class HeartbeatPacer:
    """Calls `send_heartbeat()` on a background thread every `interval`
    seconds, until `stop()` is called.

    `send_heartbeat` is expected to raise on failure (a broken
    connection, a worker fault reply) -- this class does not retry or
    reconnect on its own. Reconnecting needs a fresh generation resend
    via reconcile(), not just a fresh heartbeat, so that's the caller's
    job (main.py), reported here via `on_error`.
    """

    def __init__(
        self,
        interval: float,
        send_heartbeat: Callable[[], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._interval = interval
        self._send_heartbeat = send_heartbeat
        self._on_error = on_error
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="heartbeat-pacer")
        self._thread.start()

    def stop(self) -> None:
        """Signals the pacer to stop and waits for the background thread
        to actually exit. Safe to call even if start() was never
        called."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval * 5 + 1)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._send_heartbeat()
            except Exception as exc:  # noqa: BLE001 -- deliberately broad: any
                # failure here must not kill the pacer thread silently: it
                # gets reported via on_error and the loop keeps ticking so a
                # transient failure doesn't permanently stop heartbeats.
                if self._on_error is not None:
                    self._on_error(exc)
