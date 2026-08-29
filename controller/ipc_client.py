#!/usr/bin/env python3
"""Controller-side client for the ARP worker's IPC protocol.

Wire format matches phase3/arp-worker/internal/ipc/protocol.go exactly:
versioned JSON over a Unix domain socket, one newline-delimited frame per
message. Keep the two in sync if either changes.
"""
from __future__ import annotations

import dataclasses
import json
import socket
from typing import Any

PROTOCOL_VERSION = 1


class WorkerError(RuntimeError):
    """Raised when the worker replies with a "fault" message, replies with
    an unsupported protocol version, or the connection breaks mid-request
    (EOF before a full frame, malformed JSON)."""


@dataclasses.dataclass(frozen=True)
class Target:
    ip: str
    mac: str

    def to_wire(self) -> dict[str, str]:
        return {"ip": self.ip, "mac": self.mac}


@dataclasses.dataclass(frozen=True)
class GenerationApplied:
    generation: int
    target_count: int
    resolution_failures: list[str]


@dataclasses.dataclass(frozen=True)
class HeartbeatAck:
    sequence: int
    sent_counters: dict[str, int]


class WorkerClient:
    """One connection to the arp-worker's Unix socket.

    Not thread-safe, and not meant to be used from more than one thread
    at once for request/reply calls -- this matches the worker's own
    "single connection at a time" design (see
    phase3/arp-worker/internal/ipc/server.go's doc comment). The one
    exception is that heartbeat() is safe to call from a background
    pacer thread (see controller/lease.py) as long as nothing else on
    this client is in flight concurrently -- callers are responsible for
    that serialization (main.py's reconciliation loop and the heartbeat
    pacer never overlap by construction).
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = b""

    @classmethod
    def connect(cls, path: str, timeout: float = 5.0) -> "WorkerClient":
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(path)
        return cls(sock)

    def close(self) -> None:
        self._sock.close()

    def replace_targets(
        self,
        generation: int,
        gateway: Target,
        targets: list[Target],
        full_duplex: bool = False,
    ) -> GenerationApplied:
        reply = self._request(
            {
                "v": PROTOCOL_VERSION,
                "op": "replace_targets",
                "generation": generation,
                "gateway": gateway.to_wire(),
                "targets": [t.to_wire() for t in targets],
                "full_duplex": full_duplex,
            }
        )
        self._expect_op(reply, "generation_applied")
        return GenerationApplied(
            generation=reply["generation"],
            target_count=reply["target_count"],
            resolution_failures=reply.get("resolution_failures") or [],
        )

    def heartbeat(self, sequence: int) -> HeartbeatAck:
        reply = self._request({"v": PROTOCOL_VERSION, "op": "heartbeat", "sequence": sequence})
        self._expect_op(reply, "heartbeat_ack")
        return HeartbeatAck(
            sequence=reply["sequence"], sent_counters=reply.get("sent_counters") or {}
        )

    def shutdown(self, reason: str) -> None:
        """Sends "shutdown" and does not wait for a reply -- per
        phase3/arp-worker/internal/ipc/dispatch.go, "shutdown" always
        terminates the connection on the worker's side without a reply
        of its own, so waiting here would just block until the worker
        closes the socket anyway.
        """
        self._send({"v": PROTOCOL_VERSION, "op": "shutdown", "reason": reason})

    def _request(self, msg: dict[str, Any]) -> dict[str, Any]:
        self._send(msg)
        reply = self._read_frame()
        if reply.get("v") != PROTOCOL_VERSION:
            raise WorkerError(f"worker replied with unsupported protocol version: {reply!r}")
        if reply.get("op") == "fault":
            raise WorkerError(
                f"worker fault: reason={reply.get('reason')!r} action={reply.get('action')!r}"
            )
        return reply

    def _expect_op(self, reply: dict[str, Any], op: str) -> None:
        if reply.get("op") != op:
            raise WorkerError(f"expected op={op!r} in reply, got {reply!r}")

    def _send(self, msg: dict[str, Any]) -> None:
        line = json.dumps(msg, separators=(",", ":")) + "\n"
        self._sock.sendall(line.encode("utf-8"))

    def _read_frame(self) -> dict[str, Any]:
        while b"\n" not in self._buf:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise WorkerError("worker closed the connection before sending a reply")
            self._buf += chunk
        line, self._buf = self._buf.split(b"\n", 1)
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise WorkerError(f"malformed JSON frame from worker: {line!r}") from exc
