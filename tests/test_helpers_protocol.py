"""Drive the Squid helper scripts' actual decision logic and the shared
stdin/stdout protocol loop (common/squid_helper.py) directly -- no Docker,
no real Squid, no network. The helper modules' own `sys.path.insert(0,
"/opt/parental-proxy")` is a no-op off-container (that path doesn't exist),
which is fine: conftest.py already puts common/ and proxy/ on sys.path so
their real `import auth` / `import db` etc. resolve correctly either way.
"""
from __future__ import annotations

import io

import auth
import authz_helper
import basic_auth_helper
import db
import series_resolve
import sni_helper
import squid_helper


def _add_user(conn, username, password, display_name=None):
    conn.execute(
        "INSERT INTO users (username, display_name, password_hash, created_at) VALUES (?,?,?,?)",
        (username, display_name or username, auth.hash_password(password), db.now_iso()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def _add_domain(conn, pattern, mode, is_global=1, kind="generic"):
    conn.execute(
        "INSERT INTO domains (pattern, mode, kind, is_global, note, created_at) VALUES (?,?,?,?,NULL,?)",
        (pattern, mode, kind, is_global, db.now_iso()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM domains WHERE pattern = ?", (pattern,)).fetchone()


# ============================================================
# squid_helper.run() -- the shared protocol loop itself
# ============================================================

def _run_protocol(monkeypatch, lines, field_count, handler, **kwargs):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(lines) + "\n" if lines else ""))
    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    # squid_helper.run() opens its own db connection via db.get_conn(); the
    # `conn` fixture already monkeypatched db.DB_PATH for this test.
    squid_helper.run("test", field_count, handler, **kwargs)
    return out.getvalue().splitlines()


def test_protocol_ok_err_per_line(conn, monkeypatch):
    def handler(c, a, b):
        return a == "yes"

    replies = _run_protocol(monkeypatch, ["yes x", "no x"], 2, handler)
    assert replies == ["OK", "ERR"]


def test_protocol_wrong_field_count_is_err(conn, monkeypatch):
    def handler(c, a, b):
        return True

    replies = _run_protocol(monkeypatch, ["only-one-field"], 2, handler)
    assert replies == ["ERR"]


def test_protocol_unquotes_percent_encoded_fields_by_default(conn, monkeypatch):
    seen = {}

    def handler(c, a):
        seen["value"] = a
        return True

    _run_protocol(monkeypatch, ["hello%20world"], 1, handler)
    assert seen["value"] == "hello world"


def test_protocol_keep_trailing_spaces_for_password_field(conn, monkeypatch):
    seen = {}

    def handler(c, username, password):
        seen["password"] = password
        return True

    _run_protocol(
        monkeypatch, ["kid1 pass with spaces"], 2, handler,
        unquote=False, keep_trailing_spaces=True,
    )
    assert seen["password"] == "pass with spaces"


def test_protocol_handler_exception_is_err_and_does_not_kill_the_loop(conn, monkeypatch):
    def handler(c, a):
        if a == "boom":
            raise ValueError("kaboom")
        return True

    replies = _run_protocol(monkeypatch, ["boom", "ok"], 1, handler)
    assert replies == ["ERR", "OK"]


# ============================================================
# basic_auth_helper.check
# ============================================================

def test_basic_auth_correct_credentials(conn):
    _add_user(conn, "kid1", "s3cret")
    assert basic_auth_helper.check(conn, "kid1", "s3cret") is True


def test_basic_auth_wrong_password(conn):
    _add_user(conn, "kid1", "s3cret")
    assert basic_auth_helper.check(conn, "kid1", "wrong") is False


def test_basic_auth_unknown_user(conn):
    assert basic_auth_helper.check(conn, "nosuchuser", "whatever") is False


# ============================================================
# sni_helper -- ssl_bump step2 decisions
# ============================================================

def test_sni_handle_bump_true_only_for_bump_mode(conn):
    _add_domain(conn, r"crunchyroll\.com", mode="bump")
    _add_domain(conn, r"example\.com", mode="splice")
    assert sni_helper.handle_bump(conn, "-", "1.2.3.4", "crunchyroll.com") is True
    assert sni_helper.handle_bump(conn, "-", "1.2.3.4", "example.com") is False
    assert sni_helper.handle_bump(conn, "-", "1.2.3.4", "unknown.com") is False


def test_sni_handle_trusted_true_only_for_trusted_mode(conn):
    _add_domain(conn, r"crunchyrollcdn\.com", mode="trusted")
    assert sni_helper.handle_trusted(conn, "-", "1.2.3.4", "crunchyrollcdn.com") is True
    assert sni_helper.handle_trusted(conn, "-", "1.2.3.4", "elsewhere.com") is False


def test_sni_handle_splice_unauthenticated_denied_and_logged(conn):
    _add_domain(conn, r"example\.com", mode="splice")
    assert sni_helper.handle_splice(conn, "-", "192.168.1.5", "example.com") is False
    row = conn.execute("SELECT * FROM access_log").fetchone()
    assert row["reason"] == "not_authenticated"
    assert row["allowed"] == 0


def test_sni_handle_splice_outside_lan_denied_and_logged(conn):
    db.set_setting(conn, "local_network", "192.168.1.0/24")
    conn.commit()
    _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"example\.com", mode="splice")
    assert sni_helper.handle_splice(conn, "kid1", "10.0.0.9", "example.com") is False
    row = conn.execute("SELECT * FROM access_log").fetchone()
    assert row["reason"] == "outside_lan"


def test_sni_handle_splice_global_domain_allowed(conn):
    _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"example\.com", mode="splice", is_global=1)
    assert sni_helper.handle_splice(conn, "kid1", "192.168.1.5", "example.com") is True
    row = conn.execute("SELECT * FROM access_log").fetchone()
    assert row["allowed"] == 1
    assert row["reason"] == "global_domain"


