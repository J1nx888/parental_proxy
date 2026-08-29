#!/usr/bin/env python3
"""Milestone 6: the controller's own health reporting into
interception_runtime -- specifically the mode/last_healthy_at/
fail_open_reason columns tracking the controller<->ARP-worker pipeline,
deliberately separate from phase3/nftables-manager's own nft_mode/
nft_last_healthy_at/nft_fail_reason columns (see common/db.py's schema
comment) so the two subsystems never clobber each other's status in
the shared singleton row.
"""
from __future__ import annotations

import sqlite3

import db


def report_healthy(conn: sqlite3.Connection, applied_generation: int) -> None:
    """Call this once per successful reconciliation cycle (see
    controller/main.py's run())."""
    now = db.now_iso()
    conn.execute(
        "INSERT INTO interception_runtime "
        "(singleton_id, applied_generation, mode, last_healthy_at, fail_open_reason) "
        "VALUES (1, ?, 'running', ?, NULL) "
        "ON CONFLICT(singleton_id) DO UPDATE SET "
        "applied_generation = excluded.applied_generation, mode = 'running', "
        "last_healthy_at = excluded.last_healthy_at, fail_open_reason = NULL",
        (applied_generation, now),
    )
    conn.commit()


def report_fail_open(conn: sqlite3.Connection, reason: str) -> None:
    """Call this when a reconciliation cycle fails -- does not touch
    applied_generation (the last successfully-applied generation is
    still meaningful/true even while a later cycle is failing)."""
    conn.execute(
        "INSERT INTO interception_runtime (singleton_id, mode, fail_open_reason) "
        "VALUES (1, 'fail_open', ?) "
        "ON CONFLICT(singleton_id) DO UPDATE SET mode = 'fail_open', "
        "fail_open_reason = excluded.fail_open_reason",
        (reason,),
    )
    conn.commit()
