"""dashboard/captive_portal_server.py: Phase 4 milestone 3's
captive-portal login server. Real integration test -- binds an
ephemeral port and makes real HTTP requests, same style as
test_block_page_server.py, since a stdlib http.server handler is what's
actually under test, not something worth mocking. do_POST's DB access
uses the `conn` fixture's own db.DB_PATH (a process-global db.py
attribute), so the server's own request-handling threads -- which open
their own connection per request, see the module's own docstring for
why -- land on the exact same on-disk temp DB these tests set up.
"""
from __future__ import annotations

import http.client
from urllib.parse import urlencode

import pytest

import auth
import captive_portal_server
import db
import identity

MAC_A = "aa:bb:cc:dd:ee:01"
# The real source address of every http.client request this test file
# makes -- record_binding() must bind MAC_A to THIS address, not an
# arbitrary LAN-looking one, or resolve_device() correctly finds no
# active binding for the connection that's actually arriving.
IP_1 = "127.0.0.1"


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """The rate limiter's own state (see captive_portal_server.py's own
    comment on why it's a module-level dict, not per-instance) would
    otherwise leak between tests -- every test in this file's real HTTP
    requests come from the same source address (127.0.0.1), so a failed
    login recorded by one test would count against a completely
    unrelated later test without this."""
    captive_portal_server._failed_attempts.clear()
    yield
    captive_portal_server._failed_attempts.clear()


@pytest.fixture
def server(conn):  # noqa: ARG001 -- depended on for its db.DB_PATH monkeypatch side effect
    srv = captive_portal_server.start(host="127.0.0.1", port=0)  # port=0 -> OS picks a free one
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _get(server, path="/", host_header="captive.apple.com", method="GET"):
    http_conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        http_conn.request(method, path, headers={"Host": host_header})
        resp = http_conn.getresponse()
        return resp.status, resp.read().decode("utf-8"), dict(resp.getheaders())
    finally:
        http_conn.close()


def _post(server, username, password):
    body = urlencode({"username": username, "password": password})
    http_conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    try:
        http_conn.request(
            "POST", "/", body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))},
        )
        resp = http_conn.getresponse()
        return resp.status, resp.read().decode("utf-8")
    finally:
        http_conn.close()


def _add_user(conn, username, password, display_name=None):
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash, created_at) VALUES (?,?,?,?)",
        (username, display_name or username, auth.hash_password(password), db.now_iso()),
    )
    conn.commit()
    return conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()["id"]


# ============================================================
# GET/HEAD -- always the same login page, regardless of path/Host
# ============================================================

def test_get_returns_the_login_page(server):
    status, body, headers = _get(server)
    assert status == 200
    assert "text/html" in headers["Content-Type"]
    assert "Sign in" in body
    assert "<form" in body


def test_any_path_or_host_gets_the_same_login_page(server):
    """The whole design (see module docstring): every major OS's
    captive-portal probe uses a different hostname/path, but nftables
    redirects by source IP + destination port, not by hostname -- so
    every one of them must land on the exact same response."""
    for host, path in [
        ("captive.apple.com", "/hotspot-detect.html"),
        ("connectivitycheck.gstatic.com", "/generate_204"),
        ("www.msftconnecttest.com", "/connecttest.txt"),
        ("detectportal.firefox.com", "/success.txt"),
        ("example.com", "/"),
    ]:
        status, body, _ = _get(server, path=path, host_header=host)
        assert status == 200, f"{host}{path} did not get the login page"
        assert "Sign in" in body


def test_head_request_gets_a_200_with_no_body_assertion_needed(server):
    status, _, headers = _get(server, method="HEAD")
    assert status == 200
    assert "text/html" in headers["Content-Type"]


def test_responses_are_never_cached(server):
    _, _, headers = _get(server)
    assert headers.get("Cache-Control") == "no-store"


# ============================================================
# POST -- the actual login
# ============================================================

