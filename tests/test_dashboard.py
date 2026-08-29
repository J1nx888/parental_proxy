"""dashboard/dashboard.py via Flask's test client -- no real HTTP server.

dashboard.py runs bootstrap_admin() and reads its secret key at *import*
time, against whatever db.DB_PATH is active then. To get a fully isolated
dashboard (fresh DB, known admin credentials) per test, the `client` fixture
below monkeypatches db.DB_PATH and the DASHBOARD_USER/PASSWORD env vars
*before* importing the module, then importlib.reload()s it if it was
already imported by an earlier test -- reload re-runs the whole module body,
including a fresh `app = Flask(__name__)` and a fresh bootstrap_admin().
"""
from __future__ import annotations

import base64
import importlib

import pytest

ADMIN_USER = "admin"
ADMIN_PASSWORD = "testpass123"


def _auth_header(username=ADMIN_USER, password=ADMIN_PASSWORD):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def dashboard_app(monkeypatch, tmp_path):
    monkeypatch.setenv("DASHBOARD_USER", ADMIN_USER)
    monkeypatch.setenv("DASHBOARD_PASSWORD", ADMIN_PASSWORD)
    monkeypatch.delenv("LOCAL_NETWORK", raising=False)

    import db as db_mod
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "dashboard_test.db")

    import dashboard
    importlib.reload(dashboard)
    dashboard.app.testing = True
    return dashboard.app


@pytest.fixture
def client(dashboard_app):
    return dashboard_app.test_client()


@pytest.fixture
def db_conn(dashboard_app):
    import db as db_mod
    conn = db_mod.get_conn()
    yield conn
    conn.close()


# ============================================================
# ADMIN AUTH
# ============================================================

def test_unauthenticated_request_gets_401(client):
    resp = client.get("/users")
    assert resp.status_code == 401


def test_wrong_password_gets_401(client):
    resp = client.get("/users", headers=_auth_header(password="nope"))
    assert resp.status_code == 401


def test_correct_credentials_get_200(client):
    resp = client.get("/users", headers=_auth_header())
    assert resp.status_code == 200


def test_ca_cert_route_requires_no_auth(client):
    # Public endpoint; 404 is expected here (no cert generated in this
    # sandbox) but it must NOT be 401 -- that would mean auth leaked onto it.
    resp = client.get("/ca-cert")
    assert resp.status_code != 401


def test_index_redirects_to_report(client):
    resp = client.get("/", headers=_auth_header())
    assert resp.status_code == 302
    assert "/report" in resp.headers["Location"]


# ============================================================
# USER CRUD
# ============================================================

def test_add_user_then_appears_in_list(client, db_conn):
    resp = client.post(
        "/users/add", data={"username": "kid1", "display_name": "Kid One", "password": "pw123"},
        headers=_auth_header(),
    )
    assert resp.status_code == 302
    row = db_conn.execute("SELECT * FROM users WHERE username = 'kid1'").fetchone()
    assert row is not None
    assert row["display_name"] == "Kid One"


def test_add_user_missing_password_rejected(client, db_conn):
    resp = client.post("/users/add", data={"username": "kid1"}, headers=_auth_header())
    assert resp.status_code == 302
    assert "error=1" in resp.headers["Location"]
    assert db_conn.execute("SELECT * FROM users WHERE username = 'kid1'").fetchone() is None


def test_add_user_invalid_username_rejected(client, db_conn):
    resp = client.post(
        "/users/add", data={"username": "bad user!", "password": "pw"}, headers=_auth_header()
    )
    assert "error=1" in resp.headers["Location"]
    assert db_conn.execute("SELECT * FROM users").fetchone() is None


