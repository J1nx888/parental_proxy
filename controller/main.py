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
component, and the gateway is passed in on the command line rather than
resolved live (that's the ARP worker's own job at startup -- see
phase3/arp-worker/internal/worker/safety.go's ResolveGateway -- not
something the controller should do a second time). See
docs/design/phase3-technical-design.md and RoadMap.md's milestone list.

**Discovery is now wired in (2026-08-30)**: when --db-path is given,
run() also starts controller/discovery.py's snapshot loop on its own
background thread and its own DB connection (see discovery.run_loop's
own docstring for why a separate connection is required). This is still
only the periodic ip-neigh-show snapshot -- the higher-precedence live
rtnetlink-event listener remains unbuilt (see discovery.py's module
docstring).
"""
from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Callable

# common/*.py lives in ../common relative to this file when run from a
# repo checkout. controller/Dockerfile (added 2026-08-30) instead
# flat-copies common/*.py alongside controller/*.py into one directory,
# matching proxy/Dockerfile and dashboard/Dockerfile's own pattern --
# in that layout ../common doesn't exist, but it doesn't need to:
# Python already puts this script's own directory (where the flat-
# copied common/*.py sit) on sys.path[0] by default. Only insert the
# repo-checkout path when it's actually there, so both layouts work.
_common_dir = Path(__file__).resolve().parent.parent / "common"
if _common_dir.is_dir():
    sys.path.insert(0, str(_common_dir))

import adguard_sync
import discovery
import rtnetlink_listener
import health
import sdnotify
from ipc_client import Target, WorkerClient, WorkerConnectionError
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
    discovery_interval: float | None = None,
    enable_rtnetlink: bool = False,
    adguard_interval: float | None = None,
    adguard_url: str | None = None,
    adguard_username: str | None = None,
    adguard_password: str | None = None,
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

    discovery_interval, if given (not None), starts discovery.py's
    periodic `ip neigh show` snapshot on its own background thread and
    its own DB connection (see discovery.run_loop's own docstring for
    why it must open that connection itself rather than being handed
    health_conn/policy_conn) for the duration of this call, stopped in
    the `finally` block below alongside the heartbeat pacer. None (the
    default) means no discovery loop runs, matching this parameter's
    absence before 2026-08-30 -- existing callers that don't pass it see
    no behavior change.

    enable_rtnetlink, if True, starts
    controller/rtnetlink_listener.py's live RTM_NEWNEIGH listener
    alongside the discovery snapshot loop above -- the higher-precedence
    source discovery.py's own docstring flagged as still unbuilt until
    2026-08-30. Also stopped in the `finally` block below. Independent
    of discovery_interval -- both can run together (the snapshot catches
    anything the live listener missed, e.g. a device already-idle before
    this process started), matching the design doc's own layered
    precedence order rather than one replacing the other.

    adguard_interval, if given (not None), starts
    controller/adguard_sync.py's periodic hard-deny sync on its own
    background thread and its own DB connection (same reasoning as
    discovery_interval above) for the duration of this call, stopped in
    the `finally` block alongside the heartbeat pacer and discovery
    task. adguard_url/adguard_username/adguard_password are required
    together with it -- see main()'s own argument validation.

    A single failed reconcile cycle (a worker fault, a transient DB
    error) is logged and reported via health_conn rather than crashing
    the process -- matching the fail-open design's "controller drives
    repair, not a crash" intent (RoadMap.md's Milestone 9 fault-
    campaign). If the underlying socket itself dies (WorkerConnectionError
    -- a broken pipe, connection reset, or EOF, as opposed to a healthy
    connection carrying an application-level fault), run() closes the
    dead client and tries exactly one fresh connection per loop
    iteration -- naturally rate-limited to poll_interval without needing
    explicit backoff, and still responsive to a stop request every
    iteration. `applied` is reset to None on reconnect: a freshly
    (re)connected worker process may be a brand-new process (systemd
    restarted it) with no memory of any prior generation, so the next
    cycle must treat this as a first application again rather than
    possibly skipping a resend because desired state happens not to
    have changed since the connection dropped.

    **This reconnect path is also triggered by a failed heartbeat, not
    just a failed run_cycle() (added 2026-08-30, a real gap found during
    this project's first live-container verification pass)**: if
    desired state never changes across a worker restart, run_cycle()'s
    own reconcile() correctly returns None every time (nothing new to
    send) and never touches the connection at all -- with an unchanging
    desired state, a dead worker could previously go undetected
    indefinitely, since the heartbeat pacer's own failures were only
    ever logged, never acted on. The heartbeat pacer is the one thing
    that touches the connection every single cycle regardless of
    desired state, which is what makes it the thing that actually
    notices. `heartbeat_worker_dead` (a threading.Event set by the
    pacer's on_error callback, checked at the top of the main loop)
    routes a heartbeat-detected failure through the exact same
    `_reconnect()` codepath run_cycle()'s own WorkerConnectionError
    uses, rather than duplicating the reconnect logic.

    Known race, accepted rather than fixed here given the scope of this
    pass: the heartbeat pacer runs on its own thread and reads `client`
    from this closure at call time. If it fires in the narrow window
    between closing a dead client and a fresh one being assigned, its
    heartbeat call raises AttributeError on None -- HeartbeatPacer's own
    broad exception handling logs it via on_error rather than crashing,
    so this is a harmless, if noisy, cosmetic race, not a correctness
    bug. A future pass could add a lock around client reads/writes if
    the noise proves annoying in practice.

    Known race, accepted rather than fixed here given the scope of this
    pass: the heartbeat pacer runs on its own thread and reads `client`
    from this closure at call time. If it fires in the narrow window
    between closing a dead client and a fresh one being assigned, its
    heartbeat call raises AttributeError on None -- HeartbeatPacer's own
    broad exception handling logs it via on_error rather than crashing,
    so this is a harmless, if noisy, cosmetic race, not a correctness
    bug. A future pass could add a lock around client reads/writes if
    the noise proves annoying in practice.
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

    # Set by the heartbeat pacer's own error callback below when a
    # heartbeat fails with WorkerConnectionError -- checked at the top
    # of the main loop to trigger the exact same reconnect path
    # run_cycle()'s own WorkerConnectionError handling uses. Added
    # 2026-08-30 after a real gap found during this project's first
    # live-container verification pass: if desired state never changes
    # across a worker restart, run_cycle() never touches the connection
    # at all (by design -- reconcile() returns None, nothing to send),
    # so a dead worker was previously only ever noticed by whichever
    # thread happened to actually try using the socket next -- which,
    # with an unchanging desired state, could be never. The heartbeat
    # pacer is the one thing that ALWAYS touches the connection every
    # cycle regardless of desired state, making it the right place to
    # actually detect this.
    heartbeat_worker_dead = threading.Event()

    def _on_heartbeat_error(exc: Exception) -> None:
        log.warning("heartbeat failed: %s", exc)
        if isinstance(exc, WorkerConnectionError):
            heartbeat_worker_dead.set()

    pacer = HeartbeatPacer(heartbeat_interval, _send_heartbeat, on_error=_on_heartbeat_error)
    pacer.start()

    discovery_task = None
    if discovery_interval is not None:
        discovery_task = discovery.run_loop(
            discovery_interval,
            on_error=lambda exc: log.warning("discovery snapshot failed: %s", exc),
        )

    rtnetlink_task = None
    if enable_rtnetlink:
        rtnetlink_task = rtnetlink_listener.run_loop(
            on_error=lambda exc: log.warning("rtnetlink listener failed: %s", exc),
        )

    adguard_task = None
    if adguard_interval is not None:
        adguard_task = adguard_sync.run_loop(
            adguard_interval,
            adguard_url,
            adguard_username,
            adguard_password,
            on_error=lambda exc: log.warning("adguard sync failed: %s", exc),
        )

    sdnotify.ready()

    def _reconnect(reason: str) -> None:
        nonlocal client, applied
        log.warning("worker connection lost (%s) -- attempting to reconnect", reason)
        if health_conn is not None:
            health.report_fail_open(health_conn, f"worker connection lost: {reason}")
        client.close()
        try:
            client = WorkerClient.connect(socket_path)
            applied = None
            log.info("reconnected to worker")
        except OSError as reconnect_exc:
            log.warning("reconnect attempt failed, will retry next cycle: %s", reconnect_exc)

    try:
        while not stop:
            if heartbeat_worker_dead.is_set():
                heartbeat_worker_dead.clear()
                _reconnect("detected via a failed heartbeat")
            else:
                try:
                    applied = run_cycle(client, desired_state_provider, applied, health_conn, policy_conn)
                except WorkerConnectionError as exc:
                    _reconnect(str(exc))
            time.sleep(poll_interval)
    finally:
        pacer.stop()
        if discovery_task is not None:
            discovery_task.stop()
        if rtnetlink_task is not None:
            rtnetlink_task.stop()
        if adguard_task is not None:
            adguard_task.stop()
        try:
            client.shutdown("controller_requested")
        except WorkerConnectionError:
            pass  # already dead -- nothing to tell it
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
    for why a single bad cycle shouldn't crash the process -- with ONE
    deliberate exception: WorkerConnectionError propagates to the
    caller uncaught, since only run() (which owns the WorkerClient
    variable) can actually reconnect; this function has no way to hand
    its caller a replacement client. Returns the (possibly unchanged)
    AppliedState for the caller to pass back in next cycle.
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
    except WorkerConnectionError:
        raise  # let run() handle reconnection -- see this function's own docstring
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
    parser.add_argument(
        "--discovery-interval", type=float, default=30.0,
        help="Seconds between discovery.py periodic ip-neigh-show snapshots "
        "(only runs at all if --db-path is set). See controller/discovery.py's "
        "module docstring for what this does and doesn't catch.",
    )
    parser.add_argument(
        "--no-discovery", action="store_true",
        help="Disable the discovery snapshot loop even when --db-path is set "
        "-- e.g. for a deployment that already runs the snapshot externally "
        "(cron, a separate process) and doesn't want it duplicated here.",
    )
    parser.add_argument(
        "--no-rtnetlink", action="store_true",
        help="Disable the live rtnetlink RTM_NEWNEIGH listener "
        "(controller/rtnetlink_listener.py) even when --db-path is set. "
        "Requires the pyroute2 package (controller/requirements.txt) and a "
        "Linux host -- enabled by default alongside --db-path since it has "
        "no interval of its own to tune, only a way to turn it off.",
    )
    parser.add_argument(
        "--adguard-url",
        help="Base URL of AdGuard Home's control API, e.g. http://127.0.0.1:3000 "
        "(or http://adguard:3000 once this process runs in the same compose "
        "network as the adguard service). Enables the hard-deny sync loop "
        "(controller/adguard_sync.py) when set, together with "
        "--adguard-username/--adguard-password. Only runs at all if --db-path "
        "is also set.",
    )
    parser.add_argument("--adguard-username", help="Required if --adguard-url is set.")
    parser.add_argument("--adguard-password", help="Required if --adguard-url is set.")
    parser.add_argument(
        "--adguard-interval", type=float, default=30.0,
        help="Seconds between adguard_sync.py hard-deny rule pushes (only runs "
        "at all if --adguard-url is set).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    if args.adguard_url and not (args.adguard_username and args.adguard_password):
        parser.error("--adguard-url requires --adguard-username and --adguard-password")

    conn: sqlite3.Connection | None = None
    discovery_interval: float | None = None
    enable_rtnetlink = False
    adguard_interval: float | None = None
    if args.db_path:
        if not args.gateway_ip or not args.gateway_mac:
            parser.error("--db-path requires --gateway-ip and --gateway-mac")
        provider, conn = _build_db_backed_provider(
            args.db_path, args.gateway_ip, args.gateway_mac, args.full_duplex
        )
        if not args.no_discovery:
            # discovery.run_loop() opens its own connection internally
            # (see its docstring for why) -- db.DB_PATH is already set to
            # args.db_path by _build_db_backed_provider above, so it just
            # needs to be told to run at all, and at what interval.
            discovery_interval = args.discovery_interval
        if not args.no_rtnetlink:
            # rtnetlink_listener.run_loop() opens its own connection
            # internally too, same reasoning -- see its own module
            # docstring for why pyroute2 (Linux-only) is imported lazily
            # rather than at this file's top level.
            enable_rtnetlink = True
        if args.adguard_url:
            # Same reasoning as discovery_interval above -- adguard_sync
            # opens its own connection internally, reading the DB
            # policy-state discovery already keeps current.
            adguard_interval = args.adguard_interval
    else:
        provider = placeholder_desired_state

    run(
        args.socket,
        provider,
        args.heartbeat_interval,
        args.poll_interval,
        health_conn=conn,
        policy_conn=conn,
        discovery_interval=discovery_interval,
        enable_rtnetlink=enable_rtnetlink,
        adguard_interval=adguard_interval,
        adguard_url=args.adguard_url,
        adguard_username=args.adguard_username,
        adguard_password=args.adguard_password,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