def test_successful_login_authenticates_the_device_and_shows_success(server, conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")
    device_id = conn.execute("SELECT device_id FROM device_bindings WHERE ipv4_address = ?", (IP_1,)).fetchone()["device_id"]
    user_id = _add_user(conn, "kid1", "correcthorse")

    status, body = _post(server, "kid1", "correcthorse")

    assert status == 200
    assert "signed in" in body.lower()
    row = conn.execute("SELECT is_authenticated, user_id FROM devices WHERE id = ?", (device_id,)).fetchone()
    assert row["is_authenticated"] == 1
    assert row["user_id"] == user_id


def test_login_never_sets_bump_enabled(server, conn):
    """Phase 4's own design sketch: the login flow grants DNS-tier
    access ONLY, never bump_enabled -- that stays a separate, deliberate
    admin action."""
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")
    _add_user(conn, "kid1", "correcthorse")

    _post(server, "kid1", "correcthorse")

    row = conn.execute("SELECT bump_enabled FROM devices WHERE mac_address = ?", (MAC_A,)).fetchone()
    assert row["bump_enabled"] == 0


def test_login_does_not_overwrite_an_existing_user_assignment(server, conn):
    """COALESCE semantics: an admin who already assigned this device to
    someone else keeps that assignment even if a different login
    happens to succeed against it (e.g. a shared family device)."""
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")
    device_id = conn.execute("SELECT device_id FROM device_bindings WHERE ipv4_address = ?", (IP_1,)).fetchone()["device_id"]
    original_owner_id = _add_user(conn, "parent1", "adminpw")
    conn.execute("UPDATE devices SET user_id = ? WHERE id = ?", (original_owner_id, device_id))
    conn.commit()
    _add_user(conn, "kid1", "correcthorse")

    _post(server, "kid1", "correcthorse")

    row = conn.execute("SELECT user_id, is_authenticated FROM devices WHERE id = ?", (device_id,)).fetchone()
    assert row["user_id"] == original_owner_id
    assert row["is_authenticated"] == 1


def test_wrong_password_does_not_authenticate(server, conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")
    _add_user(conn, "kid1", "correcthorse")

    status, body = _post(server, "kid1", "wrongpassword")

    assert status == 200
    assert "incorrect" in body.lower()
    row = conn.execute("SELECT is_authenticated FROM devices WHERE mac_address = ?", (MAC_A,)).fetchone()
    assert row["is_authenticated"] == 0


def test_unknown_username_does_not_authenticate(server, conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")

    status, body = _post(server, "nosuchkid", "whatever")

    assert status == 200
    assert "incorrect" in body.lower()


def test_login_from_an_ip_with_no_active_binding_fails_gracefully(server, conn):
    """By construction this request could only reach the server via
    nftables' own unauthenticated_v4 redirect, which requires an active
    binding to exist -- but this must still fail closed, not crash, if
    that assumption is ever violated (e.g. a real race)."""
    _add_user(conn, "kid1", "correcthorse")

    status, body = _post(server, "kid1", "correcthorse")

    assert status == 200
    assert "couldn't identify your device" in body


def test_login_ip_is_looked_up_independent_of_which_device_it_belongs_to(server, conn):
    """Two devices, only one of which is making this request -- login
    must only ever touch the device whose IP the request actually came
    from (127.0.0.1, since these are real loopback HTTP requests),
    never a different, unrelated device that merely happens to also be
    active in device_bindings at the same time."""
    identity.record_binding(conn, "aa:bb:cc:dd:ee:03", "192.168.1.99", source="rtnetlink")
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")
    _add_user(conn, "kid1", "correcthorse")

    _post(server, "kid1", "correcthorse")

    untouched = conn.execute("SELECT is_authenticated FROM devices WHERE mac_address = 'aa:bb:cc:dd:ee:03'").fetchone()
    touched = conn.execute("SELECT is_authenticated FROM devices WHERE mac_address = ?", (MAC_A,)).fetchone()
    assert untouched["is_authenticated"] == 0
    assert touched["is_authenticated"] == 1


# ============================================================
# Brute-force rate limiting -- built alongside the login form itself
# rather than retrofitted later, per this project's standing
# security-by-design practice (RoadMap.md's cross-cutting section).
# ============================================================

def test_is_rate_limited_pure_logic():
    ip = "203.0.113.5"  # TEST-NET-3, never a real request source here
    assert captive_portal_server._is_rate_limited(ip) is False
    for _ in range(captive_portal_server._MAX_ATTEMPTS):
        captive_portal_server._record_failed_attempt(ip)
    assert captive_portal_server._is_rate_limited(ip) is True


def test_clear_failed_attempts_resets_the_pure_logic():
    ip = "203.0.113.6"
    for _ in range(captive_portal_server._MAX_ATTEMPTS):
        captive_portal_server._record_failed_attempt(ip)
    assert captive_portal_server._is_rate_limited(ip) is True

    captive_portal_server._clear_failed_attempts(ip)

    assert captive_portal_server._is_rate_limited(ip) is False


def test_rate_limit_blocks_further_attempts_after_the_max(server, conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")
    _add_user(conn, "kid1", "correcthorse")

    for _ in range(captive_portal_server._MAX_ATTEMPTS):
        status, body = _post(server, "kid1", "wrongpassword")
        assert status == 200
        assert "incorrect" in body.lower()

    # The (max+1)th attempt is blocked outright -- even with the
    # CORRECT password this time, since a rate limiter that only
    # blocks incorrect guesses would let an attacker use up its own
    # budget testing wrong passwords and slip the right one in at the
    # end unrestricted.
    status, body = _post(server, "kid1", "correcthorse")
    assert status == 200
    assert "too many attempts" in body.lower()
    row = conn.execute("SELECT is_authenticated FROM devices WHERE mac_address = ?", (MAC_A,)).fetchone()
    assert row["is_authenticated"] == 0, "the rate-limited attempt must not have logged the device in"


def test_a_successful_login_clears_the_failure_count(server, conn):
    identity.record_binding(conn, MAC_A, IP_1, source="rtnetlink")
    _add_user(conn, "kid1", "correcthorse")

    for _ in range(captive_portal_server._MAX_ATTEMPTS - 1):
        _post(server, "kid1", "wrongpassword")
    status, body = _post(server, "kid1", "correcthorse")
    assert "signed in" in body.lower(), "one attempt below the limit must still succeed with the right password"

    assert captive_portal_server._is_rate_limited(IP_1) is False
