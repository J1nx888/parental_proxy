#!/usr/bin/env python3
"""Squid `auth_param basic program` helper -- one login per person.

Protocol: "username password" per line on stdin, percent-encoded, and the
password is everything after the first space (so it may contain spaces).
Responds "OK"/"ERR" per line. The stdin loop itself lives in
common/squid_helper.py.

Confirmed against a real Squid 5.7 instance (2026-08-28): despite the
"classic Basic doesn't percent-encode" folklore this docstring used to
repeat, this Squid version *does* percent-encode both fields exactly like
its external_acl_type helpers -- a raw capture showed a password of
`a b%c d` arriving as `a%20b%25c%20d`. With unquote=False (the previous
setting), any password containing a space, `%`, or other character needing
escaping could never successfully authenticate at all. See
docs/review-2026-08-28.md (item 2.8).
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


def main() -> int:
    return squid_helper.run(
        "basic_auth_helper", 2, check,
        unquote=True, keep_trailing_spaces=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
