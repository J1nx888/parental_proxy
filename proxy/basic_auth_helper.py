#!/usr/bin/env python3
"""Squid `auth_param basic program` helper.

Protocol: one "username password" per line on stdin (password is the rest
of the line after the first space -- Squid's classic Basic scheme doesn't
percent-encode it). Responds "OK" or "ERR" per line, flushed immediately.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/opt/parental-proxy")

import auth
import db


def check(conn, username: str, password: str) -> bool:
    row = conn.execute(
        "SELECT password_hash FROM users WHERE username = ?", (username,)
    ).fetchone()
    if row is None:
        return False
    return auth.verify_password(password, row["password_hash"])


def main() -> int:
    conn = db.get_conn()
    db.init_db(conn)
    for line in sys.stdin:
        line = line.rstrip("\n").rstrip("\r")
        if " " not in line:
            sys.stdout.write("ERR\n")
            sys.stdout.flush()
            continue
        username, password = line.split(" ", 1)
        try:
            ok = check(conn, username, password)
        except Exception as exc:
            print(f"basic_auth_helper error: {exc}", file=sys.stderr, flush=True)
            ok = False
        sys.stdout.write(("OK\n" if ok else "ERR\n"))
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
