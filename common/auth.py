#!/usr/bin/env python3
"""PBKDF2-SHA256 password hashing. Stdlib-only so the proxy container (which
needs to verify passwords for Squid's auth helper) doesn't need pip at all.
"""
from __future__ import annotations

import hashlib
import hmac
import os

ITERATIONS = 260_000
ALGORITHM = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), ITERATIONS)
    return f"{ALGORITHM}${ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations_s, salt, expected_hex = encoded.split("$", 3)
        iterations = int(iterations_s)
    except (ValueError, AttributeError):
        return False
    if algorithm != ALGORITHM:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return hmac.compare_digest(digest.hex(), expected_hex)
