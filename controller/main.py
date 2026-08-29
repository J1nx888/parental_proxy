#!/usr/bin/env python3
"""Entrypoint for the interception-controller (Milestone 3 scaffold).

Wires a WorkerClient, a desired-state source, reconcile(), and the
heartbeat pacer together into the actual control loop RoadMap.md's
Milestone 3 describes: versioned Unix-socket IPC, generations, leases,
idempotent reconciliation.

NOT a real deployable yet: placeholder_desired_state() below has no real
data source -- Milestone 4 (identity model, device_bindings,
devices.is_authenticated / bypass_v4 policy) is what a genuine
implementation needs, and hasn't been built. There's also no
Dockerfile/systemd unit for this component yet. See
docs/design/phase3-technical-design.md and RoadMap.md's milestone list.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from typing import Callable

from ipc_client import WorkerClient
from lease import HeartbeatPacer
from reconcile import AppliedState, DesiredState, reconcile

log = logging.getLogger("controller")


def placeholder_desired_state() -> DesiredState:
    """TODO(Milestone 4): replace with a real query against
    device_bindings + devices.is_authenticated/bypass_v4 once that
    schema exists (see docs/design/phase3-technical-design.md section
    7 for the draft migration). Deliberately raises rather than
    guessing at a "safe-looking" empty target list -- an empty
    DesiredState would still be a real generation applied to the
    worker (see reconcile()), which is not something this scaffold
    should do silently before there's a real policy behind it.
    """
    raise NotImplementedError(
        "no real desired-state source yet -- see the TODO above. Pass a "
        "different `desired_state_provider` to run() for manual testing "
        "against a real worker socket."
    )


def run(
    socket_path: str,
    desired_state_provider: Callable[[], DesiredState],
    heartbeat_interval: float = 2.0,
    poll_interval: float = 5.0,
) -> None:
    """The main control loop. Runs until SIGTERM/SIGINT.

    Registering the signal handlers here (rather than in main()) is
    deliberate: run() is also what a future integration test would call
    directly against a real worker socket, and it should be
    self-contained regardless of caller.
    """
    client = WorkerClient.connect(socket_path)
    applied: AppliedState | None = None
    sequence = 0
    stop = False

    def _request_stop(signum, frame):  # noqa: ARG001 -- required signal handler signature
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    def _send_heartbeat() -> None:
        nonlocal sequence
        sequence += 1
        client.heartbeat(sequence)

    pacer = HeartbeatPacer(
        heartbeat_interval,
        _send_heartbeat,
        on_error=lambda exc: log.warning("heartbeat failed: %s", exc),
    )
    pacer.start()

    try:
        while not stop:
            desired = desired_state_provider()
            next_gen = reconcile(applied, desired)
            if next_gen is not None:
                result = client.replace_targets(
                    next_gen, desired.gateway, list(desired.targets), desired.full_duplex
                )
                if result.resolution_failures:
                    log.warning("worker rejected some targets: %s", result.resolution_failures)
                applied = AppliedState(generation=next_gen, desired=desired)
                log.info("applied generation %d (%d targets)", next_gen, result.target_count)
            time.sleep(poll_interval)
    finally:
        pacer.stop()
        client.shutdown("controller_requested")
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/parental_proxy/arp-worker.sock")
    parser.add_argument("--heartbeat-interval", type=float, default=2.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)
    run(args.socket, placeholder_desired_state, args.heartbeat_interval, args.poll_interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
