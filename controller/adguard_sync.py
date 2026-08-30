#!/usr/bin/env python3
"""Enforces the hard-deny invariant RoadMap.md locked 2026-08-30 (the
"two independent axes" section): a domain marked `domains.mode = 'bump'`
must NEVER be reachable via plain, unrefined DNS-tier access on a
device that isn't `bump_enabled` -- refined through Squid, or denied
outright, never a silent DNS-tier fallback. nftables has no domain
visibility to enforce this itself (it can only redirect by IP+port, see
knftables_adapter.go's own comment on this exact point), so it has to
happen here, at the DNS tier, before a connection to that domain is
even attempted -- via AdGuard Home's per-client custom filtering rules
(see common/adguard_client.py's docstring for how that's verified to
actually work).

Same idempotent-full-reconcile shape as everywhere else in this
codebase (phase3/nftables-manager's flush-before-re-add,
controller/policy_state.py's full DesiredPolicy recompute every cycle):
every sync reads the DB and AdGuard's current rules fresh, computes the
complete desired managed-rules block, and replaces it whole -- no
incremental add/remove, no assumption about what a previous cycle left
behind.
"""
from __future__ import annotations

import logging
import sqlite3

import adguard_client
from periodic import PeriodicTask

log = logging.getLogger("controller.adguard_sync")

# Bracket this process's own rules within AdGuard's shared custom-rules
# list so a sync can find and replace exactly its own block, leaving
# anything an admin added by hand (directly in AdGuard's own UI)
# completely untouched -- the same "dedicated space, never touch
# anything else on the box" discipline
# phase3/nftables-manager/internal/nft/knftables_adapter.go applies to
# its own nftables table, applied here to AdGuard's rules list instead.
_MARKER_BEGIN = "! === parental_proxy managed rules -- do not edit below this line, see docs/security/overview.md ==="
_MARKER_END = "! === end parental_proxy managed rules ==="


def _domain_rule(pattern: str, client_ips: list[str], block_page_ip: str | None = None) -> str:
    """One AdGuard regex rule, scoped to client_ips via the `$client`
    modifier (confirmed live against a real AdGuard Home instance --
    see common/adguard_client.py's docstring). The regex body mirrors
    common/matching.py's own `_domain_regex()` anchoring EXACTLY
    (`(?:^|\\.)(?:pattern)\\Z`, case-insensitive) so a domain is
    hard-denied here under precisely the same suffix-match rules Squid
    itself uses to decide bump-mode -- translated to AdGuard's regex
    rule syntax (Go's RE2 engine via the `/regex/` rule form): RE2 has
    no `\\Z`, but a plain `$` is equivalent here since a DNS query name
    never contains an embedded newline; `(?i)` is RE2's inline
    case-insensitivity flag, replacing Python's separate
    `re.IGNORECASE` argument.

    If block_page_ip is given, the rule ALSO carries a `$dnsrewrite`
    modifier pointing the DNS answer at that IP instead of the plain
    default deny (confirmed live combinable with `$client` on one rule).
    That IP is expected to be running dashboard/block_page_server.py,
    which only ever answers on port 80 -- deliberately no HTTPS
    equivalent, see that module's own docstring for why showing a page
    over HTTPS to a device that was never asked to trust this project's
    CA would be worse than today's plain failure, not better.
    """
    body = f"(?i)(?:^|\\.)(?:{pattern})$"
    rule = f"/{body}/$client={','.join(client_ips)}"
    if block_page_ip:
        rule += f",dnsrewrite=NOERROR;A;{block_page_ip}"
    return rule


