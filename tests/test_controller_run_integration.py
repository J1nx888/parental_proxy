"""controller/main.py: run() end-to-end, including the
reconnect-on-WorkerConnectionError path (Milestone 9). Uses a real
listening AF_UNIX socket (not a socketpair) since run() calls
WorkerClient.connect(path) itself, both for the initial connection and
for the reconnect attempt -- a socketpair can't be reconnected to by
path the way a real bind()+listen() socket can.

This is a genuine integration test: real signals (run() must be
invoked from the main thread, since signal.signal() only works there),
real threads (the heartbeat pacer running concurrently with the main
loop -- exactly the condition that surfaced the need for WorkerClient's
internal lock, see ipc_client.py's own note), and a real simulated
worker crash-and-restart.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import threading
import time

import pytest

from ipc_client import Target
from main import run
from reconcile import DesiredState

af_unix_only = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="AF_UNIX not available on this platform"
)
pytestmark = af_unix_only

GATEWAY = Target(ip="192.168.1.1", mac="aa:bb:cc:dd:ee:00")


def _read_line(sock) -> dict:
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("peer closed before a full line arrived")
        buf += chunk
    line, _, _ = buf.partition(b"\n")
    return json.loads(line)


def _send_line(sock, msg: dict) -> None:
    sock.sendall((json.dumps(msg) + "\n").encode())


def _desired_state_provider() -> DesiredState:
    return DesiredState(gateway=GATEWAY, targets=(Target(ip="192.168.1.21", mac="aa:bb:cc:dd:ee:01"),))


@pytest.fixture
def restore_signal_handlers():
    """run() installs its own SIGTERM/SIGINT handlers for the duration
    of the call, which is exactly what it's supposed to do -- but a
    test process shouldn't leave those handlers in place for whatever
    runs after it in the same pytest session."""
    original_term = signal.getsignal(signal.SIGTERM)
    original_int = signal.getsignal(signal.SIGINT)
    yield
    signal.signal(signal.SIGTERM, original_term)
    signal.signal(signal.SIGINT, original_int)


def test_run_reconnects_after_worker_dies_then_shuts_down_cleanly(tmp_path, conn, restore_signal_handlers):
    sock_path = str(tmp_path / "worker.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    server.settimeout(0.5)

    connection_count = 0
    stop_server = threading.Event()

    def serve():
        nonlocal connection_count
        while not stop_server.is_set():
            try:
                conn_sock, _ = server.accept()
            except socket.timeout:
                continue
            connection_count += 1
            first_connection = connection_count == 1
            conn_sock.settimeout(2)
            try:
                if first_connection:
                    # Simulate the worker process crashing: read exactly
                    # one request, then die without ever replying.
                    _read_line(conn_sock)
                else:
                    # Behave normally on the reconnected connection.
                    while True:
                        try:
                            req = _read_line(conn_sock)
                        except (ConnectionError, socket.timeout):
                            break
                        if req["op"] == "replace_targets":
                            _send_line(conn_sock, {
                                "v": 1, "op": "generation_applied",
                                "generation": req["generation"],
                                "target_count": len(req["targets"]),
                                "resolution_failures": [],
                            })
                        elif req["op"] == "heartbeat":
                            _send_line(conn_sock, {
                                "v": 1, "op": "heartbeat_ack",
                                "sequence": req["sequence"], "sent_counters": {},
                            })
                        elif req["op"] == "shutdown":
                            break
            except (ConnectionError, OSError, socket.timeout):
                pass
            finally:
                conn_sock.close()

    server_thread = threading.Thread(target=serve)
    server_thread.start()

    def send_sigterm_after_delay():
        time.sleep(1.2)
        os.kill(os.getpid(), signal.SIGTERM)

    timer_thread = threading.Thread(target=send_sigterm_after_delay)
    timer_thread.start()

    try:
        run(
            sock_path,
            _desired_state_provider,
            heartbeat_interval=0.15,
            poll_interval=0.15,
            health_conn=conn,
            policy_conn=None,
        )
    finally:
        stop_server.set()
        server_thread.join(timeout=3)
        timer_thread.join(timeout=3)
        server.close()

    assert connection_count >= 2, (
        f"expected run() to reconnect after the first connection died, "
        f"saw only {connection_count} connection(s)"
    )

    row = conn.execute("SELECT mode FROM interception_runtime WHERE singleton_id = 1").fetchone()
    assert row is not None
    assert row["mode"] == "running", (
        "expected the reconnected cycle to eventually report healthy again"
    )
