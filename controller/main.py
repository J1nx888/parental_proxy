#!/usr/bin/env python3
"""Entrypoint for the interception-controller (Milestone 3 scaffold).

Wires a WorkerClient, a desired-state source, reconcile(), and the
heartbeat pacer together into the actual control loop RoadMap.md's
Milestone 3 describes: versioned Unix-socket IPC, generations, leases,
idempotent reconciliation.

NOT a real deployable yet: --db-path wires in the real Milestone 4
desired-state source (controller/desired_state.py, devices +
device_bindings), but there's still no Dockerfile/systemd unit for this
component, no discovery daemon populating device_bindings from live
traffic, and the gateway is passed in on the command line rather than
resolved live (that's the ARP worker's own job at startup -- see
phase3/arp-worker/internal/worker/safety.go's ResolveGateway -- not
something the controller should do a second time). See
docs/design/phase3-technical-design.md and RoadMap.md's milestone list.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Callable

# common/*.py lives in ../common relative to this file when run from a
# repo checkout -- no Dockerfile for this component yet (see this
# package's own status notes), so there's no flat-copy build step to
# rely on. Mirrors dashboard/dev_server.py's bootstrap.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))

from ipc_client import Target, WorkerClient
from lease import HeartbeatPacer
from reconcile import AppliedState, DesiredState, reconcile

log = logging.getLogger("controller")


def placeholder_desired_state() -> DesiredState:
    """The default when --db-path isn't given. See
    controller/desired_state.py's db_backed_desired_state for the real
    Milestone 4 source (devices + device_bindings) -- this placeholder
    still exists for running against a worker with no real device data
    behind it yet. Deliberately raises rather than guessing at a
    "safe-looking" empty target list -- an empty DesiredState would
    still be a real generation applied to the worker (see reconcile()),
    which isn't something to do silently by default.
    """
    raise NotImplementedError(
        "no desired-state source configured -- pass --db-path (and "
        "--gateway-ip/--gateway-mac) to use the real devices/"
        "device_bindings source, or pass a different "
        "`desired_state_provider` to run() directly for manual testing."
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


def _build_db_backed_provider(
    db_path: str, gateway_ip: str, gateway_mac: str, full_duplex: bool
) -> Callable[[], DesiredState]:
    import db
    from desired_state import db_backed_desired_state

    db.DB_PATH = Path(db_path)
    conn = db.get_conn()
    db.init_db(conn)
    gateway = Target(ip=gateway_ip, mac=gateway_mac)

    return lambda: db_backed_desired_state(conn, gateway, full_duplex=full_duplex)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/parental_proxy/arp-worker.sock")
    parser.add_argument("--heartbeat-interval", type=float, default=2.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument(
        "--db-path",
        help="Use the real devices/device_bindings tables (Milestone 4) as the "
        "desired-state source instead of the placeholder. Requires --gateway-ip "
        "and --gateway-mac.",
    )
    parser.add_argument("--gateway-ip", help="Required if --db-path is set.")
    parser.add_argument("--gateway-mac", help="Required if --db-path is set.")
    parser.add_argument("--full-duplex", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    if args.db_path:
        if not args.gateway_ip or not args.gateway_mac:
            parser.error("--db-path requires --gateway-ip and --gateway-mac")
        provider = _build_db_backed_provider(
            args.db_path, args.gateway_ip, args.gateway_mac, args.full_duplex
        )
    else:
        provider = placeholder_desired_state

    run(args.socket, provider, args.heartbeat_interval, args.poll_interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