def build_rules(conn: sqlite3.Connection, block_page_ip: str | None = None) -> list[str]:
    """The complete list of managed hard-deny rules for right now: one
    rule per `mode = 'bump'` domain, each scoped to every device that
    is currently NOT `bump_enabled`.

    Deliberately not filtered by `is_authenticated`/`ignored`/
    `quarantined_at`: a device excluded from DNS-tier interception
    entirely (bypass_v4/quarantine_v4, see policy_class.py) never
    actually queries AdGuard's redirected port in the first place, so
    including its IP here changes nothing for it -- and the fail-closed
    default (deny unless a device is explicitly opted into bump) is the
    one RoadMap.md's invariant actually calls for. A device with no
    currently-active `device_bindings` row contributes no IP -- there's
    nothing to add a rule for yet; the same DHCP-staleness bound as
    `controller/discovery.py`'s snapshot loop applies here (a device's
    new IP is only picked up once discovery records it, and only
    enforced once the next adguard_sync cycle runs after that).

    Returns an empty list when there are no bump-mode domains configured
    at all, or no non-bump device currently has a known IP -- both
    legitimate "nothing to deny yet" states, not errors.
    """
    domains = conn.execute("SELECT pattern FROM domains WHERE mode = 'bump' ORDER BY id").fetchall()
    if not domains:
        return []

    non_bump_ips = [
        row["ipv4_address"]
        for row in conn.execute(
            """
            SELECT DISTINCT b.ipv4_address
            FROM devices d
            JOIN device_bindings b ON b.device_id = d.id AND b.active = 1
            WHERE d.bump_enabled = 0
            ORDER BY b.ipv4_address
            """
        ).fetchall()
    ]
    if not non_bump_ips:
        return []

    return [_domain_rule(row["pattern"], non_bump_ips, block_page_ip) for row in domains]


def _strip_managed_block(rules: list[str]) -> list[str]:
    """Remove a previously-synced managed block from AdGuard's rules
    list, keeping everything else (an admin's own manually-added rules,
    in whatever order they were in) exactly as-is."""
    if _MARKER_BEGIN not in rules:
        return list(rules)
    start = rules.index(_MARKER_BEGIN)
    try:
        end = rules.index(_MARKER_END, start)
        return rules[:start] + rules[end + 1 :]
    except ValueError:
        # A begin marker with no matching end (hand-edited AdGuard
        # rules, or an interrupted previous sync) -- drop from the
        # begin marker to the end of the list rather than leave a
        # dangling, unterminated block sitting there forever.
        return rules[:start]


def sync_once(
    conn: sqlite3.Connection, base_url: str, username: str, password: str, block_page_ip: str | None = None
) -> int:
    """One full sync cycle. Returns the number of managed rules pushed
    (0 is a normal, healthy state -- see build_rules' own docstring)."""
    managed = build_rules(conn, block_page_ip)
    current = adguard_client.get_custom_rules(base_url, username, password)
    preserved = _strip_managed_block(current)

    new_rules = preserved if not managed else preserved + [_MARKER_BEGIN, *managed, _MARKER_END]
    adguard_client.set_custom_rules(base_url, username, password, new_rules)
    return len(managed)


def run_loop(
    interval: float,
    base_url: str,
    username: str,
    password: str,
    block_page_ip: str | None = None,
    on_error=None,
) -> PeriodicTask:
    """Starts `sync_once()` running on a fixed interval, on its own
    background thread, until the returned `PeriodicTask.stop()` is
    called -- same shape as `controller/discovery.py`'s `run_loop()`,
    including the same reason for opening its own DB connection lazily
    on the background thread rather than accepting one from the caller
    (see that module's docstring: `sqlite3.Connection` objects are only
    usable from the thread that created them).

    A failed sync (AdGuard unreachable, a transient HTTP error) is
    reported via `on_error` rather than killing the loop, same as every
    other periodic task in this codebase -- a temporarily-unreachable
    AdGuard means the PREVIOUSLY-pushed rules stay in effect (fails
    closed: whatever was denied stays denied), not a reason to stop
    trying.
    """
    state: dict[str, sqlite3.Connection] = {}

    def task() -> None:
        conn = state.get("conn")
        if conn is None:
            import db  # local import: mirrors discovery.run_loop's own lazy `import db`

            conn = db.get_conn()
            db.init_db(conn)
            state["conn"] = conn
        sync_once(conn, base_url, username, password, block_page_ip)

    pt = PeriodicTask(interval, task, on_error=on_error, thread_name="adguard-sync")
    pt.start()
    return pt