def test_sni_handle_splice_per_user_domain_requires_assignment(conn):
    user = _add_user(conn, "kid1", "pw")
    domain = _add_domain(conn, r"example\.com", mode="splice", is_global=0)
    assert sni_helper.handle_splice(conn, "kid1", "192.168.1.5", "example.com") is False

    conn.execute("INSERT INTO user_domains (user_id, domain_id) VALUES (?,?)", (user["id"], domain["id"]))
    conn.commit()
    assert sni_helper.handle_splice(conn, "kid1", "192.168.1.5", "example.com") is True


def test_sni_handle_splice_wrong_mode_domain_denied(conn):
    _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"crunchyroll\.com", mode="bump", is_global=1)
    assert sni_helper.handle_splice(conn, "kid1", "192.168.1.5", "crunchyroll.com") is False


def test_sni_handle_block_page_terminate_default(conn):
    assert sni_helper.handle_block_page(conn, "-", "1.2.3.4", "anything.com") is False


def test_sni_handle_block_page_redirect_when_configured(conn):
    db.set_setting(conn, "block_page_mode", "redirect")
    conn.commit()
    assert sni_helper.handle_block_page(conn, "-", "1.2.3.4", "anything.com") is True


def test_sni_handle_block_page_terminate_logs_unconfigured_domain(conn):
    """GH #1: a genuinely unconfigured domain must be visible on the Report
    page even under the safe default (terminate) mode, since nothing else
    in the SNI-layer chain -- or downstream -- ever logs it otherwise."""
    _add_user(conn, "kid1", "pw")
    assert sni_helper.handle_block_page(conn, "kid1", "192.168.1.5", "unknown-site.example") is False
    row = conn.execute("SELECT * FROM access_log").fetchone()
    assert row is not None
    assert row["username"] == "kid1"
    assert row["domain"] == "unknown-site.example"
    assert row["path"] is None
    assert row["allowed"] == 0
    assert row["reason"] == "unknown_domain"


def test_sni_handle_block_page_terminate_unauthenticated_uses_placeholder(conn):
    sni_helper.handle_block_page(conn, "-", "192.168.1.5", "unknown-site.example")
    row = conn.execute("SELECT * FROM access_log").fetchone()
    assert row is not None
    assert row["username"] == "(unauthenticated)"
    assert row["user_id"] is None


def test_sni_handle_block_page_terminate_does_not_double_log_configured_domain(conn):
    """A configured splice-mode domain the user isn't permitted is already
    logged by handle_splice before this rule is ever reached -- logging it
    again here would just be a worse duplicate."""
    _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"example\.com", mode="splice", is_global=0)
    sni_helper.handle_block_page(conn, "kid1", "192.168.1.5", "example.com")
    assert conn.execute("SELECT * FROM access_log").fetchone() is None


def test_sni_handle_block_page_redirect_does_not_log(conn):
    """In redirect mode, authz_helper.decide() logs this same case with the
    real path once the connection is bumped -- logging it here too would
    just lose to the dedupe window (GH #5) and hide the richer entry."""
    db.set_setting(conn, "block_page_mode", "redirect")
    conn.commit()
    _add_user(conn, "kid1", "pw")
    sni_helper.handle_block_page(conn, "kid1", "192.168.1.5", "unknown-site.example")
    assert conn.execute("SELECT * FROM access_log").fetchone() is None


# ============================================================
# authz_helper.decide -- HTTP-layer decision on bump-mode domains
# ============================================================

def test_authz_unauthenticated_denied(conn):
    assert authz_helper.decide(conn, "-", "192.168.1.5", "example.com:443", "/") is False


def test_authz_outside_lan_denied_and_logged(conn):
    db.set_setting(conn, "local_network", "192.168.1.0/24")
    conn.commit()
    _add_user(conn, "kid1", "pw")
    assert authz_helper.decide(conn, "kid1", "10.0.0.1", "example.com:443", "/") is False
    row = conn.execute("SELECT * FROM access_log").fetchone()
    assert row["reason"] == "outside_lan"


