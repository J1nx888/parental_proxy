#!/usr/bin/env python3
"""Entrypoint for the interception-controller (Milestones 3/4/6/7
scaffold).

Wires a WorkerClient, a desired-state source, reconcile(), and the
heartbeat pacer together into the control loop RoadMap.md's Milestone 3
describes: versioned Unix-socket IPC, generations, leases, idempotent
reconciliation. Milestone 6 adds systemd sd_notify/watchdog integration
and interception_runtime health reporting; Milestone 7 adds computing
and publishing the DesiredPolicy blob phase3/nftables-manager reads.

NOT a real deployable yet: --db-path wires in the real Milestone 4
desired-state source (controller/desired_state.py, devices +
device_bindings), but there's still no Dockerfile/systemd unit for this
component, no discovery daemon populating device_bindings from live
traffic, and the gateway is passed in on the command line rather than
resolved live (that's the ARP worker's own job at startup -- see
phase3/arp-worker/internal/worker/safety.go's ResolveGateway -- not
something the controller should do a second time). Reconnection after
the worker socket itself dies (as opposed to a single failed request)
isn't implemented either -- see run()'s own note. See
docs/design/phase3-technical-design.md and RoadMap.md's milestone list.
"""
from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import sys
import time
from pathlib import Path
from typing import Callable

# common/*.py lives in ../common relative to this file when run from a
# repo checkout -- no Dockerfile for this component yet (see this
# package's own status notes), so there's no flat-copy build step to
# rely on. Mirrors dashboard/dev_server.py's bootstrap.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "common"))

import health
import sdnotify
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
    health_conn: sqlite3.Connection | None = None,
    policy_conn: sqlite3.Connection | None = None,
) -> None:
    """The main control loop. Runs until SIGTERM/SIGINT.

    Registering the signal handlers here (rather than in main()) is
    deliberate: run() is also what a future integration test would call
    directly against a real worker socket, and it should be
    self-contained regardless of caller.

    health_conn/policy_conn are separate parameters (even though
    they're typically the same connection in practice -- see
    _build_db_backed_provider) because they're conceptually independent:
    a caller could report health without computing policy, or vice
    versa, and keeping them distinct avoids run() assuming its caller's
    wiring. Either or both may be None.

    A single failed reconcile cycle (a worker fault, a transient DB
    error) is logged and reported via health_conn rather than crashing
    the process -- matching the fail-open design's "controller drives
    repair, not a crash" intent. What this does NOT yet do: reconnect
    the WorkerClient if the underlying socket itself dies (as opposed
    to one request failing) -- that needs backoff/retry logic not
    built yet (RoadMap.md's Milestone 9 fault-campaign territory).
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
        sdnotify.watchdog()  # a successful heartbeat round-trip IS this process's own liveness signal

    pacer = HeartbeatPacer(
        heartbeat_interval,
        _send_heartbeat,
        on_error=lambda exc: log.warning("heartbeat failed: %s", exc),
    )
    pacer.start()
    sdnotify.ready()

    try:
        while not stop:
            applied = run_cycle(client, desired_state_provider, applied, health_conn, policy_conn)
            time.sleep(poll_interval)
    finally:
        pacer.stop()
        client.shutdown("controller_requested")
        client.close()


def run_cycle(
    client: WorkerClient,
    desired_state_provider: Callable[[], DesiredState],
    applied: AppliedState | None,
    health_conn: sqlite3.Connection | None,
    policy_conn: sqlite3.Connection | None,
) -> AppliedState | None:
    """One reconcile+health+policy cycle, pulled out of run()'s loop
    specifically so it's independently testable against a fake worker
    socket without needing to fight Python's main-thread-only
    signal.signal() restriction that run() itself is subject to.

    A failure anywhere in this cycle is caught and reported via
    health_conn rather than propagating -- see run()'s own docstring
    for why a single bad cycle shouldn't crash the process. Returns the
    (possibly unchanged) AppliedState for the caller to pass back in
    next cycle.
    """
    try:
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

        if policy_conn is not None:
            # Milestone 7: recompute and publish DesiredPolicy every
            # cycle -- phase3/nftables-manager reads this directly from
            # the DB (see policy_state.py's own module doc), so this is
            # the only "push" step needed on this side.
            from policy_state import compute_desired_policy, write_desired_policy

            write_desired_policy(policy_conn, compute_desired_policy(policy_conn))

        if health_conn is not None:
            health.report_healthy(health_conn, applied.generation if applied else 0)
    except Exception as exc:  # noqa: BLE001 -- deliberately broad, see run()'s own docstring
        log.warning("reconcile cycle failed: %s", exc)
        if health_conn is not None:
            health.report_fail_open(health_conn, str(exc))

    return applied


def _build_db_backed_provider(
    db_path: str, gateway_ip: str, gateway_mac: str, full_duplex: bool
) -> tuple[Callable[[], DesiredState], sqlite3.Connection]:
    import db
    from desired_state import db_backed_desired_state

    db.DB_PATH = Path(db_path)
    conn = db.get_conn()
    db.init_db(conn)
    gateway = Target(ip=gateway_ip, mac=gateway_mac)

    provider = lambda: db_backed_desired_state(conn, gateway, full_duplex=full_duplex)  # noqa: E731
    return provider, conn


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/parental_proxy/arp-worker.sock")
    parser.add_argument("--heartbeat-interval", type=float, default=2.0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument(
        "--db-path",
        help="Use the real devices/device_bindings tables (Milestone 4) as the "
        "desired-state source instead of the placeholder, and enable health/"
        "policy reporting into the same database (Milestones 6/7). Requires "
        "--gateway-ip and --gateway-mac.",
    )
    parser.add_argument("--gateway-ip", help="Required if --db-path is set.")
    parser.add_argument("--gateway-mac", help="Required if --db-path is set.")
    parser.add_argument("--full-duplex", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    conn: sqlite3.Connection | None = None
    if args.db_path:
        if not args.gateway_ip or not args.gateway_mac:
            parser.error("--db-path requires --gateway-ip and --gateway-mac")
        provider, conn = _build_db_backed_provider(
            args.db_path, args.gateway_ip, args.gateway_mac, args.full_duplex
        )
    else:
        provider = placeholder_desired_state

    run(
        args.socket,
        provider,
        args.heartbeat_interval,
        args.poll_interval,
        health_conn=conn,
        policy_conn=conn,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
