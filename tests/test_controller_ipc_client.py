"""controller/ipc_client.py: the controller-side half of the same wire
protocol implemented on the worker side by
phase3/arp-worker/internal/ipc/protocol.go. Tested against a real
connected Unix socket pair (socket.socketpair) with the "worker" side
driven manually from a background thread -- no real arp-worker process
needed, and this exercises the actual socket/JSON-framing code, not a
mock of it.
"""
from __future__ import annotations

import json
import socket
import threading

import pytest

from ipc_client import GenerationApplied, HeartbeatAck, Target, WorkerClient, WorkerError

# AF_UNIX is the actual deploy-target reality (the worker only ever runs on
# Linux -- see phase3/arp-worker), and is what CI (ubuntu-latest) exercises.
# It's absent on some Windows Python builds, so skip there instead of
# reporting a spurious local failure for something that isn't this module's
# bug.
pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX"), reason="AF_UNIX not available on this platform"
)


def _make_pair():
    client_sock, worker_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return WorkerClient(client_sock), worker_sock


def _read_line(sock) -> dict:
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError("peer closed before sending a full line")
        buf += chunk
    line, _, _ = buf.partition(b"\n")
    return json.loads(line)


def _send_line(sock, msg: dict) -> None:
    sock.sendall((json.dumps(msg) + "\n").encode())


def test_replace_targets_round_trip():
    client, worker_sock = _make_pair()
    try:
        def fake_worker():
            req = _read_line(worker_sock)
            assert req["op"] == "replace_targets"
            assert req["generation"] == 43
            assert req["gateway"] == {"ip": "192.168.1.1", "mac": "aa:bb:cc:dd:ee:01"}
            assert req["targets"] == [{"ip": "192.168.1.21", "mac": "aa:bb:cc:dd:ee:22"}]
            _send_line(worker_sock, {
                "v": 1, "op": "generation_applied", "generation": 43,
                "target_count": 1, "resolution_failures": [],
            })

        t = threading.Thread(target=fake_worker)
        t.start()
        result = client.replace_targets(
            43,
            Target(ip="192.168.1.1", mac="aa:bb:cc:dd:ee:01"),
            [Target(ip="192.168.1.21", mac="aa:bb:cc:dd:ee:22")],
        )
        t.join(timeout=2)

        assert result == GenerationApplied(generation=43, target_count=1, resolution_failures=[])
    finally:
        client.close()
        worker_sock.close()


def test_heartbeat_round_trip():
    client, worker_sock = _make_pair()
    try:
        def fake_worker():
            req = _read_line(worker_sock)
            assert req == {"v": 1, "op": "heartbeat", "sequence": 8842}
            _send_line(worker_sock, {
                "v": 1, "op": "heartbeat_ack", "sequence": 8842,
                "sent_counters": {"192.168.1.21": 118},
            })

        t = threading.Thread(target=fake_worker)
        t.start()
        result = client.heartbeat(8842)
        t.join(timeout=2)

        assert result == HeartbeatAck(sequence=8842, sent_counters={"192.168.1.21": 118})
    finally:
        client.close()
        worker_sock.close()


def test_fault_reply_raises_workererror():
    client, worker_sock = _make_pair()
    try:
        def fake_worker():
            _read_line(worker_sock)
            _send_line(worker_sock, {
                "v": 1, "op": "fault", "reason": "lease_expired",
                "action": "entering_repair_only_mode",
            })

        t = threading.Thread(target=fake_worker)
        t.start()
        with pytest.raises(WorkerError, match="lease_expired"):
            client.heartbeat(1)
        t.join(timeout=2)
    finally:
        client.close()
        worker_sock.close()


def test_unsupported_version_raises_workererror():
    client, worker_sock = _make_pair()
    try:
        def fake_worker():
            _read_line(worker_sock)
            _send_line(worker_sock, {"v": 99, "op": "heartbeat_ack", "sequence": 1})

        t = threading.Thread(target=fake_worker)
        t.start()
        with pytest.raises(WorkerError, match="version"):
            client.heartbeat(1)
        t.join(timeout=2)
    finally:
        client.close()
        worker_sock.close()


def test_unexpected_op_raises_workererror():
    """A reply that parses fine and carries the right version but the
    wrong op (e.g. a heartbeat_ack arriving in reply to replace_targets)
    must not be silently accepted as if it matched."""
    client, worker_sock = _make_pair()
    try:
        def fake_worker():
            _read_line(worker_sock)
            _send_line(worker_sock, {"v": 1, "op": "heartbeat_ack", "sequence": 1})

        t = threading.Thread(target=fake_worker)
        t.start()
        with pytest.raises(WorkerError, match="generation_applied"):
            client.replace_targets(1, Target(ip="192.168.1.1", mac="aa:bb:cc:dd:ee:01"), [])
        t.join(timeout=2)
    finally:
        client.close()
        worker_sock.close()


def test_shutdown_sends_without_waiting_for_reply():
    client, worker_sock = _make_pair()
    try:
        client.shutdown("controller_requested")
        sent = _read_line(worker_sock)
        assert sent == {"v": 1, "op": "shutdown", "reason": "controller_requested"}
    finally:
        client.close()
        worker_sock.close()


def test_worker_closing_connection_mid_request_raises():
    client, worker_sock = _make_pair()
    try:
        def fake_worker():
            _read_line(worker_sock)
            worker_sock.close()  # close without ever replying

        t = threading.Thread(target=fake_worker)
        t.start()
        with pytest.raises(WorkerError, match="closed the connection"):
            client.heartbeat(1)
        t.join(timeout=2)
    finally:
        client.close()
