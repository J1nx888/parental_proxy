#!/usr/bin/env python3
"""Squid `auth_param basic program` helper -- one login per person.

Protocol: "username password" per line on stdin. The classic Basic scheme
does not percent-encode these, and the password is everything after the
first space (so it may contain spaces). Responds "OK"/"ERR" per line.
The stdin loop itself lives in common/squid_helper.py.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/opt/parental-proxy")

import auth
import squid_helper


def check(conn, username: str, password: str) -> bool:
    row = conn.execute(
        "SELECT password_hash FROM users WHERE username = ?", (username,)
    ).fetchone()
    if row is None:
        return False
    return auth.verify_password(password, row["password_hash"])


if __name__ == "__main__":
    raise SystemExit(
        squid_helper.run(
            "basic_auth_helper", 2, check,
            unquote=False, keep_trailing_spaces=True,
        )
    )