def test_authz_unknown_domain_denied(conn):
    _add_user(conn, "kid1", "pw")
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "unknown.example:443", "/") is False
    row = conn.execute("SELECT * FROM access_log").fetchone()
    assert row["reason"] == "unknown_domain"


def test_authz_splice_mode_domain_denied_as_not_bump_mode(conn):
    _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"example\.com", mode="splice", is_global=1)
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "example.com:443", "/") is False
    row = conn.execute("SELECT * FROM access_log").fetchone()
    assert row["reason"] == "not_bump_mode"


def test_authz_bump_domain_not_assigned_to_user_denied(conn):
    _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"example\.com", mode="bump", is_global=0)
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "example.com:443", "/") is False
    row = conn.execute("SELECT * FROM access_log").fetchone()
    assert row["reason"] == "domain_not_assigned"


def test_authz_generic_bump_domain_no_path_rules_allows_any_path(conn):
    _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"example\.com", mode="bump", is_global=1)
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "example.com:443", "/whatever") is True


def test_authz_generic_bump_domain_with_path_rules_enforces_them(conn):
    _add_user(conn, "kid1", "pw")
    domain = _add_domain(conn, r"example\.com", mode="bump", is_global=1)
    conn.execute("INSERT INTO domain_paths (domain_id, pattern) VALUES (?, ?)", (domain["id"], r"^/allowed"))
    conn.commit()
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "example.com:443", "/allowed/x") is True
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "example.com:443", "/blocked") is False


def test_authz_strips_port_from_dst(conn):
    _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"example\.com", mode="bump", is_global=1)
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "example.com:8443", "/") is True


def test_authz_crunchyroll_cms_objects_always_allowed(conn):
    user = _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"crunchyroll\.com", mode="bump", is_global=1, kind="crunchyroll")
    assert authz_helper.decide(
        conn, "kid1", "192.168.1.5", "www.crunchyroll.com:443",
        "/content/v2/cms/objects/GYE5K0XVR",
    ) is True


def test_authz_crunchyroll_series_page_requires_approval(conn):
    user = _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"crunchyroll\.com", mode="bump", is_global=1, kind="crunchyroll")
    path = "/series/GYE5K0XVR/ace-attorney"
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "www.crunchyroll.com:443", path) is False

    conn.execute(
        "INSERT INTO user_shows (user_id, series_id, series_name) VALUES (?, 'GYE5K0XVR', 'Ace Attorney')",
        (user["id"],),
    )
    conn.commit()
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "www.crunchyroll.com:443", path) is True


def test_authz_crunchyroll_watch_page_resolves_series_and_checks_approval(conn, monkeypatch):
    user = _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"crunchyroll\.com", mode="bump", is_global=1, kind="crunchyroll")

    monkeypatch.setattr(
        series_resolve, "resolve_series_ids", lambda c, ids: {i: "GYE5K0XVR" for i in ids}
    )
    path = "/watch/G6NQ5DWX6/episode-1"
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "www.crunchyroll.com:443", path) is False

    conn.execute(
        "INSERT INTO user_shows (user_id, series_id, series_name) VALUES (?, 'GYE5K0XVR', 'Ace Attorney')",
        (user["id"],),
    )
    conn.commit()
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "www.crunchyroll.com:443", path) is True


def test_authz_crunchyroll_resolution_failure_fails_closed(conn, monkeypatch):
    _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"crunchyroll\.com", mode="bump", is_global=1, kind="crunchyroll")

    monkeypatch.setattr(series_resolve, "resolve_series_ids", lambda c, ids: None)
    path = "/watch/G6NQ5DWX6/episode-1"
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "www.crunchyroll.com:443", path) is False
    row = conn.execute("SELECT * FROM access_log").fetchone()
    assert row["reason"] == "resolution_failed"


def test_authz_crunchyroll_blocked_shape_denied(conn):
    _add_user(conn, "kid1", "pw")
    _add_domain(conn, r"crunchyroll\.com", mode="bump", is_global=1, kind="crunchyroll")
    # '/watch/' marker present but id has an invalid character -> BLOCKED_SHAPE
    path = "/watch/bad!id"
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "www.crunchyroll.com:443", path) is False
    row = conn.execute("SELECT * FROM access_log").fetchone()
    assert row["reason"] == "blocked_shape"


def test_authz_crunchyroll_other_shape_falls_back_to_path_allowlist(conn):
    _add_user(conn, "kid1", "pw")
    domain = _add_domain(conn, r"crunchyroll\.com", mode="bump", is_global=1, kind="crunchyroll")
    conn.execute("INSERT INTO domain_paths (domain_id, pattern) VALUES (?, ?)", (domain["id"], r"^/discover"))
    conn.commit()
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "www.crunchyroll.com:443", "/discover") is True
    assert authz_helper.decide(conn, "kid1", "192.168.1.5", "www.crunchyroll.com:443", "/not-configured") is False