def test_add_user_duplicate_username_rejected(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    resp = client.post("/users/add", data={"username": "kid1", "password": "pw2"}, headers=_auth_header())
    assert "error=1" in resp.headers["Location"]
    count = db_conn.execute("SELECT COUNT(*) c FROM users WHERE username = 'kid1'").fetchone()["c"]
    assert count == 1


def test_delete_user_removes_row(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()["id"]
    resp = client.post("/users/delete", data={"user_id": user_id}, headers=_auth_header())
    assert resp.status_code == 302
    assert db_conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone() is None


def test_reset_password_changes_hash(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()["id"]
    old_hash = db_conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()[0]

    client.post(
        "/users/reset-password", data={"user_id": user_id, "password": "newpw"}, headers=_auth_header()
    )
    new_hash = db_conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    assert new_hash != old_hash

    import auth
    assert auth.verify_password("newpw", new_hash) is True


# ============================================================
# DOMAIN CRUD
# ============================================================

def test_add_domain_then_appears(client, db_conn):
    resp = client.post(
        "/domains/add",
        data={"pattern": r"example\.com", "mode": "splice", "note": "test"},
        headers=_auth_header(),
    )
    assert resp.status_code == 302
    row = db_conn.execute("SELECT * FROM domains WHERE pattern = ?", (r"example\.com",)).fetchone()
    assert row is not None
    assert row["mode"] == "splice"
    assert row["is_global"] == 0


def test_add_domain_invalid_regex_rejected(client, db_conn):
    resp = client.post(
        "/domains/add", data={"pattern": "(unbalanced", "mode": "splice"}, headers=_auth_header()
    )
    assert "error=1" in resp.headers["Location"]
    assert db_conn.execute("SELECT * FROM domains").fetchone() is None


def test_add_domain_invalid_mode_rejected(client, db_conn):
    resp = client.post(
        "/domains/add", data={"pattern": r"example\.com", "mode": "not-a-mode"}, headers=_auth_header()
    )
    assert "error=1" in resp.headers["Location"]


def test_delete_domain_removes_row(client, db_conn):
    client.post("/domains/add", data={"pattern": r"example\.com", "mode": "splice"}, headers=_auth_header())
    domain_id = db_conn.execute("SELECT id FROM domains WHERE pattern = ?", (r"example\.com",)).fetchone()[0]
    client.post("/domains/delete", data={"domain_id": domain_id}, headers=_auth_header())
    assert db_conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone() is None


def test_delete_domain_refuses_crunchyroll_builtin(client, db_conn):
    db_conn.execute(
        "INSERT INTO domains (pattern, mode, kind, is_global, note, created_at) "
        "VALUES ('crunchyroll\\.com', 'bump', 'crunchyroll', 1, NULL, datetime('now'))"
    )
    db_conn.commit()
    domain_id = db_conn.execute("SELECT id FROM domains WHERE kind = 'crunchyroll'").fetchone()[0]
    resp = client.post("/domains/delete", data={"domain_id": domain_id}, headers=_auth_header())
    assert "error=1" in resp.headers["Location"]
    assert db_conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone() is not None


def test_toggle_user_domain_add_and_remove(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    client.post("/domains/add", data={"pattern": r"example\.com", "mode": "splice"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    domain_id = db_conn.execute("SELECT id FROM domains WHERE pattern = ?", (r"example\.com",)).fetchone()[0]

    client.post(
        "/domains/toggle-user",
        data={"domain_id": domain_id, "user_id": user_id, "action": "add"},
        headers=_auth_header(),
    )
    assert db_conn.execute(
        "SELECT 1 FROM user_domains WHERE user_id = ? AND domain_id = ?", (user_id, domain_id)
    ).fetchone() is not None

    client.post(
        "/domains/toggle-user",
        data={"domain_id": domain_id, "user_id": user_id, "action": "remove"},
        headers=_auth_header(),
    )
    assert db_conn.execute(
        "SELECT 1 FROM user_domains WHERE user_id = ? AND domain_id = ?", (user_id, domain_id)
    ).fetchone() is None


def test_add_path_and_delete_path(client, db_conn):
    client.post("/domains/add", data={"pattern": r"example\.com", "mode": "bump"}, headers=_auth_header())
    domain_id = db_conn.execute("SELECT id FROM domains WHERE pattern = ?", (r"example\.com",)).fetchone()[0]

    client.post(
        "/domains/paths/add", data={"domain_id": domain_id, "pattern": r"^/ok"}, headers=_auth_header()
    )
    path_row = db_conn.execute("SELECT * FROM domain_paths WHERE domain_id = ?", (domain_id,)).fetchone()
    assert path_row is not None

    client.post("/domains/paths/delete", data={"path_id": path_row["id"]}, headers=_auth_header())
    assert db_conn.execute("SELECT * FROM domain_paths WHERE id = ?", (path_row["id"],)).fetchone() is None


# ============================================================
# REPORT / APPROVE
# ============================================================

def test_approve_blocked_site_from_report_grants_access(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    db_conn.execute(
        "INSERT INTO access_log (ts, user_id, username, domain, path, allowed, reason) "
        "VALUES (datetime('now'), ?, 'kid1', 'newsite.example', NULL, 0, 'unknown_domain')",
        (user_id,),
    )
    db_conn.commit()
    log_id = db_conn.execute("SELECT id FROM access_log").fetchone()[0]

    resp = client.post("/report/approve", data={"log_id": log_id}, headers=_auth_header())
    assert resp.status_code == 302
    assert "error" not in resp.headers["Location"] or "error=1" not in resp.headers["Location"]

    import matching
    domain = matching.find_domain(db_conn, "newsite.example")
    assert domain is not None
    assert matching.user_has_domain(db_conn, user_id, domain["id"]) is True
    # Auto-created domain must be scoped to this user only, not global.
    assert domain["is_global"] == 0


def test_approve_blocked_show_from_report_grants_access(client, db_conn, monkeypatch):
    import dashboard
    monkeypatch.setattr(dashboard.cr_api, "series_title", lambda series_id, timeout=5.0: "Ace Attorney")

    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    db_conn.execute(
        "INSERT INTO access_log "
        "(ts, user_id, username, domain, path, series_id, series_name, allowed, reason) "
        "VALUES (datetime('now'), ?, 'kid1', 'www.crunchyroll.com', '/watch/x', 'GYE5K0XVR', NULL, 0, 'show_not_approved')",
        (user_id,),
    )
    db_conn.commit()
    log_id = db_conn.execute("SELECT id FROM access_log").fetchone()[0]

    client.post("/report/approve", data={"log_id": log_id}, headers=_auth_header())

    import matching
    assert matching.user_has_show(db_conn, user_id, "GYE5K0XVR") is True


def test_approve_missing_log_entry_errors(client):
    resp = client.post("/report/approve", data={"log_id": "999999"}, headers=_auth_header())
    assert "error=1" in resp.headers["Location"]


def test_approve_path_not_allowed_redirects_to_prefilled_add_path_form(client, db_conn):
    """GH #6: approving a path-blocked row used to be a silent no-op
    (re-asserting a domain assignment that already existed). It must now
    send the admin to review a derived pattern instead of auto-saving one
    or doing nothing."""
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    client.post("/domains/add", data={"pattern": r"asurascans\.example", "mode": "bump", "is_global": "on"}, headers=_auth_header())
    domain_id = db_conn.execute("SELECT id FROM domains WHERE pattern = ?", (r"asurascans\.example",)).fetchone()[0]
    db_conn.execute(
        "INSERT INTO access_log (ts, user_id, username, domain, path, allowed, reason) "
        "VALUES (datetime('now'), ?, 'kid1', 'asurascans.example', '/comics/some-comic?ref=1', 0, 'path_not_allowed')",
        (user_id,),
    )
    db_conn.commit()
    log_id = db_conn.execute("SELECT id FROM access_log").fetchone()[0]

    resp = client.post("/report/approve", data={"log_id": log_id}, headers=_auth_header())
    assert resp.status_code == 302
    location = resp.headers["Location"]
    assert f"/domains/{domain_id}" in location
    assert "prefill_path=" in location
    # No domain_paths row should be silently created -- the admin must
    # actually submit the (possibly edited) Add Path form for that.
    assert db_conn.execute("SELECT * FROM domain_paths WHERE domain_id = ?", (domain_id,)).fetchone() is None

    # Following the redirect shows the derived pattern in the form, ready
    # to review/edit before saving -- query string stripped, anchored,
    # escaped.
    detail_resp = client.get(location, headers=_auth_header())
    assert rb'value="^/comics/some\-comic"' in detail_resp.data or b"^/comics/some-comic" in detail_resp.data


def test_path_to_pattern_strips_query_anchors_and_escapes():
    import dashboard
    assert dashboard.path_to_pattern("/comics/some-comic?ref=1") == r"^/comics/some\-comic"
    assert dashboard.path_to_pattern("/a.b/c") == r"^/a\.b/c"
    assert dashboard.path_to_pattern(None) == "^/"
    assert dashboard.path_to_pattern("") == "^/"


def test_report_page_lists_logged_rows(client, db_conn):
    db_conn.execute(
        "INSERT INTO access_log (ts, user_id, username, domain, path, allowed, reason) "
        "VALUES (datetime('now'), NULL, 'kid1', 'example.com', '/', 1, 'global_domain')"
    )
    db_conn.commit()
    resp = client.get("/report", headers=_auth_header())
    assert resp.status_code == 200
    assert b"example.com" in resp.data


# ============================================================
# CSRF / cross-origin guard
# ============================================================

def test_cross_origin_post_is_rejected(client):
    resp = client.post(
        "/users/add",
        data={"username": "kid1", "password": "pw"},
        headers={**_auth_header(), "Origin": "http://evil.example"},
    )
    assert resp.status_code == 403


def test_same_origin_post_is_allowed(client):
    resp = client.post(
        "/users/add",
        data={"username": "kid1", "password": "pw"},
        headers={**_auth_header(), "Origin": "http://localhost"},
        base_url="http://localhost/",
    )
    assert resp.status_code == 302


def test_post_without_origin_or_referer_is_allowed(client):
    # Non-browser client (curl, a script) -- no ambient credentials to abuse.
    resp = client.post(
        "/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header()
    )
    assert resp.status_code == 302


# ============================================================
# user_detail / add_show / remove_show
# ============================================================

def test_user_detail_page_renders(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "display_name": "Kid One", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    resp = client.get(f"/users/{user_id}", headers=_auth_header())
    assert resp.status_code == 200
    assert b"Kid One" in resp.data


def test_user_detail_unknown_id_redirects_with_error(client):
    resp = client.get("/users/999999", headers=_auth_header())
    assert resp.status_code == 302
    assert "error=1" in resp.headers["Location"]


def test_add_show_invalid_url_rejected(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    resp = client.post(
        "/shows/add", data={"user_id": user_id, "url": "https://example.com/not-crunchyroll"},
        headers=_auth_header(),
    )
    assert "error=1" in resp.headers["Location"]
    assert db_conn.execute("SELECT * FROM user_shows WHERE user_id = ?", (user_id,)).fetchone() is None


def test_add_show_uses_override_name_without_hitting_cr_api(client, db_conn):
    """No cr_api mock installed -- an explicit name must short-circuit the
    cr_api.series_title() lookup entirely (block_network would otherwise
    fail the request)."""
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    resp = client.post(
        "/shows/add",
        data={
            "user_id": user_id,
            "url": "https://www.crunchyroll.com/series/GYE5K0XVR/ace-attorney",
            "name": "My Custom Name",
        },
        headers=_auth_header(),
    )
    assert resp.status_code == 302
    row = db_conn.execute("SELECT * FROM user_shows WHERE user_id = ?", (user_id,)).fetchone()
    assert row["series_id"] == "GYE5K0XVR"
    assert row["series_name"] == "My Custom Name"


def test_add_show_falls_back_to_url_slug_when_cr_api_unavailable(client, db_conn):
    """No name override and no cr_api mock: series_title() hits the blocked
    network, catches the failure internally, and add_show falls back to the
    slug-derived name rather than erroring out."""
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    resp = client.post(
        "/shows/add",
        data={"user_id": user_id, "url": "https://www.crunchyroll.com/series/GYE5K0XVR/ace-attorney"},
        headers=_auth_header(),
    )
    assert resp.status_code == 302
    row = db_conn.execute("SELECT * FROM user_shows WHERE user_id = ?", (user_id,)).fetchone()
    assert row["series_name"] == "Ace Attorney"  # derived from the slug


def test_add_show_uses_cr_api_title_when_mocked(client, db_conn, monkeypatch):
    import dashboard
    monkeypatch.setattr(dashboard.cr_api, "series_title", lambda series_id, timeout=5.0: "Real CR Title")

    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    client.post(
        "/shows/add",
        data={"user_id": user_id, "url": "https://www.crunchyroll.com/series/GYE5K0XVR/ace-attorney"},
        headers=_auth_header(),
    )
    row = db_conn.execute("SELECT * FROM user_shows WHERE user_id = ?", (user_id,)).fetchone()
    assert row["series_name"] == "Real CR Title"


def test_remove_show_deletes_row(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    client.post(
        "/shows/add",
        data={"user_id": user_id, "url": "https://www.crunchyroll.com/series/GYE5K0XVR/ace-attorney", "name": "X"},
        headers=_auth_header(),
    )
    assert db_conn.execute("SELECT * FROM user_shows WHERE user_id = ?", (user_id,)).fetchone() is not None

    client.post("/shows/remove", data={"user_id": user_id, "series_id": "GYE5K0XVR"}, headers=_auth_header())
    assert db_conn.execute("SELECT * FROM user_shows WHERE user_id = ?", (user_id,)).fetchone() is None


# ============================================================
# domains filtered by user (GH #2)
# ============================================================

def test_domains_filtered_by_user_shows_only_assigned_and_global(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    client.post("/users/add", data={"username": "kid2", "password": "pw"}, headers=_auth_header())
    user1_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    user2_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid2'").fetchone()[0]

    client.post("/domains/add", data={"pattern": r"global\.example", "mode": "splice", "is_global": "on"}, headers=_auth_header())
    client.post("/domains/add", data={"pattern": r"kid1-only\.example", "mode": "splice"}, headers=_auth_header())
    client.post("/domains/add", data={"pattern": r"kid2-only\.example", "mode": "splice"}, headers=_auth_header())

    global_id = db_conn.execute("SELECT id FROM domains WHERE pattern = ?", (r"global\.example",)).fetchone()[0]
    kid1_domain_id = db_conn.execute("SELECT id FROM domains WHERE pattern = ?", (r"kid1-only\.example",)).fetchone()[0]
    client.post("/domains/toggle-user", data={"domain_id": kid1_domain_id, "user_id": user1_id, "action": "add"}, headers=_auth_header())

    resp = client.get(f"/domains?user_id={user1_id}", headers=_auth_header())
    assert resp.status_code == 200
    assert rb"global\.example" in resp.data
    assert rb"kid1-only\.example" in resp.data
    assert rb"kid2-only\.example" not in resp.data
    assert b"clear filter" in resp.data


def test_domains_unfiltered_shows_everything(client, db_conn):
    client.post("/domains/add", data={"pattern": r"a\.example", "mode": "splice"}, headers=_auth_header())
    client.post("/domains/add", data={"pattern": r"b\.example", "mode": "splice"}, headers=_auth_header())
    resp = client.get("/domains", headers=_auth_header())
    assert rb"a\.example" in resp.data
    assert rb"b\.example" in resp.data
    assert b"clear filter" not in resp.data


def test_users_page_sites_link_includes_user_id(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    resp = client.get("/users", headers=_auth_header())
    assert f"/domains?user_id={user_id}".encode() in resp.data


# ============================================================
# GH #6: paste-a-URL one-step page approval
# ============================================================

def test_add_domain_from_url_creates_bump_domain_path_and_assignment(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]

    resp = client.post(
        "/domains/add-url",
        data={"url": "https://asurascans.example/comics/some-comic?ref=1", "user_id": str(user_id)},
        headers=_auth_header(),
    )
    assert resp.status_code == 302
    assert f"user_id={user_id}".encode() in resp.headers["Location"].encode()

    domain = db_conn.execute("SELECT * FROM domains WHERE pattern = ?", (r"asurascans\.example",)).fetchone()
    assert domain is not None
    assert domain["mode"] == "bump"
    assert domain["is_global"] == 0

    path_row = db_conn.execute("SELECT * FROM domain_paths WHERE domain_id = ?", (domain["id"],)).fetchone()
    assert path_row is not None
    assert path_row["pattern"] == r"^/comics/some\-comic"

    assert db_conn.execute(
        "SELECT 1 FROM user_domains WHERE user_id = ? AND domain_id = ?", (user_id, domain["id"])
    ).fetchone() is not None


def test_add_domain_from_url_reuses_existing_bump_domain(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    client.post("/domains/add", data={"pattern": r"asurascans\.example", "mode": "bump", "is_global": "on"}, headers=_auth_header())

    client.post(
        "/domains/add-url",
        data={"url": "https://asurascans.example/comics/another-one", "user_id": str(user_id)},
        headers=_auth_header(),
    )
    count = db_conn.execute("SELECT COUNT(*) c FROM domains WHERE pattern = ?", (r"asurascans\.example",)).fetchone()["c"]
    assert count == 1  # no duplicate domain row created


def test_add_domain_from_url_rejects_non_bump_existing_domain(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    client.post("/domains/add", data={"pattern": r"asurascans\.example", "mode": "splice", "is_global": "on"}, headers=_auth_header())

    resp = client.post(
        "/domains/add-url",
        data={"url": "https://asurascans.example/comics/some-comic", "user_id": str(user_id)},
        headers=_auth_header(),
    )
    assert "error=1" in resp.headers["Location"]
    assert db_conn.execute("SELECT * FROM domain_paths").fetchone() is None


def test_add_domain_from_url_without_user_id_errors(client, db_conn):
    resp = client.post(
        "/domains/add-url", data={"url": "https://asurascans.example/comics/x"}, headers=_auth_header()
    )
    assert "error=1" in resp.headers["Location"]
    assert db_conn.execute("SELECT * FROM domains").fetchone() is None


def test_add_domain_from_url_with_invalid_user_id_errors(client, db_conn):
    resp = client.post(
        "/domains/add-url",
        data={"url": "https://asurascans.example/comics/x", "user_id": "999999"},
        headers=_auth_header(),
    )
    assert "error=1" in resp.headers["Location"]
    assert db_conn.execute("SELECT * FROM domains").fetchone() is None


def test_add_domain_from_url_with_invalid_url_errors(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    resp = client.post(
        "/domains/add-url", data={"url": "not a url at all!!", "user_id": str(user_id)}, headers=_auth_header()
    )
    assert "error=1" in resp.headers["Location"]


def test_domains_filtered_view_shows_paste_url_form(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    resp = client.get(f"/domains?user_id={user_id}", headers=_auth_header())
    assert b"add-url" in resp.data


def test_domains_unfiltered_view_hides_paste_url_form(client, db_conn):
    resp = client.get("/domains", headers=_auth_header())
    assert b"add-url" not in resp.data


def test_domains_filter_with_nonexistent_user_id_shows_error(client, db_conn):
    """Code-review fix: an invalid/stale user_id must surface an error like
    the rest of this file's "no longer exists" convention, not silently
    fall back to the unfiltered list."""
    resp = client.get("/domains?user_id=999999", headers=_auth_header())
    assert resp.status_code == 302
    assert "error=1" in resp.headers["Location"]


def test_users_page_assigned_count_includes_global_domains(client, db_conn):
    """Code-review fix: the "N assigned" count must match what its own
    ?user_id= link actually shows -- explicit assignments plus every
    global domain, not just explicit assignments."""
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    client.post("/domains/add", data={"pattern": r"global\.example", "mode": "splice", "is_global": "on"}, headers=_auth_header())
    resp = client.get("/users", headers=_auth_header())
    assert b"1 assigned" in resp.data


def test_add_domain_from_filtered_view_preserves_filter(client, db_conn):
    """Code-review fix: add_domain must forward ?user_id= through its
    redirect so the admin stays in the filtered view they were on."""
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    resp = client.post(
        "/domains/add",
        data={"pattern": r"new\.example", "mode": "splice", "user_id": str(user_id)},
        headers=_auth_header(),
    )
    assert resp.status_code == 302
    assert f"user_id={user_id}".encode() in resp.headers["Location"].encode()


def test_delete_domain_from_filtered_view_preserves_filter(client, db_conn):
    """Code-review fix: delete_domain must forward ?user_id= through its
    redirect so the admin stays in the filtered view they were on."""
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    client.post("/domains/add", data={"pattern": r"new\.example", "mode": "splice"}, headers=_auth_header())
    domain_id = db_conn.execute("SELECT id FROM domains WHERE pattern = ?", (r"new\.example",)).fetchone()[0]
    resp = client.post(
        "/domains/delete", data={"domain_id": domain_id, "user_id": str(user_id)}, headers=_auth_header()
    )
    assert resp.status_code == 302
    assert f"user_id={user_id}".encode() in resp.headers["Location"].encode()


def test_add_domain_without_filter_does_not_add_user_id_to_redirect(client, db_conn):
    resp = client.post(
        "/domains/add", data={"pattern": r"unfiltered\.example", "mode": "splice"}, headers=_auth_header()
    )
    assert b"user_id" not in resp.headers["Location"].encode()


# ============================================================
# domain_detail / update_domain
# ============================================================

def test_domain_detail_page_renders(client, db_conn):
    client.post("/domains/add", data={"pattern": r"example\.com", "mode": "splice"}, headers=_auth_header())
    domain_id = db_conn.execute("SELECT id FROM domains WHERE pattern = ?", (r"example\.com",)).fetchone()[0]
    resp = client.get(f"/domains/{domain_id}", headers=_auth_header())
    assert resp.status_code == 200
    assert b"example" in resp.data


def test_domain_detail_unknown_id_redirects_with_error(client):
    resp = client.get("/domains/999999", headers=_auth_header())
    assert resp.status_code == 302
    assert "error=1" in resp.headers["Location"]


def test_update_domain_changes_mode_and_global_flag(client, db_conn):
    client.post("/domains/add", data={"pattern": r"example\.com", "mode": "splice"}, headers=_auth_header())
    domain_id = db_conn.execute("SELECT id FROM domains WHERE pattern = ?", (r"example\.com",)).fetchone()[0]

    resp = client.post(
        "/domains/update",
        data={"domain_id": domain_id, "mode": "bump", "is_global": "on", "note": "updated"},
        headers=_auth_header(),
    )
    assert resp.status_code == 302
    row = db_conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()
    assert row["mode"] == "bump"
    assert row["is_global"] == 1
    assert row["note"] == "updated"


# ============================================================
# SETTINGS
# ============================================================

def test_settings_page_renders(client):
    resp = client.get("/settings", headers=_auth_header())
    assert resp.status_code == 200
    assert b"admin" in resp.data.lower()


def test_update_local_network_saves_value(client, db_conn):
    resp = client.post(
        "/settings/local-network", data={"local_network": "10.0.0.0/8"}, headers=_auth_header()
    )
    assert resp.status_code == 302
    import db
    assert db.get_setting(db_conn, "local_network") == "10.0.0.0/8"


def test_update_local_network_blank_disables_check(client, db_conn):
    client.post("/settings/local-network", data={"local_network": ""}, headers=_auth_header())
    import db
    assert db.get_setting(db_conn, "local_network") == ""
    import matching
    assert matching.ip_in_configured_lan(db_conn, "8.8.8.8") is True


def test_update_block_page_mode_valid_value_saved(client, db_conn):
    resp = client.post(
        "/settings/block-page-mode", data={"block_page_mode": "redirect"}, headers=_auth_header()
    )
    assert resp.status_code == 302
    import db
    assert db.get_setting(db_conn, "block_page_mode") == "redirect"


def test_update_block_page_mode_invalid_value_rejected(client, db_conn):
    resp = client.post(
        "/settings/block-page-mode", data={"block_page_mode": "not-a-mode"}, headers=_auth_header()
    )
    assert "error=1" in resp.headers["Location"]


def test_update_admin_changes_username_and_password(client, db_conn):
    resp = client.post(
        "/settings/admin",
        data={"admin_username": "newadmin", "admin_password": "newpassword123"},
        headers=_auth_header(),
    )
    assert resp.status_code == 302

    # Old credentials no longer work.
    assert client.get("/users", headers=_auth_header()).status_code == 401
    # New credentials do.
    resp = client.get("/users", headers=_auth_header(username="newadmin", password="newpassword123"))
    assert resp.status_code == 200


def test_update_admin_blank_username_rejected(client, db_conn):
    resp = client.post(
        "/settings/admin", data={"admin_username": "", "admin_password": ""}, headers=_auth_header()
    )
    assert "error=1" in resp.headers["Location"]
    # Original credentials still work.
    assert client.get("/users", headers=_auth_header()).status_code == 200


def test_update_admin_blank_password_keeps_current_password(client, db_conn):
    client.post(
        "/settings/admin", data={"admin_username": ADMIN_USER, "admin_password": ""},
        headers=_auth_header(),
    )
    # Original password still works since it was left blank on the form.
    assert client.get("/users", headers=_auth_header()).status_code == 200


# ============================================================
# /blocked: "Request approval" + admin Dismiss/Approve
# ============================================================

def _insert_recent_denial(db_conn, user_id, domain="newsite.example", path=None):
    # Must match db.now_iso()'s exact format (T/Z, not SQLite's datetime('now')
    # 'YYYY-MM-DD HH:MM:SS') -- /blocked's lookback filters with `ts >= ?`
    # against db.iso_secs_ago(), and the two formats don't compare correctly
    # against each other lexicographically (space sorts before 'T').
    import db as db_mod
    db_conn.execute(
        "INSERT INTO access_log (ts, user_id, username, domain, path, allowed, reason) "
        "VALUES (?, ?, 'kid1', ?, ?, 0, 'unknown_domain')",
        (db_mod.now_iso(), user_id, domain, path),
    )
    db_conn.commit()
    return db_conn.execute("SELECT id FROM access_log ORDER BY id DESC LIMIT 1").fetchone()[0]


def test_blocked_page_offers_request_button_for_a_recent_denial(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    _insert_recent_denial(db_conn, user_id)

    resp = client.get("/blocked")
    assert resp.status_code == 403
    assert b"Request approval" in resp.data


def test_blocked_page_has_no_button_with_no_recent_denial(client):
    resp = client.get("/blocked")
    assert b"Request approval" not in resp.data
    assert b"Request sent" not in resp.data


def test_request_approval_sets_flag_and_blocked_page_reflects_it(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    log_id = _insert_recent_denial(db_conn, user_id)

    resp = client.post("/blocked/request-approval", data={"log_id": log_id})
    assert resp.status_code == 302

    row = db_conn.execute("SELECT approval_requested_at FROM access_log WHERE id = ?", (log_id,)).fetchone()
    assert row["approval_requested_at"] is not None

    # No admin auth needed for either the page or the click -- the kid on
    # the blocked device isn't logged into the dashboard.
    resp = client.get("/blocked")
    assert b"Request sent" in resp.data
    assert b"Request approval" not in resp.data


def test_report_shows_pending_request_badge_and_card(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    log_id = _insert_recent_denial(db_conn, user_id)
    client.post("/blocked/request-approval", data={"log_id": log_id})

    resp = client.get("/report", headers=_auth_header())
    assert b"Pending approval requests" in resp.data
    assert b"newsite.example" in resp.data


def test_dismiss_request_clears_flag_without_granting_access(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    log_id = _insert_recent_denial(db_conn, user_id)
    client.post("/blocked/request-approval", data={"log_id": log_id})

    resp = client.post("/report/dismiss-request", data={"log_id": log_id}, headers=_auth_header())
    assert resp.status_code == 302

    row = db_conn.execute("SELECT approval_requested_at FROM access_log WHERE id = ?", (log_id,)).fetchone()
    assert row["approval_requested_at"] is None

    import matching
    assert matching.find_domain(db_conn, "newsite.example") is None  # still not approved

    resp = client.get("/report", headers=_auth_header())
    assert b"Pending approval requests" not in resp.data


def test_dismiss_request_requires_admin_auth(client, db_conn):
    resp = client.post("/report/dismiss-request", data={"log_id": "1"})
    assert resp.status_code == 401


def test_approving_a_pending_request_also_clears_the_flag(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    log_id = _insert_recent_denial(db_conn, user_id)
    client.post("/blocked/request-approval", data={"log_id": log_id})

    client.post("/report/approve", data={"log_id": log_id}, headers=_auth_header())

    row = db_conn.execute("SELECT approval_requested_at FROM access_log WHERE id = ?", (log_id,)).fetchone()
    assert row["approval_requested_at"] is None


# ============================================================
# Report: scope=global ("approve for everyone") + date-range filter
# ============================================================

def test_approve_scope_global_makes_the_domain_global(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    client.post("/users/add", data={"username": "kid2", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    log_id = _insert_recent_denial(db_conn, user_id, domain="sharedsite.example")

    resp = client.post(
        "/report/approve", data={"log_id": log_id, "scope": "global"}, headers=_auth_header()
    )
    assert resp.status_code == 302

    import matching
    domain = matching.find_domain(db_conn, "sharedsite.example")
    assert domain is not None
    assert domain["is_global"] == 1
    # Global means nobody needs an explicit per-user assignment -- not even
    # the kid who originally triggered the request.
    assert db_conn.execute(
        "SELECT 1 FROM user_domains WHERE domain_id = ?", (domain["id"],)
    ).fetchone() is None


def test_approve_scope_global_flips_an_existing_non_global_domain(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    db_conn.execute(
        "INSERT INTO domains (pattern, mode, kind, is_global, created_at) "
        "VALUES ('existing\\.example', 'splice', 'generic', 0, datetime('now'))"
    )
    db_conn.commit()
    log_id = _insert_recent_denial(db_conn, user_id, domain="existing.example")

    client.post("/report/approve", data={"log_id": log_id, "scope": "global"}, headers=_auth_header())

    row = db_conn.execute("SELECT is_global FROM domains WHERE pattern = 'existing\\.example'").fetchone()
    assert row["is_global"] == 1


def test_approve_scope_global_for_a_show_grants_every_user(client, db_conn, monkeypatch):
    import dashboard
    monkeypatch.setattr(dashboard.cr_api, "series_title", lambda series_id, timeout=5.0: "Ace Attorney")

    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    client.post("/users/add", data={"username": "kid2", "password": "pw"}, headers=_auth_header())
    kid1_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    kid2_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid2'").fetchone()[0]
    db_conn.execute(
        "INSERT INTO access_log "
        "(ts, user_id, username, domain, path, series_id, series_name, allowed, reason) "
        "VALUES (datetime('now'), ?, 'kid1', 'www.crunchyroll.com', '/watch/x', 'GYE5K0XVR', NULL, 0, 'show_not_approved')",
        (kid1_id,),
    )
    db_conn.commit()
    log_id = db_conn.execute("SELECT id FROM access_log ORDER BY id DESC LIMIT 1").fetchone()[0]

    client.post("/report/approve", data={"log_id": log_id, "scope": "global"}, headers=_auth_header())

    import matching
    assert matching.user_has_show(db_conn, kid1_id, "GYE5K0XVR") is True
    assert matching.user_has_show(db_conn, kid2_id, "GYE5K0XVR") is True


def test_approve_default_scope_is_still_user_only(client, db_conn):
    """No scope field at all (the plain Recent-activity table's button
    doesn't send one) must behave exactly like the pre-existing per-user
    approve, not silently go global."""
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    log_id = _insert_recent_denial(db_conn, user_id, domain="onlykid1.example")

    client.post("/report/approve", data={"log_id": log_id}, headers=_auth_header())

    import matching
    domain = matching.find_domain(db_conn, "onlykid1.example")
    assert domain["is_global"] == 0
    assert matching.user_has_domain(db_conn, user_id, domain["id"]) is True


def test_report_days_filter_excludes_older_rows(client, db_conn):
    import db as db_mod
    old_ts = db_mod.iso_secs_ago(20 * 86400)  # 20 days ago
    db_conn.execute(
        "INSERT INTO access_log (ts, user_id, username, domain, path, allowed, reason) "
        "VALUES (?, NULL, 'kid1', 'stale.example', NULL, 1, 'global_domain')",
        (old_ts,),
    )
    db_conn.execute(
        "INSERT INTO access_log (ts, user_id, username, domain, path, allowed, reason) "
        "VALUES (?, NULL, 'kid1', 'fresh.example', NULL, 1, 'global_domain')",
        (db_mod.now_iso(),),
    )
    db_conn.commit()

    resp_default = client.get("/report", headers=_auth_header())  # default = 7 days
    assert b"fresh.example" in resp_default.data
    assert b"stale.example" not in resp_default.data

    resp_30 = client.get("/report?days=30", headers=_auth_header())
    assert b"fresh.example" in resp_30.data
    assert b"stale.example" in resp_30.data


def test_report_invalid_days_value_falls_back_to_default(client):
    resp = client.get("/report?days=not-a-number", headers=_auth_header())
    assert resp.status_code == 200
    resp = client.get("/report?days=999", headers=_auth_header())
    assert resp.status_code == 200


def test_dismiss_request_preserves_current_filter(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    log_id = _insert_recent_denial(db_conn, user_id)

    resp = client.post(
        "/report/dismiss-request",
        data={"log_id": log_id, "user": "kid1", "status": "blocked", "days": "30"},
        headers=_auth_header(),
    )
    location = resp.headers["Location"]
    assert "user=kid1" in location
    assert "status=blocked" in location
    assert "days=30" in location


# ============================================================
# Report: clickable Allowed/Blocked stats + Clear filters
# ============================================================

def _insert_logged(db_conn, domain, allowed):
    import db as db_mod
    db_conn.execute(
        "INSERT INTO access_log (ts, user_id, username, domain, path, allowed, reason) "
        "VALUES (?, NULL, 'kid1', ?, NULL, ?, 'global_domain')",
        (db_mod.now_iso(), domain, 1 if allowed else 0),
    )
    db_conn.commit()


def test_allowed_and_blocked_stats_link_to_status_filter(client, db_conn):
    resp = client.get("/report", headers=_auth_header())
    assert b'href="/report?user=&amp;status=allowed&amp;days=7"' in resp.data
    assert b'href="/report?user=&amp;status=blocked&amp;days=7"' in resp.data


def test_clicking_allowed_stat_filters_page_to_allowed_only(client, db_conn):
    _insert_logged(db_conn, "allowedsite.example", allowed=True)
    _insert_logged(db_conn, "blockedsite.example", allowed=False)

    resp = client.get("/report?status=allowed", headers=_auth_header())
    assert b"allowedsite.example" in resp.data
    assert b"blockedsite.example" not in resp.data
    # The Blocked stat link should still be present so the admin can pivot
    # straight to the other side without going through "Clear filters" first.
    assert b'href="/report?user=&amp;status=blocked&amp;days=7"' in resp.data


def test_clicking_blocked_stat_filters_page_to_blocked_only(client, db_conn):
    _insert_logged(db_conn, "allowedsite.example", allowed=True)
    _insert_logged(db_conn, "blockedsite.example", allowed=False)

    resp = client.get("/report?status=blocked", headers=_auth_header())
    assert b"blockedsite.example" in resp.data
    assert b"allowedsite.example" not in resp.data


def test_stat_link_preserves_current_kid_and_days_filter(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    resp = client.get("/report?user=kid1&days=30", headers=_auth_header())
    assert b'href="/report?user=kid1&amp;status=allowed&amp;days=30"' in resp.data


def test_clear_filters_button_hidden_on_default_view(client):
    resp = client.get("/report", headers=_auth_header())
    assert b"Clear filters" not in resp.data


def test_clear_filters_button_shown_when_a_filter_is_active(client):
    resp = client.get("/report?status=blocked", headers=_auth_header())
    assert b"Clear filters" in resp.data
    assert b'href="/report"' in resp.data

    resp = client.get("/report?days=30", headers=_auth_header())
    assert b"Clear filters" in resp.data


# ============================================================
# Devices (v2 roadmap groundwork -- not enforced anywhere yet)
# ============================================================

def test_normalize_mac_accepts_colon_and_hyphen_forms():
    import dashboard
    assert dashboard.normalize_mac("AA:BB:CC:DD:EE:FF") == "aa:bb:cc:dd:ee:ff"
    assert dashboard.normalize_mac("aa-bb-cc-dd-ee-ff") == "aa:bb:cc:dd:ee:ff"
    assert dashboard.normalize_mac("not a mac") is None
    assert dashboard.normalize_mac("aa:bb:cc:dd:ee") is None
    assert dashboard.normalize_mac("") is None


def test_add_device_then_appears_in_list(client, db_conn):
    resp = client.post(
        "/devices/add", data={"mac_address": "AA:BB:CC:DD:EE:01", "label": "Test Tablet"},
        headers=_auth_header(),
    )
    assert resp.status_code == 302
    row = db_conn.execute("SELECT * FROM devices WHERE mac_address = 'aa:bb:cc:dd:ee:01'").fetchone()
    assert row is not None
    assert row["label"] == "Test Tablet"
    assert row["bump_enabled"] == 0
    assert row["bypass_login"] == 0
    assert row["is_authenticated"] == 1

    resp = client.get("/devices", headers=_auth_header())
    assert b"aa:bb:cc:dd:ee:01" in resp.data
    assert b"Test Tablet" in resp.data


def test_add_device_invalid_mac_rejected(client, db_conn):
    resp = client.post("/devices/add", data={"mac_address": "not-a-mac"}, headers=_auth_header())
    assert "error=1" in resp.headers["Location"]
    assert db_conn.execute("SELECT * FROM devices").fetchone() is None


def test_add_device_duplicate_mac_rejected(client, db_conn):
    client.post("/devices/add", data={"mac_address": "AA:BB:CC:DD:EE:02"}, headers=_auth_header())
    resp = client.post("/devices/add", data={"mac_address": "aa:bb:cc:dd:ee:02"}, headers=_auth_header())
    assert "error=1" in resp.headers["Location"]
    count = db_conn.execute("SELECT COUNT(*) c FROM devices").fetchone()["c"]
    assert count == 1


def test_update_device_sets_flags_and_user(client, db_conn):
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    client.post("/devices/add", data={"mac_address": "AA:BB:CC:DD:EE:03"}, headers=_auth_header())
    device_id = db_conn.execute("SELECT id FROM devices").fetchone()[0]

    resp = client.post(
        "/devices/update",
        data={
            "device_id": device_id, "label": "Alex's Phone", "user_id": user_id,
            "bump_enabled": "on", "bypass_login": "",
        },
        headers=_auth_header(),
    )
    assert resp.status_code == 302

    row = db_conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    assert row["label"] == "Alex's Phone"
    assert row["user_id"] == user_id
    assert row["bump_enabled"] == 1
    assert row["bypass_login"] == 0


def test_delete_device_removes_row(client, db_conn):
    client.post("/devices/add", data={"mac_address": "AA:BB:CC:DD:EE:04"}, headers=_auth_header())
    device_id = db_conn.execute("SELECT id FROM devices").fetchone()[0]

    resp = client.post("/devices/delete", data={"device_id": device_id}, headers=_auth_header())
    assert resp.status_code == 302
    assert db_conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone() is None


def test_deleting_a_user_unassigns_their_devices_instead_of_deleting_them(client, db_conn):
    """devices.user_id is ON DELETE SET NULL, not CASCADE -- a device is a
    physical object that still exists after the person who used it is
    removed from the system, unlike e.g. a user's site/show approvals."""
    client.post("/users/add", data={"username": "kid1", "password": "pw"}, headers=_auth_header())
    user_id = db_conn.execute("SELECT id FROM users WHERE username = 'kid1'").fetchone()[0]
    client.post(
        "/devices/add", data={"mac_address": "AA:BB:CC:DD:EE:05", "user_id": user_id},
        headers=_auth_header(),
    )
    device_id = db_conn.execute("SELECT id FROM devices").fetchone()[0]
    assert db_conn.execute("SELECT user_id FROM devices WHERE id = ?", (device_id,)).fetchone()[0] == user_id

    client.post("/users/delete", data={"user_id": user_id}, headers=_auth_header())

    row = db_conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    assert row is not None
    assert row["user_id"] is None


def test_devices_requires_admin_auth(client):
    assert client.get("/devices").status_code == 401
    assert client.post("/devices/add", data={"mac_address": "aa:bb:cc:dd:ee:06"}).status_code == 401
