#!/usr/bin/env python3
"""Web dashboard for the parental proxy v2.

Everything configurable lives here: users (proxy logins), domains (with
mode splice/bump/trusted, global-vs-per-user access, and per-domain path
restrictions for bump-mode domains), each user's approved Crunchyroll
shows, the access report (with one-click approve on blocked entries), and
settings (LAN CIDR, admin credentials).

Auth: HTTP Basic against admin credentials stored in `settings` (bootstrapped
once from DASHBOARD_USER/DASHBOARD_PASSWORD on first run, editable from the
Settings page afterward). /ca-cert is deliberately unauthenticated -- it's a
public certificate, not a secret, and every client device needs to fetch it.
"""
from __future__ import annotations

import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, redirect, render_template_string, request, send_file, url_for

sys.path.insert(0, str(Path(__file__).parent))

import auth
import cr_api
import db
import matching

CA_CERT_PATH = Path(os.environ.get("PP_CA_CERT_PATH", "/config/ssl_cert/ca_cert.pem"))

app = Flask(__name__)


@app.before_request
def _reject_cross_origin_writes():
    """Lightweight CSRF guard. The dashboard authenticates with HTTP Basic,
    so a browser that has logged in once will attach credentials to a
    cross-site form POST automatically. Browsers always send an Origin
    header on such POSTs; reject any whose Origin (or, failing that,
    Referer) host isn't this dashboard. A request carrying neither header
    is a non-browser client (curl, a script) with no ambient credentials to
    abuse, so it's allowed through."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    for header in ("Origin", "Referer"):
        value = request.headers.get(header)
        if value:
            if urlparse(value).netloc != request.host:
                return Response("Cross-origin request blocked.", 403)
            return None
    return None


def get_db():
    conn = db.get_conn()
    db.init_db(conn)
    return conn


# ==========================================================
# ADMIN AUTH (DB-backed, bootstrapped from env on first run)
# ==========================================================

def bootstrap_admin() -> None:
    conn = get_db()
    try:
        env_user = os.environ.get("DASHBOARD_USER")
        env_pass = os.environ.get("DASHBOARD_PASSWORD")
        if db.get_setting(conn, "admin_username") is None:
            db.set_setting(conn, "admin_username", env_user or "admin")
        if db.get_setting(conn, "admin_password_hash") is None:
            if env_pass:
                db.set_setting(conn, "admin_password_hash", auth.hash_password(env_pass))
            else:
                generated = secrets.token_urlsafe(12)
                db.set_setting(conn, "admin_password_hash", auth.hash_password(generated))
                username = db.get_setting(conn, "admin_username")
                print(
                    "\n" + "=" * 64
                    + "\n  No DASHBOARD_PASSWORD was set. Generated an admin login:\n"
                    + f"    username: {username}\n"
                    + f"    password: {generated}\n"
                    + "  Change it from the Settings page after logging in.\n"
                    + "=" * 64 + "\n",
                    file=sys.stderr, flush=True,
                )
        db.set_setting_if_absent(conn, "local_network", os.environ.get("LOCAL_NETWORK", "192.168.1.0/24"))
        db.set_setting_if_absent(conn, "secret_key", secrets.token_hex(32))
        conn.commit()
    finally:
        conn.close()


def _check_admin_auth(basic_auth) -> bool:
    if basic_auth is None:
        return False
    conn = get_db()
    try:
        expected_user = db.get_setting(conn, "admin_username")
        expected_hash = db.get_setting(conn, "admin_password_hash")
    finally:
        conn.close()
    if not expected_user or not expected_hash:
        return False
    return basic_auth.username == expected_user and auth.verify_password(basic_auth.password, expected_hash)


def require_admin(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _check_admin_auth(request.authorization):
            return Response(
                "Authentication required", 401,
                {"WWW-Authenticate": 'Basic realm="Parental Proxy Admin"'},
            )
        return view(*args, **kwargs)
    return wrapped


@app.route("/logout")
@require_admin
def logout():
    """HTTP Basic Auth has no real server-side session to revoke -- the
    browser just caches the credential per-origin until it's closed. The
    Logout link in BASE navigates here with a deliberately wrong credential
    embedded in the URL (http://logout:logout@host/logout); that overwrites
    whatever the browser had cached for this origin, @require_admin above
    then 401s it (same as any other bad credential), and browsers respond
    to a 401 + WWW-Authenticate on a top-level navigation by showing a
    fresh native sign-in prompt. This body only runs in the practically
    impossible case that the real admin credentials happen to literally be
    "logout"/"logout"."""
    return Response(
        "Logged out.", 401, {"WWW-Authenticate": 'Basic realm="Parental Proxy Admin"'}
    )


# ==========================================================
# SHARED PAGE CHROME
# ==========================================================

BASE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Parental Proxy</title>
<meta name="theme-color" content="#2f6fed">
<link rel="manifest" href="{{ url_for('static', filename='manifest.webmanifest') }}">
<link rel="icon" href="{{ url_for('static', filename='icons/favicon.ico') }}">
<link rel="apple-touch-icon" href="{{ url_for('static', filename='icons/apple-touch-icon.png') }}">
<link rel="stylesheet" href="{{ url_for('static', filename='css/app.css') }}">
</head>
<body>
<header class="topbar">
  <div class="page">
    <a class="brand" href="{{ url_for('report') }}">
      <img src="{{ url_for('static', filename='icons/icon-192.png') }}" alt="">
      Parental Proxy
    </a>
    <nav class="tabs">
      <a href="{{ url_for('report') }}" class="{{ 'active' if active=='report' else '' }}">
        Report{% if pending_count %} <span class="badge pending">{{ pending_count }}</span>{% endif %}
      </a>
      <a href="{{ url_for('users') }}" class="{{ 'active' if active=='users' else '' }}">Users</a>
      <a href="{{ url_for('domains') }}" class="{{ 'active' if active=='domains' else '' }}">Domains</a>
      <a href="{{ url_for('devices') }}" class="{{ 'active' if active=='devices' else '' }}">Devices</a>
    </nav>
    <div class="topbar-actions">
      <a class="icon-btn {{ 'active' if active=='settings' else '' }}" href="{{ url_for('settings_page') }}" title="Settings" aria-label="Settings">&#9881;</a>
      <a class="logout-link" href="http://logout:logout@{{ request.host }}{{ url_for('logout') }}" title="Log out">Logout</a>
    </div>
  </div>
</header>
<div class="page">
{% if message %}<div class="flash {{ 'error' if error else 'ok' }}">{{ message }}</div>{% endif %}
{{ body|safe }}
</div>
<script>
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
}
</script>
</body>
</html>
"""


def render(active: str, body: str) -> str:
    conn = get_db()
    pending_count = conn.execute(
        "SELECT COUNT(*) c FROM access_log WHERE approval_requested_at IS NOT NULL"
    ).fetchone()["c"]
    return render_template_string(
        BASE, active=active, body=body, pending_count=pending_count,
        message=request.args.get("message"), error=request.args.get("error"),
    )


def flash_redirect(endpoint: str, message: str, error: bool = False, **kwargs):
    return redirect(url_for(endpoint, message=message, error="1" if error else None, **kwargs))


# ==========================================================
# CA CERT (public, unauthenticated)
# ==========================================================

@app.route("/sw.js")
def service_worker():
    # Served from root (not /static/sw.js) so its default scope is the whole
    # app -- a service worker can only control paths at or below where it's
    # served from, and it needs to control every dashboard page, not just
    # /static/ assets.
    resp = send_file(Path(app.static_folder) / "sw.js", mimetype="application/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/ca-cert")
def ca_cert():
    if not CA_CERT_PATH.exists():
        return Response(
            "The CA certificate hasn't been generated yet. Start the proxy "
            "container first, then reload this page.", 404,
        )
    return send_file(
        CA_CERT_PATH, mimetype="application/x-x509-ca-cert",
        as_attachment=True, download_name="parental-proxy-ca.crt",
    )


BLOCKED_BODY = """
<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Blocked</title>
<style>
:root{color-scheme:light dark;}
body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;max-width:28rem;margin:4rem auto;
padding:0 1.25rem;text-align:center;color:#1e293b;}
@media (prefers-color-scheme:dark){body{color:#e5eaf3;}}
.icon{width:56px;height:56px;border-radius:16px;background:#2f6fed;display:inline-flex;
align-items:center;justify-content:center;margin-bottom:1rem;}
h1{font-size:1.15rem;margin:0 0 .5rem;}
p{font-size:.92rem;opacity:.75;}
button{margin-top:1.25rem;padding:.6rem 1.4rem;border-radius:8px;border:none;background:#2f6fed;
color:#fff;font-size:.92rem;font-weight:600;cursor:pointer;font-family:inherit;}
.sent{margin-top:1.25rem;font-size:.88rem;color:#15803d;font-weight:600;}
</style></head><body>
<div class='icon'>
<svg width='28' height='28' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='2.2'
stroke-linecap='round' stroke-linejoin='round'><path d='M12 3l8 3.5v5.2c0 4.7-3.2 8.6-8 9.8
-4.8-1.2-8-5.1-8-9.8V6.5L12 3z'/></svg>
</div>
<h1>This site or show isn't approved.</h1>
<p>Ask a parent to check the dashboard if you think this should be allowed.</p>
{% if row and row.approval_requested_at %}
<p class="sent">Request sent -- a parent will see this on the dashboard.</p>
{% elif row %}
<form method="post" action="{{ url_for('request_approval') }}">
  <input type="hidden" name="log_id" value="{{ row.id }}">
  <button type="submit">Request approval</button>
</form>
{% endif %}
</body></html>
"""

# How long after a denial the /blocked page will still offer to attach a
# "Request approval" click to it. Generous enough for a slow device/redirect,
# short enough that two different people getting blocked seconds apart on a
# small home network essentially never collide.
BLOCKED_REQUEST_LOOKBACK_SECONDS = 30


@app.route("/blocked")
def blocked():
    # Reached two ways: a bump-mode denial (authz_helper.py, always shows a
    # page) or a splice-mode denial when block_page_mode='redirect'. Either
    # way, authz_helper.py/sni_helper.py just wrote the matching access_log
    # row a moment before this page loaded -- correlating by recency instead
    # of by request identity avoids depending on exactly how Squid's
    # deny_info would need to be configured to pass the original URL through
    # the redirect (version-specific behavior not verified against a live
    # Squid instance; see commit notes). Only rows with a user_id are
    # eligible, matching approve_from_report()'s own requirement.
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM access_log WHERE allowed = 0 AND user_id IS NOT NULL AND ts >= ? "
        "ORDER BY id DESC LIMIT 1",
        (db.iso_secs_ago(BLOCKED_REQUEST_LOOKBACK_SECONDS),),
    ).fetchone()
    return render_template_string(BLOCKED_BODY, row=row), 403


@app.route("/blocked/request-approval", methods=["POST"])
def request_approval():
    # Deliberately unauthenticated, same as /blocked itself -- whoever got
    # blocked is the one clicking this. Worst case of abuse is dashboard
    # noise (an extra pending-request row an admin has to dismiss), not any
    # data exposure or access grant: it only sets a timestamp on a row
    # that's already denied and already visible on the Report page.
    log_id = request.form.get("log_id", "")
    conn = get_db()
    row = conn.execute(
        "SELECT id FROM access_log WHERE id = ? AND allowed = 0 AND approval_requested_at IS NULL",
        (log_id,),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE access_log SET approval_requested_at = ? WHERE id = ?", (db.now_iso(), log_id)
        )
        conn.commit()
    return redirect(url_for("blocked"))


@app.route("/")
def index():
    return redirect(url_for("report"))


# ==========================================================
# USERS
# ==========================================================

USERS_BODY = """
<div class="cert-banner">
  <strong>Setting up a new device or user?</strong> Each person needs their
  own login (below) configured in the device's proxy settings, and the
  device needs the CA certificate trusted.
  <br><a class="btn add" href="{{ url_for('ca_cert') }}">Download CA certificate</a>
</div>

<div class="card">
<h2>Users ({{ users|length }})</h2>
<div class="table-scroll">
<table>
  <tr><th>Username</th><th>Display name</th><th>Sites</th><th>Shows</th><th></th></tr>
  {% for u in users %}
  <tr>
    <td><code>{{ u.username }}</code></td>
    <td>{{ u.display_name }}</td>
    <td><a href="{{ url_for('domains', user_id=u.id) }}">{{ u.domain_count }} assigned</a></td>
    <td>{{ u.show_count }} approved</td>
    <td>
      <a class="btn small" href="{{ url_for('user_detail', user_id=u.id) }}">Manage</a>
      <form class="inline" method="post" action="{{ url_for('delete_user') }}">
        <input type="hidden" name="user_id" value="{{ u.id }}">
        <button class="danger small" type="submit" onclick="return confirm('Delete {{ u.username }}? This removes their login, site access, and show approvals.')">Delete</button>
      </form>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="5"><em>No users yet.</em></td></tr>
  {% endfor %}
</table>
</div>
<form class="add-form" method="post" action="{{ url_for('add_user') }}">
  <input type="text" name="username" placeholder="username, e.g. kid1" required>
  <input type="text" name="display_name" placeholder="Display name, e.g. Alex">
  <input type="password" name="password" placeholder="Password" required>
  <button class="add" type="submit">Add user</button>
</form>
<p class="hint">This username/password is what gets configured in that person's device proxy settings (not the dashboard login).</p>
</div>
"""


@app.route("/users")
@require_admin
def users():
    conn = get_db()
    rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    out = []
    for u in rows:
        # Matches what the "N assigned" link's ?user_id= filter on /domains
        # actually shows: explicit assignments plus every global domain.
        domain_count = conn.execute(
            "SELECT COUNT(*) c FROM domains d "
            "LEFT JOIN user_domains ud ON ud.domain_id = d.id AND ud.user_id = ? "
            "WHERE d.is_global = 1 OR ud.user_id IS NOT NULL",
            (u["id"],),
        ).fetchone()["c"]
        show_count = conn.execute(
            "SELECT COUNT(*) c FROM user_shows WHERE user_id = ?", (u["id"],)
        ).fetchone()["c"]
        out.append({**dict(u), "domain_count": domain_count, "show_count": show_count})
    return render("users", render_template_string(USERS_BODY, users=out))


@app.route("/users/add", methods=["POST"])
@require_admin
def add_user():
    username = request.form.get("username", "").strip()
    display_name = request.form.get("display_name", "").strip() or username
    password = request.form.get("password", "")
    if not username or not password:
        return flash_redirect("users", "Username and password are required.", error=True)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        return flash_redirect("users", "Username can only contain letters, numbers, _ . -", error=True)
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, created_at) VALUES (?,?,?,?)",
            (username, display_name, auth.hash_password(password), db.now_iso()),
        )
        conn.commit()
    except Exception as exc:
        if "UNIQUE" in str(exc):
            return flash_redirect("users", f"Username {username!r} already exists.", error=True)
        raise
    return flash_redirect("users", f"Added user {username}.")


@app.route("/users/delete", methods=["POST"])
@require_admin
def delete_user():
    user_id = request.form.get("user_id", "")
    conn = get_db()
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    return flash_redirect("users", "User deleted.")


@app.route("/users/reset-password", methods=["POST"])
@require_admin
def reset_password():
    user_id = request.form.get("user_id", "")
    password = request.form.get("password", "")
    if not password:
        return flash_redirect("user_detail", "Password is required.", error=True, user_id=user_id)
    conn = get_db()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (auth.hash_password(password), user_id),
    )
    conn.commit()
    return flash_redirect("user_detail", "Password updated.", user_id=user_id)


USER_DETAIL_BODY = """
<p><a href="{{ url_for('users') }}">&larr; All users</a></p>
<h1>{{ u.display_name }} <code>({{ u.username }})</code></h1>

<div class="card">
<h2>Assigned sites</h2>
<div class="table-scroll">
<table>
  <tr><th>Domain</th><th>Mode</th></tr>
  {% for d in assigned_domains %}
  <tr><td><code>{{ d.pattern }}</code></td><td><span class="badge mode-{{ d.mode }}">{{ d.mode }}</span></td></tr>
  {% else %}
  <tr><td colspan="2"><em>No per-user sites assigned (still gets global sites).</em></td></tr>
  {% endfor %}
</table>
</div>
<p class="hint">Manage assignment from the <a href="{{ url_for('domains') }}">Domains</a> page -- pick the site there and check this user.</p>
</div>

<div class="card">
<h2>Approved Crunchyroll shows ({{ shows|length }})</h2>
<div class="table-scroll">
<table>
  <tr><th>Series ID</th><th>Name</th><th></th></tr>
  {% for s in shows %}
  <tr>
    <td><code>{{ s.series_id }}</code></td>
    <td>{{ s.series_name }}</td>
    <td>
      <form class="inline" method="post" action="{{ url_for('remove_show') }}">
        <input type="hidden" name="user_id" value="{{ u.id }}">
        <input type="hidden" name="series_id" value="{{ s.series_id }}">
        <button class="danger small" type="submit" onclick="return confirm('Remove {{ s.series_name }}?')">Remove</button>
      </form>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="3"><em>No shows approved yet.</em></td></tr>
  {% endfor %}
</table>
</div>
<form class="add-form" method="post" action="{{ url_for('add_show') }}">
  <input type="hidden" name="user_id" value="{{ u.id }}">
  <input type="url" name="url" placeholder="https://www.crunchyroll.com/series/GYE5K0XVR/ace-attorney" required style="flex:1; min-width:280px;">
  <input type="text" name="name" placeholder="Name (auto-filled, editable)">
  <button class="add" type="submit">Approve show</button>
</form>
</div>

<div class="card">
<h2>Change password</h2>
<form class="add-form" method="post" action="{{ url_for('reset_password') }}">
  <input type="hidden" name="user_id" value="{{ u.id }}">
  <input type="password" name="password" placeholder="New password" required>
  <button class="add" type="submit">Update password</button>
</form>
</div>
"""


@app.route("/users/<int:user_id>")
@require_admin
def user_detail(user_id: int):
    conn = get_db()
    u = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if u is None:
        return flash_redirect("users", "That user no longer exists.", error=True)
    assigned_domains = conn.execute(
        "SELECT d.pattern, d.mode FROM domains d "
        "JOIN user_domains ud ON ud.domain_id = d.id "
        "WHERE ud.user_id = ? ORDER BY d.pattern", (user_id,),
    ).fetchall()
    shows = conn.execute(
        "SELECT series_id, series_name FROM user_shows WHERE user_id = ? ORDER BY series_name",
        (user_id,),
    ).fetchall()
    body = render_template_string(USER_DETAIL_BODY, u=u, assigned_domains=assigned_domains, shows=shows)
    return render("users", body)


@app.route("/shows/add", methods=["POST"])
@require_admin
def add_show():
    user_id = request.form.get("user_id", "")
    url = request.form.get("url", "")
    override_name = request.form.get("name", "").strip()
    series_id, suggested_name = parse_series_url(url)
    if series_id is None:
        return flash_redirect("user_detail", suggested_name, error=True, user_id=user_id)
    name = override_name or cr_api.series_title(series_id) or suggested_name
    conn = get_db()
    conn.execute(
        "INSERT INTO user_shows (user_id, series_id, series_name) VALUES (?,?,?) "
        "ON CONFLICT(user_id, series_id) DO UPDATE SET series_name = excluded.series_name",
        (user_id, series_id, name),
    )
    conn.commit()
    return flash_redirect("user_detail", f"Approved {name}.", user_id=user_id)


@app.route("/shows/remove", methods=["POST"])
@require_admin
def remove_show():
    user_id = request.form.get("user_id", "")
    series_id = request.form.get("series_id", "")
    conn = get_db()
    conn.execute(
        "DELETE FROM user_shows WHERE user_id = ? AND series_id = ?", (user_id, series_id)
    )
    conn.commit()
    return flash_redirect("user_detail", "Removed.", user_id=user_id)


SERIES_URL_RE = re.compile(
    r"^https?://www\.crunchyroll\.com/series/([A-Za-z0-9]+)(?:/([^/?#]+))?",
    re.IGNORECASE,
)


def parse_series_url(url: str) -> tuple[str | None, str]:
    """Returns (series_id, name) on success, or (None, error message)."""
    url = (url or "").strip()
    match = SERIES_URL_RE.match(url)
    if not match:
        return None, (
            "That doesn't look like a Crunchyroll series URL. Expected "
            "something like https://www.crunchyroll.com/series/GYE5K0XVR/ace-attorney"
        )
    series_id = match.group(1).upper()
    slug = match.group(2) or ""
    name = " ".join(w.capitalize() for w in slug.split("-") if w) or series_id
    return series_id, name


# ==========================================================
# DOMAINS
# ==========================================================

def path_to_pattern(path: str) -> str:
    """Derive a starting-point regex pattern from a real, literal request
    path -- query string stripped, then anchored to the start and
    regex-escaped so path characters that happen to be regex metacharacters
    (a literal `.` is the common one) are matched literally rather than as
    "any character". No trailing anchor: matches the given path and
    anything after it (e.g. GH #6's motivating case -- approving one
    comic's URL should keep matching future chapters under the same path),
    same convention as the seeded CRUNCHYROLL_PATHS prefixes. This is a
    starting point for the admin to review/edit, not a final answer --
    callers must still let it go through the normal add_path validation.
    """
    return "^" + re.escape((path or "/").split("?", 1)[0])


# Shared by the add-domain form and the domain Manage page's Access card --
# one Everyone checkbox plus three independent multi-selects (Users,
# Groups, Devices), so "single user, multiple users, a device, a group, or
# any combination" is just whatever's selected across these three lists at
# once. Needs all_users/all_groups/all_devices and preselected_user_ids/
# preselected_group_ids/preselected_device_ids/is_global_checked in scope
# wherever it's used.
ACCESS_SELECTS = """
  <label><input type="checkbox" name="is_global" {{ 'checked' if is_global_checked }}> Everyone</label>
  <div class="access-grid">
    <div>
      <div class="access-label">Users</div>
      <select name="user_ids" multiple size="5">
        {% for u in all_users %}
        <option value="{{ u.id }}" {{ 'selected' if u.id in preselected_user_ids }}>{{ u.display_name }}</option>
        {% endfor %}
      </select>
    </div>
    <div>
      <div class="access-label">Groups</div>
      <select name="group_ids" multiple size="5">
        {% for g in all_groups %}
        <option value="{{ g.id }}" {{ 'selected' if g.id in preselected_group_ids }}>{{ g.name }}</option>
        {% endfor %}
      </select>
    </div>
    <div>
      <div class="access-label">Devices</div>
      <select name="device_ids" multiple size="5">
        {% for dev in all_devices %}
        <option value="{{ dev.id }}" {{ 'selected' if dev.id in preselected_device_ids }}>{{ dev.label or dev.mac_address }}</option>
        {% endfor %}
      </select>
    </div>
  </div>
"""


DOMAINS_BODY = """
<div class="card">
<h2>Domains ({{ domains|length }})</h2>
{% if filtered_user %}
<p class="hint">
  Showing domains assigned to <strong>{{ filtered_user.display_name }}</strong>
  (plus everyone's global domains) --
  <a href="{{ url_for('domains') }}">clear filter</a>
</p>
{% elif filtered_group %}
<p class="hint">
  Showing domains assigned to the <strong>{{ filtered_group.name }}</strong> group
  (plus everyone's global domains) --
  <a href="{{ url_for('domains') }}">clear filter</a>
</p>
{% elif filtered_device %}
<p class="hint">
  Showing domains assigned to <strong>{{ filtered_device.label or filtered_device.mac_address }}</strong>
  (plus everyone's global domains) --
  <a href="{{ url_for('domains') }}">clear filter</a>
</p>
{% endif %}
<p class="hint">
  <span class="badge mode-splice">splice</span> host-only, never decrypted &nbsp;
  <span class="badge mode-bump">bump</span> fully decrypted, path/show rules apply &nbsp;
  <span class="badge mode-trusted">trusted</span> always passed through, unchecked
</p>
<div class="table-scroll">
<table>
  <tr><th>Pattern</th><th>Mode</th><th>Access</th><th>Note</th><th></th></tr>
  {% for d in domains %}
  <tr>
    <td><code>{{ d.pattern }}</code></td>
    <td><span class="badge mode-{{ d.mode }}">{{ d.mode }}</span></td>
    <td>{{ 'Everyone' if d.is_global else 'Per-user/group/device' }}</td>
    <td>{{ d.note or '' }}</td>
    <td>
      <a class="btn small" href="{{ url_for('domain_detail', domain_id=d.id) }}">Manage</a>
      <form class="inline" method="post" action="{{ url_for('delete_domain') }}">
        <input type="hidden" name="domain_id" value="{{ d.id }}">
        {% if filtered_user %}<input type="hidden" name="user_id" value="{{ filtered_user.id }}">{% endif %}
        {% if filtered_group %}<input type="hidden" name="group_id" value="{{ filtered_group.id }}">{% endif %}
        {% if filtered_device %}<input type="hidden" name="device_id" value="{{ filtered_device.id }}">{% endif %}
        <button class="danger small" type="submit" onclick="return confirm('Delete this domain rule?')">Delete</button>
      </form>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="5"><em>No domains configured.</em></td></tr>
  {% endfor %}
</table>
</div>

<form class="add-form" method="post" action="{{ url_for('add_domain') }}">
  {% if filtered_user %}<input type="hidden" name="user_id" value="{{ filtered_user.id }}">{% endif %}
  {% if filtered_group %}<input type="hidden" name="group_id" value="{{ filtered_group.id }}">{% endif %}
  {% if filtered_device %}<input type="hidden" name="device_id" value="{{ filtered_device.id }}">{% endif %}
  <input type="text" name="pattern" placeholder="e.g. example\\.com" required>
  <select name="mode">
    <option value="splice">splice (host-only)</option>
    <option value="bump">bump (decrypt, path rules)</option>
    <option value="trusted">trusted (always pass, unchecked)</option>
  </select>
  <input type="text" name="note" placeholder="Note (optional)">
  <button class="add" type="submit">Add domain</button>
""" + ACCESS_SELECTS + """
</form>
<p class="hint">
  "Everyone" is for shared infrastructure (fonts, auth providers, CDNs). Otherwise pick any
  combination of specific users, groups, and/or devices -- hold Ctrl/Cmd (or Shift for a range)
  to select more than one in a list. You can adjust all of this later from the domain's Manage page.
</p>
</div>

{% if filtered_user %}
<div class="card">
<h2>Approve a specific page for {{ filtered_user.display_name }}</h2>
<p class="hint">Paste a full URL to approve just that page (and anything after it), without opening the rest of the site. Creates the domain in bump mode if it doesn't already exist, adds a path rule derived from the URL, and assigns both to {{ filtered_user.display_name }}.</p>
<form class="add-form" method="post" action="{{ url_for('add_domain_from_url') }}">
  <input type="hidden" name="user_id" value="{{ filtered_user.id }}">
  <input type="url" name="url" placeholder="https://example.com/some/specific/page" required style="flex:1; min-width:320px;">
  <button class="add" type="submit">Approve this page</button>
</form>
</div>
{% endif %}
"""


def _get_filtered_target(conn, args_or_form):
    """Resolves the ?user_id= / ?group_id= / ?device_id= filter (Domains
    page) or the equivalent hidden form fields (add/delete actions taken
    from a filtered view) to at most one of (filtered_user, filtered_group,
    filtered_device, error_message). user_id wins if more than one is
    somehow present."""
    user_id = args_or_form.get("user_id", "")
    group_id = args_or_form.get("group_id", "")
    device_id = args_or_form.get("device_id", "")
    if user_id:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return (row, None, None, None) if row else (None, None, None, "That user no longer exists.")
    if group_id:
        row = conn.execute("SELECT * FROM groups WHERE id = ?", (group_id,)).fetchone()
        return (None, row, None, None) if row else (None, None, None, "That group no longer exists.")
    if device_id:
        row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
        return (None, None, row, None) if row else (None, None, None, "That device no longer exists.")
    return None, None, None, None


@app.route("/domains")
@require_admin
def domains():
    conn = get_db()
    filtered_user, filtered_group, filtered_device, error = _get_filtered_target(conn, request.args)
    if error:
        return flash_redirect("domains", error, error=True)

    if filtered_user or filtered_group or filtered_device:
        # Same rule the proxy itself uses at request time (matching.py),
        # reused here rather than reimplemented as a second copy of the
        # "is this domain visible to this user/group/device" logic.
        all_rows = conn.execute("SELECT * FROM domains ORDER BY is_global DESC, pattern").fetchall()
        if filtered_user:
            rows = [d for d in all_rows if bool(d["is_global"]) or matching.user_has_domain(conn, filtered_user["id"], d["id"])]
        elif filtered_group:
            rows = [d for d in all_rows if bool(d["is_global"]) or matching.group_has_domain(conn, filtered_group["id"], d["id"])]
        else:
            rows = [d for d in all_rows if bool(d["is_global"]) or matching.device_has_domain(conn, filtered_device["id"], d["id"])]
    else:
        rows = conn.execute("SELECT * FROM domains ORDER BY is_global DESC, pattern").fetchall()

    all_users = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    all_groups = conn.execute("SELECT * FROM groups ORDER BY name").fetchall()
    all_devices = conn.execute("SELECT * FROM devices ORDER BY COALESCE(label, mac_address)").fetchall()
    return render(
        "domains",
        render_template_string(
            DOMAINS_BODY, domains=rows, filtered_user=filtered_user, filtered_group=filtered_group,
            filtered_device=filtered_device, all_users=all_users, all_groups=all_groups, all_devices=all_devices,
            is_global_checked=False,
            preselected_user_ids={filtered_user["id"]} if filtered_user else set(),
            preselected_group_ids={filtered_group["id"]} if filtered_group else set(),
            preselected_device_ids={filtered_device["id"]} if filtered_device else set(),
        ),
    )


@app.route("/domains/add", methods=["POST"])
@require_admin
def add_domain():
    pattern = request.form.get("pattern", "").strip()
    mode = request.form.get("mode", "splice")
    is_global = 1 if request.form.get("is_global") else 0
    note = request.form.get("note", "").strip() or None
    conn = get_db()

    # Preserves the Users/Domains/Devices-page filter (?user_id=/
    # ?group_id=/?device_id=) across this POST, so adding a domain from a
    # filtered view doesn't silently drop the admin back into the
    # unfiltered list -- and (see below) also assigns the new domain to
    # that filter's subject, not just redirects back to it.
    _, _, _, error = _get_filtered_target(conn, request.form)
    redirect_kwargs = {}
    if request.form.get("user_id"):
        redirect_kwargs["user_id"] = request.form["user_id"]
    elif request.form.get("group_id"):
        redirect_kwargs["group_id"] = request.form["group_id"]
    elif request.form.get("device_id"):
        redirect_kwargs["device_id"] = request.form["device_id"]

    if not pattern:
        return flash_redirect("domains", "Pattern is required.", error=True, **redirect_kwargs)
    if mode not in ("splice", "bump", "trusted"):
        return flash_redirect("domains", "Invalid mode.", error=True, **redirect_kwargs)
    if len(pattern) > 200:
        return flash_redirect("domains", "Pattern too long (200 characters max).", error=True, **redirect_kwargs)
    try:
        re.compile(pattern)
    except re.error as exc:
        return flash_redirect("domains", f"Not a valid regex: {exc}", error=True, **redirect_kwargs)

    # Explicit multi-select assignment (the add-domain form's own Users/
    # Groups/Devices lists) plus the implicit single target from a
    # filtered view -- both can contribute, so a domain added from a
    # filtered view is assigned to that view's subject even if the admin
    # didn't also touch the lists.
    user_ids = {int(x) for x in request.form.getlist("user_ids") if x.isdigit()}
    group_ids = {int(x) for x in request.form.getlist("group_ids") if x.isdigit()}
    device_ids = {int(x) for x in request.form.getlist("device_ids") if x.isdigit()}
    if request.form.get("user_id", "").isdigit():
        user_ids.add(int(request.form["user_id"]))
    if request.form.get("group_id", "").isdigit():
        group_ids.add(int(request.form["group_id"]))
    if request.form.get("device_id", "").isdigit():
        device_ids.add(int(request.form["device_id"]))

    try:
        conn.execute(
            "INSERT INTO domains (pattern, mode, kind, is_global, note, created_at) VALUES (?,?,?,?,?,?)",
            (pattern, mode, "generic", is_global, note, db.now_iso()),
        )
        conn.commit()
    except Exception as exc:
        if "UNIQUE" in str(exc):
            return flash_redirect("domains", f"{pattern!r} is already configured.", error=True, **redirect_kwargs)
        raise

    domain_id = conn.execute("SELECT id FROM domains WHERE pattern = ?", (pattern,)).fetchone()["id"]
    for uid in user_ids:
        conn.execute("INSERT OR IGNORE INTO user_domains (user_id, domain_id) VALUES (?,?)", (uid, domain_id))
    for gid in group_ids:
        conn.execute("INSERT OR IGNORE INTO group_domains (group_id, domain_id) VALUES (?,?)", (gid, domain_id))
    for did in device_ids:
        conn.execute("INSERT OR IGNORE INTO device_domains (device_id, domain_id) VALUES (?,?)", (did, domain_id))
    conn.commit()
    return flash_redirect("domains", f"Added {pattern}.", **redirect_kwargs)


@app.route("/domains/add-url", methods=["POST"])
@require_admin
def add_domain_from_url():
    """GH #6: approve one specific page without the three separate steps
    (add domain, flip to bump, add a path pattern from a different page).
    Only usable from a user's filtered Domains view (?user_id=), since
    approving a page always means approving it *for someone* -- there's no
    "everyone gets this one page" equivalent the way whole-domain
    assignment has "Everyone gets this"."""
    url = request.form.get("url", "").strip()
    user_id = request.form.get("user_id", "")
    redirect_kwargs = {"user_id": user_id} if user_id else {}
    if not user_id:
        return flash_redirect(
            "domains",
            "This shortcut approves a page for a specific person -- use it from that "
            "person's filtered Domains view (via the Users page's \"N assigned\" link).",
            error=True,
        )
    conn = get_db()
    user = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        return flash_redirect("domains", "That user no longer exists.", error=True)

    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    hostname = urlparse(url).hostname
    # urlparse is lenient about what it calls a "hostname" -- it happily
    # returns garbage input as netloc/hostname without validating actual
    # hostname syntax, so a real format check is needed here too.
    if not hostname or not re.match(
        r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?)*$", hostname
    ):
        return flash_redirect("domains", "That doesn't look like a valid URL.", error=True, **redirect_kwargs)
    path = urlparse(url).path or "/"

    domain = matching.find_domain(conn, hostname)
    if domain is None:
        domain_pattern = re.escape(hostname)
        conn.execute(
            "INSERT INTO domains (pattern, mode, kind, is_global, note, created_at) "
            "VALUES (?, 'bump', 'generic', 0, 'Added via the paste-a-URL shortcut', ?)",
            (domain_pattern, db.now_iso()),
        )
        domain = conn.execute("SELECT * FROM domains WHERE pattern = ?", (domain_pattern,)).fetchone()
    elif domain["mode"] != "bump":
        return flash_redirect(
            "domains",
            f"{hostname} is already configured, but in {domain['mode']} mode -- a page-level "
            f"rule needs bump mode. Switch it from Manage first, then try again.",
            error=True, **redirect_kwargs,
        )

    conn.execute(
        "INSERT OR IGNORE INTO domain_paths (domain_id, pattern) VALUES (?, ?)",
        (domain["id"], path_to_pattern(path)),
    )
    conn.execute(
        "INSERT OR IGNORE INTO user_domains (user_id, domain_id) VALUES (?, ?)",
        (user_id, domain["id"]),
    )
    conn.commit()
    return flash_redirect("domains", f"Approved {hostname}{path} for this user.", **redirect_kwargs)


@app.route("/domains/delete", methods=["POST"])
@require_admin
def delete_domain():
    domain_id = request.form.get("domain_id", "")
    redirect_kwargs = {}
    if request.form.get("user_id"):
        redirect_kwargs["user_id"] = request.form["user_id"]
    elif request.form.get("group_id"):
        redirect_kwargs["group_id"] = request.form["group_id"]
    elif request.form.get("device_id"):
        redirect_kwargs["device_id"] = request.form["device_id"]
    conn = get_db()
    row = conn.execute("SELECT kind FROM domains WHERE id = ?", (domain_id,)).fetchone()
    if row and row["kind"] == "crunchyroll":
        return flash_redirect(
            "domains",
            "The Crunchyroll domain is built into the show-approval feature and can't be deleted "
            "(edit its mode/paths from Manage instead).",
            error=True, **redirect_kwargs,
        )
    conn.execute("DELETE FROM domains WHERE id = ?", (domain_id,))
    conn.commit()
    return flash_redirect("domains", "Domain removed.", **redirect_kwargs)


DOMAIN_DETAIL_BODY = """
<p><a href="{{ url_for('domains') }}">&larr; All domains</a></p>
<h1><code>{{ d.pattern }}</code> <span class="badge mode-{{ d.mode }}">{{ d.mode }}</span></h1>
{% if d.kind == 'crunchyroll' %}
<p class="hint">This is the built-in Crunchyroll domain. Shows are approved per-user from each user's page; the paths below are a defense-in-depth safety net, not the main show filter.</p>
{% endif %}

<div class="card">
<h2>Mode</h2>
<form class="add-form" method="post" action="{{ url_for('update_domain') }}">
  <input type="hidden" name="domain_id" value="{{ d.id }}">
  <select name="mode">
    <option value="splice" {{ 'selected' if d.mode=='splice' }}>splice (host-only)</option>
    <option value="bump" {{ 'selected' if d.mode=='bump' }}>bump (decrypt, path rules)</option>
    <option value="trusted" {{ 'selected' if d.mode=='trusted' }}>trusted (always pass, unchecked)</option>
  </select>
  <input type="text" name="note" value="{{ d.note or '' }}" placeholder="Note">
  <button class="add" type="submit">Save</button>
</form>
</div>

<div class="card">
<h2>Access</h2>
<form method="post" action="{{ url_for('update_domain_access') }}">
  <input type="hidden" name="domain_id" value="{{ d.id }}">
""" + ACCESS_SELECTS + """
  <button class="add" type="submit" style="margin-top:.8rem;">Save access</button>
</form>
<p class="hint">
  Pick any combination of specific users, groups, and/or devices -- hold Ctrl/Cmd (or Shift for a
  range) to select more than one in a list. Saving replaces the current assignment with exactly
  what's selected, so removing access is the same action as granting it: just change the selection.
  "Everyone" grants it regardless of the lists below, but they're still saved underneath it, so
  turning "Everyone" back off later doesn't lose them.
</p>
</div>

{% if d.mode == 'bump' %}
<div class="card">
<h2>Allowed paths ({{ paths|length }})</h2>
<p class="hint">Regex patterns matched against the request path. Leave empty to allow any path on this domain once it's otherwise permitted.</p>
{% if prefill_path %}
<p class="hint">A blocked request suggested the pattern below (derived from the actual path that was denied) -- review it, broaden or narrow it as needed, then save.</p>
{% endif %}
<div class="table-scroll">
<table>
  <tr><th>Pattern</th><th></th></tr>
  {% for p in paths %}
  <tr>
    <td><code>{{ p.pattern }}</code></td>
    <td>
      <form class="inline" method="post" action="{{ url_for('delete_path') }}">
        <input type="hidden" name="path_id" value="{{ p.id }}">
        <button class="danger small" type="submit">Remove</button>
      </form>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="2"><em>No path restriction -- any path is allowed once the domain check passes.</em></td></tr>
  {% endfor %}
</table>
</div>
<form class="add-form" method="post" action="{{ url_for('add_path') }}">
  <input type="hidden" name="domain_id" value="{{ d.id }}">
  <input type="text" name="pattern" placeholder="e.g. ^/discover" value="{{ prefill_path or '' }}" required>
  <button class="add" type="submit">Add path</button>
</form>
</div>
{% endif %}
"""


@app.route("/domains/<int:domain_id>")
@require_admin
def domain_detail(domain_id: int):
    conn = get_db()
    d = conn.execute("SELECT * FROM domains WHERE id = ?", (domain_id,)).fetchone()
    if d is None:
        return flash_redirect("domains", "That domain no longer exists.", error=True)
    all_users = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    all_groups = conn.execute("SELECT * FROM groups ORDER BY name").fetchall()
    all_devices = conn.execute("SELECT * FROM devices ORDER BY COALESCE(label, mac_address)").fetchall()
    assigned_user_ids = {
        row["user_id"] for row in
        conn.execute("SELECT user_id FROM user_domains WHERE domain_id = ?", (domain_id,))
    }
    assigned_group_ids = {
        row["group_id"] for row in
        conn.execute("SELECT group_id FROM group_domains WHERE domain_id = ?", (domain_id,))
    }
    assigned_device_ids = {
        row["device_id"] for row in
        conn.execute("SELECT device_id FROM device_domains WHERE domain_id = ?", (domain_id,))
    }
    paths = conn.execute(
        "SELECT * FROM domain_paths WHERE domain_id = ? ORDER BY pattern", (domain_id,)
    ).fetchall()
    body = render_template_string(
        DOMAIN_DETAIL_BODY, d=d, all_users=all_users, all_groups=all_groups, all_devices=all_devices,
        preselected_user_ids=assigned_user_ids, preselected_group_ids=assigned_group_ids,
        preselected_device_ids=assigned_device_ids, is_global_checked=bool(d["is_global"]),
        paths=paths, prefill_path=request.args.get("prefill_path"),
    )
    return render("domains", body)


@app.route("/domains/update", methods=["POST"])
@require_admin
def update_domain():
    domain_id = request.form.get("domain_id", "")
    mode = request.form.get("mode", "splice")
    note = request.form.get("note", "").strip() or None
    conn = get_db()
    conn.execute(
        "UPDATE domains SET mode = ?, note = ? WHERE id = ?",
        (mode, note, domain_id),
    )
    conn.commit()
    return flash_redirect("domain_detail", "Saved.", domain_id=domain_id)


@app.route("/domains/access", methods=["POST"])
@require_admin
def update_domain_access():
    """Replaces a domain's entire access grant (Everyone + users + groups
    + devices) with exactly what was submitted -- granting and revoking
    are the same action here, just a changed selection, rather than
    separate add/remove endpoints per assignment type."""
    domain_id = request.form.get("domain_id", "")
    is_global = 1 if request.form.get("is_global") else 0
    user_ids = {int(x) for x in request.form.getlist("user_ids") if x.isdigit()}
    group_ids = {int(x) for x in request.form.getlist("group_ids") if x.isdigit()}
    device_ids = {int(x) for x in request.form.getlist("device_ids") if x.isdigit()}

    conn = get_db()
    conn.execute("UPDATE domains SET is_global = ? WHERE id = ?", (is_global, domain_id))
    conn.execute("DELETE FROM user_domains WHERE domain_id = ?", (domain_id,))
    for uid in user_ids:
        conn.execute("INSERT OR IGNORE INTO user_domains (user_id, domain_id) VALUES (?,?)", (uid, domain_id))
    conn.execute("DELETE FROM group_domains WHERE domain_id = ?", (domain_id,))
    for gid in group_ids:
        conn.execute("INSERT OR IGNORE INTO group_domains (group_id, domain_id) VALUES (?,?)", (gid, domain_id))
    conn.execute("DELETE FROM device_domains WHERE domain_id = ?", (domain_id,))
    for did in device_ids:
        conn.execute("INSERT OR IGNORE INTO device_domains (device_id, domain_id) VALUES (?,?)", (did, domain_id))
    conn.commit()
    return flash_redirect("domain_detail", "Access updated.", domain_id=domain_id)


@app.route("/domains/paths/add", methods=["POST"])
@require_admin
def add_path():
    domain_id = request.form.get("domain_id", "")
    pattern = request.form.get("pattern", "").strip()
    if not pattern:
        return flash_redirect("domain_detail", "Pattern is required.", error=True, domain_id=domain_id)
    if len(pattern) > 200:
        return flash_redirect("domain_detail", "Pattern too long (200 characters max).", error=True, domain_id=domain_id)
    try:
        re.compile(pattern)
    except re.error as exc:
        return flash_redirect("domain_detail", f"Not a valid regex: {exc}", error=True, domain_id=domain_id)
    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO domain_paths (domain_id, pattern) VALUES (?,?)", (domain_id, pattern)
    )
    conn.commit()
    return flash_redirect("domain_detail", "Path added.", domain_id=domain_id)


@app.route("/domains/paths/delete", methods=["POST"])
@require_admin
def delete_path():
    path_id = request.form.get("path_id", "")
    conn = get_db()
    row = conn.execute("SELECT domain_id FROM domain_paths WHERE id = ?", (path_id,)).fetchone()
    conn.execute("DELETE FROM domain_paths WHERE id = ?", (path_id,))
    conn.commit()
    domain_id = row["domain_id"] if row else None
    return flash_redirect("domain_detail", "Path removed.", domain_id=domain_id)


# ==========================================================
# DEVICES (v2 roadmap groundwork -- see common/db.py's `devices` table
# comment. Nothing in the proxy/dashboard enforcement path reads these
# flags yet; this page just lets an admin start tracking devices and
# curating the future SSL-Bump list ahead of the interception-layer work.)
# ==========================================================

MAC_ADDRESS_RE = re.compile(r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")


def normalize_mac(value: str) -> str | None:
    """Accepts colon- or hyphen-separated hex pairs, returns lowercase
    colon-separated form, or None if it isn't a MAC address at all."""
    value = (value or "").strip().lower().replace("-", ":")
    return value if MAC_ADDRESS_RE.match(value) else None


def _device_assignment_value(d) -> str:
    """The current selection for the composite assignment <select> below,
    given a devices row -- inverse of _parse_device_assignment()."""
    if d["ignored"]:
        return "ignored"
    if d["user_id"]:
        return f"user:{d['user_id']}"
    if d["group_id"]:
        return f"group:{d['group_id']}"
    return ""


def _parse_device_assignment(raw: str) -> tuple[int | None, int | None, int]:
    """Parses the composite assignment <select>'s value into
    (user_id, group_id, ignored). Anything unrecognized (including a
    malformed id) falls back to unassigned rather than raising -- this is
    admin-only input from a dropdown we control, but defend anyway."""
    raw = raw or ""
    try:
        if raw == "ignored":
            return None, None, 1
        if raw.startswith("user:"):
            return int(raw[len("user:"):]), None, 0
        if raw.startswith("group:"):
            return None, int(raw[len("group:"):]), 0
    except ValueError:
        pass
    return None, None, 0


# Shared by the add-device form and the per-device Manage form -- one
# control picks "unassigned" / "ignore this device entirely" / a specific
# kid / a specific group, so there's no separate always-visible kid+group
# dropdown pair to keep in sync (this app doesn't use JS to show/hide
# fields based on another field's value).
DEVICE_ASSIGNMENT_SELECT = """
  <select name="assignment">
    <option value="" {{ 'selected' if current=='' }}>Unassigned</option>
    <option value="ignored" {{ 'selected' if current=='ignored' }}>Ignore (never filtered)</option>
    {% if all_users %}
    <optgroup label="Kid">
      {% for u in all_users %}
      <option value="user:{{ u.id }}" {{ 'selected' if current==('user:' ~ u.id) }}>{{ u.display_name }}</option>
      {% endfor %}
    </optgroup>
    {% endif %}
    {% if all_groups %}
    <optgroup label="Group">
      {% for g in all_groups %}
      <option value="group:{{ g.id }}" {{ 'selected' if current==('group:' ~ g.id) }}>{{ g.name }}</option>
      {% endfor %}
    </optgroup>
    {% endif %}
  </select>
"""


DEVICES_BODY = """
<div class="card">
<h2>Groups ({{ groups|length }})</h2>
<p class="hint">A shared-device category (TVs, IoT, Gaming Computers) with its own domain allow-list -- assign devices to a group below, then manage what it can reach from its "Manage domains" link.</p>
<div class="table-scroll">
<table>
  <tr><th>Name</th><th></th></tr>
  {% for g in groups %}
  <tr>
    <td>{{ g.name }}</td>
    <td>
      <a class="btn small" href="{{ url_for('domains', group_id=g.id) }}">Manage domains</a>
      <form class="inline" method="post" action="{{ url_for('delete_group') }}">
        <input type="hidden" name="group_id" value="{{ g.id }}">
        <button class="danger small" type="submit" onclick="return confirm('Delete this group? Its devices become unassigned.')">Delete</button>
      </form>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="2"><em>No groups yet.</em></td></tr>
  {% endfor %}
</table>
</div>
<form class="add-form" method="post" action="{{ url_for('add_group') }}">
  <input type="text" name="name" placeholder="e.g. TVs, IoT, Gaming Computers" required>
  <button class="add" type="submit">Add group</button>
</form>
</div>

<div class="card">
<h2>Devices ({{ devices|length }})</h2>
<p class="hint">
  Track known devices by MAC address ahead of the interception-layer work.
  <span class="badge mode-bump">SSL-Bump</span> devices will get full
  path/show-level rules on bump-mode domains once that's wired up -- keep
  this list small and deliberate. Everything else will get that domain's
  whole-domain treatment instead. Nothing here is enforced yet.
</p>
<div class="table-scroll">
<table>
  <tr><th>MAC address</th><th>Label</th><th>Assigned to</th><th>SSL-Bump</th><th>Bypass login</th><th></th></tr>
  {% for d in devices %}
  <tr>
    <td><code>{{ d.mac_address }}</code></td>
    <td>{{ d.label or '' }}</td>
    <td>
      {% if d.ignored %}<span class="badge pending">Ignored</span>
      {% elif d.display_name %}{{ d.display_name }}
      {% elif d.group_name %}<span class="badge mode-trusted">{{ d.group_name }}</span>
      {% else %}<em>Unassigned</em>{% endif %}
    </td>
    <td>{% if d.bump_enabled %}<span class="badge mode-bump">yes</span>{% else %}<span class="badge mode-splice">no</span>{% endif %}</td>
    <td>{% if d.bypass_login %}<span class="badge pending">yes</span>{% else %}&mdash;{% endif %}</td>
    <td>
      <a class="btn small" href="{{ url_for('device_detail', device_id=d.id) }}">Manage</a>
      <a class="btn small" href="{{ url_for('domains', device_id=d.id) }}">Domains</a>
      <form class="inline" method="post" action="{{ url_for('delete_device') }}">
        <input type="hidden" name="device_id" value="{{ d.id }}">
        <button class="danger small" type="submit" onclick="return confirm('Remove this device?')">Delete</button>
      </form>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="6"><em>No devices tracked yet.</em></td></tr>
  {% endfor %}
</table>
</div>
<form class="add-form" method="post" action="{{ url_for('add_device') }}">
  <input type="text" name="mac_address" placeholder="aa:bb:cc:dd:ee:ff" required>
  <input type="text" name="label" placeholder="Label, e.g. Alex's iPad">
""" + DEVICE_ASSIGNMENT_SELECT + """
  <button class="add" type="submit">Add device</button>
</form>
</div>
"""


@app.route("/devices")
@require_admin
def devices():
    conn = get_db()
    rows = conn.execute(
        "SELECT d.*, u.display_name, g.name AS group_name FROM devices d "
        "LEFT JOIN users u ON u.id = d.user_id "
        "LEFT JOIN groups g ON g.id = d.group_id "
        "ORDER BY d.created_at DESC"
    ).fetchall()
    all_users = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    all_groups = conn.execute("SELECT * FROM groups ORDER BY name").fetchall()
    return render(
        "devices",
        render_template_string(
            DEVICES_BODY, devices=rows, groups=all_groups, all_users=all_users, all_groups=all_groups,
            current="",
        ),
    )


@app.route("/devices/add", methods=["POST"])
@require_admin
def add_device():
    mac = normalize_mac(request.form.get("mac_address", ""))
    if mac is None:
        return flash_redirect(
            "devices", "Enter a valid MAC address, e.g. aa:bb:cc:dd:ee:ff.", error=True
        )
    label = request.form.get("label", "").strip() or None
    user_id, group_id, ignored = _parse_device_assignment(request.form.get("assignment", ""))
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO devices (mac_address, label, user_id, group_id, ignored, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (mac, label, user_id, group_id, ignored, db.now_iso()),
        )
        conn.commit()
    except Exception as exc:
        if "UNIQUE" in str(exc):
            return flash_redirect("devices", f"{mac} is already tracked.", error=True)
        raise
    return flash_redirect("devices", f"Added {mac}.")


DEVICE_DETAIL_BODY = """
<p><a href="{{ url_for('devices') }}">&larr; All devices</a></p>
<h1><code>{{ d.mac_address }}</code></h1>

<div class="card">
<form class="add-form" method="post" action="{{ url_for('update_device') }}">
  <input type="hidden" name="device_id" value="{{ d.id }}">
  <input type="text" name="label" value="{{ d.label or '' }}" placeholder="Label, e.g. Alex's iPad">
""" + DEVICE_ASSIGNMENT_SELECT + """
  <label><input type="checkbox" name="bump_enabled" {{ 'checked' if d.bump_enabled }}> SSL-Bump enabled</label>
  <label><input type="checkbox" name="bypass_login" {{ 'checked' if d.bypass_login }}> Bypass login</label>
  <button class="add" type="submit">Save</button>
</form>
<p class="hint">
  <strong>Ignore</strong> means this device is never touched at all -- stronger
  than "Unassigned" (a known device with no policy decided yet). <strong>SSL-Bump
  enabled</strong> marks this as one of the small, deliberately curated devices
  that will get full path/show-level rules on bump-mode domains -- everything
  else will fall back to whole-domain treatment once the DNS/interception tier
  exists. <strong>Bypass login</strong> is for a device that can never complete
  a login flow (a smart TV, Echo, thermostat) -- it'll be exempted from the
  future captive-portal gate and fall back to its assignment above instead of
  a personal login. None of this is enforced yet -- this page is groundwork
  for that work.
</p>
</div>
"""


@app.route("/devices/<int:device_id>")
@require_admin
def device_detail(device_id: int):
    conn = get_db()
    d = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if d is None:
        return flash_redirect("devices", "That device no longer exists.", error=True)
    all_users = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    all_groups = conn.execute("SELECT * FROM groups ORDER BY name").fetchall()
    return render(
        "devices",
        render_template_string(
            DEVICE_DETAIL_BODY, d=d, all_users=all_users, all_groups=all_groups,
            current=_device_assignment_value(d),
        ),
    )


@app.route("/devices/update", methods=["POST"])
@require_admin
def update_device():
    device_id = request.form.get("device_id", "")
    label = request.form.get("label", "").strip() or None
    user_id, group_id, ignored = _parse_device_assignment(request.form.get("assignment", ""))
    bump_enabled = 1 if request.form.get("bump_enabled") else 0
    bypass_login = 1 if request.form.get("bypass_login") else 0
    conn = get_db()
    conn.execute(
        "UPDATE devices SET label = ?, user_id = ?, group_id = ?, ignored = ?, "
        "bump_enabled = ?, bypass_login = ? WHERE id = ?",
        (label, user_id, group_id, ignored, bump_enabled, bypass_login, device_id),
    )
    conn.commit()
    return flash_redirect("device_detail", "Saved.", device_id=device_id)


@app.route("/devices/delete", methods=["POST"])
@require_admin
def delete_device():
    device_id = request.form.get("device_id", "")
    conn = get_db()
    conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    conn.commit()
    return flash_redirect("devices", "Device removed.")


@app.route("/groups/add", methods=["POST"])
@require_admin
def add_group():
    name = request.form.get("name", "").strip()
    if not name:
        return flash_redirect("devices", "Group name is required.", error=True)
    if len(name) > 100:
        return flash_redirect("devices", "Group name too long (100 characters max).", error=True)
    conn = get_db()
    try:
        conn.execute("INSERT INTO groups (name, created_at) VALUES (?,?)", (name, db.now_iso()))
        conn.commit()
    except Exception as exc:
        if "UNIQUE" in str(exc):
            return flash_redirect("devices", f"A group named {name!r} already exists.", error=True)
        raise
    return flash_redirect("devices", f"Added group {name}.")


@app.route("/groups/delete", methods=["POST"])
@require_admin
def delete_group():
    group_id = request.form.get("group_id", "")
    conn = get_db()
    conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
    conn.commit()
    return flash_redirect("devices", "Group removed.")


# ==========================================================
# REPORT
# ==========================================================

REPORT_BODY = """
{% if resolver_error %}
<div class="flash error">
  Crunchyroll metadata lookups are currently failing
  (<code>{{ resolver_error }}</code>). Shows already resolved keep working from
  cache; newly approved shows can't be verified until this clears. If it
  persists, the anonymous Crunchyroll client id in
  <code>common/cr_api.py</code> may need re-deriving.
</div>
{% endif %}

{% if pending_requests %}
<div class="card pending-card">
<h2>Pending approval requests ({{ pending_requests|length }})</h2>
<p class="hint">Someone tapped "Request approval" on a blocked page. These stay listed (regardless of the filter below) until you act on them.</p>
<div class="table-scroll">
<table>
  <tr><th>Requested (UTC)</th><th>Kid</th><th>Domain</th><th>Show / Path</th><th></th></tr>
  {% for row in pending_requests %}
  <tr>
    <td>{{ row.approval_requested_at }}</td>
    <td>{{ row.username }}</td>
    <td><code>{{ row.domain }}</code></td>
    <td>{{ row.series_name or row.series_id or row.path or '' }}</td>
    <td>
      {% if row.reason == 'path_not_allowed' %}
      <form class="inline" method="post" action="{{ url_for('approve_from_report') }}">
        <input type="hidden" name="log_id" value="{{ row.id }}">
        {% for k, v in redirect_kwargs.items() %}<input type="hidden" name="{{ k }}" value="{{ v }}">{% endfor %}
        <button class="add small" type="submit">Review path</button>
      </form>
      {% else %}
      <form class="inline" method="post" action="{{ url_for('approve_from_report') }}">
        <input type="hidden" name="log_id" value="{{ row.id }}">
        <input type="hidden" name="scope" value="user">
        {% for k, v in redirect_kwargs.items() %}<input type="hidden" name="{{ k }}" value="{{ v }}">{% endfor %}
        <button class="add small" type="submit">Approve</button>
      </form>
      <form class="inline" method="post" action="{{ url_for('approve_from_report') }}">
        <input type="hidden" name="log_id" value="{{ row.id }}">
        <input type="hidden" name="scope" value="global">
        {% for k, v in redirect_kwargs.items() %}<input type="hidden" name="{{ k }}" value="{{ v }}">{% endfor %}
        <button class="small" type="submit">Approve for everyone</button>
      </form>
      {% endif %}
      <form class="inline" method="post" action="{{ url_for('dismiss_request') }}">
        <input type="hidden" name="log_id" value="{{ row.id }}">
        {% for k, v in redirect_kwargs.items() %}<input type="hidden" name="{{ k }}" value="{{ v }}">{% endfor %}
        <button class="small" type="submit">Dismiss</button>
      </form>
    </td>
  </tr>
  {% endfor %}
</table>
</div>
</div>
{% endif %}

<div class="card">
<h2>Filter</h2>
<form class="add-form" method="get" action="{{ url_for('report') }}">
  <select name="user">
    <option value="">All kids</option>
    {% for u in all_users %}
    <option value="{{ u.username }}" {{ 'selected' if filter_user==u.username }}>{{ u.display_name }}</option>
    {% endfor %}
  </select>
  <select name="status">
    <option value="">All</option>
    <option value="blocked" {{ 'selected' if filter_status=='blocked' }}>Blocked only</option>
    <option value="allowed" {{ 'selected' if filter_status=='allowed' }}>Allowed only</option>
  </select>
  <select name="days">
    {% for d in day_options %}
    <option value="{{ d }}" {{ 'selected' if days==d }}>Last {{ d }} day{{ 's' if d != 1 else '' }}</option>
    {% endfor %}
  </select>
  <button class="add" type="submit">Apply</button>
  {% if filters_active %}<a class="btn" href="{{ url_for('report') }}">Clear filters</a>{% endif %}
</form>
<p class="hint">Applies to everything below -- the totals, both graphs, and the activity table. Click Allowed or Blocked below to filter to just that.</p>
</div>

<div class="stat-strip">
  <div class="stat"><div class="stat-value">{{ total }}</div><div class="stat-label">Requests shown</div></div>
  <a class="stat-link {{ 'active' if filter_status=='allowed' }}" href="{{ url_for('report', user=filter_user, status='allowed', days=days) }}">
    <div class="stat"><div class="stat-value">{{ allowed_total }}</div><div class="stat-label">Allowed</div></div>
  </a>
  <a class="stat-link {{ 'active' if filter_status=='blocked' }}" href="{{ url_for('report', user=filter_user, status='blocked', days=days) }}">
    <div class="stat"><div class="stat-value">{{ blocked_total }}</div><div class="stat-label">Blocked</div></div>
  </a>
  <div class="stat"><div class="stat-value">{{ (blocked_pct ~ '%') if total else '--' }}</div><div class="stat-label">% blocked</div></div>
</div>

<div class="chart-grid">
  <div class="card">
    <h2>Activity, last {{ days }} day{{ 's' if days != 1 else '' }}</h2>
    <div class="chart-card">
      {% if total %}<canvas id="activity-chart"></canvas>{% else %}<div class="empty-note">No activity logged yet.</div>{% endif %}
    </div>
  </div>
  <div class="card">
    <h2>Top domains</h2>
    <div class="chart-card">
      {% if top_domains %}<canvas id="domains-chart"></canvas>{% else %}<div class="empty-note">No activity logged yet.</div>{% endif %}
    </div>
  </div>
</div>

<div class="card">
<h2>Recent activity</h2>
<div class="table-scroll">
<table>
  <tr><th>Time (UTC)</th><th>User</th><th>Domain</th><th>Show / Path</th><th>Result</th><th></th></tr>
  {% for row in rows %}
  <tr>
    <td>{{ row.ts }}</td>
    <td>{{ row.username }}</td>
    <td><code>{{ row.domain }}</code></td>
    <td>{{ row.series_name or row.series_id or row.path or '' }}</td>
    <td><span class="badge {{ 'allowed' if row.allowed else 'blocked' }}">{{ 'allowed' if row.allowed else 'blocked' }}</span></td>
    <td>
      {% if not row.allowed and row.user_id %}
      <form class="inline" method="post" action="{{ url_for('approve_from_report') }}">
        <input type="hidden" name="log_id" value="{{ row.id }}">
        {% for k, v in redirect_kwargs.items() %}<input type="hidden" name="{{ k }}" value="{{ v }}">{% endfor %}
        <button class="add small" type="submit">Approve for {{ row.username }}</button>
      </form>
      {% endif %}
    </td>
  </tr>
  {% else %}
  <tr><td colspan="6"><em>No activity logged yet.</em></td></tr>
  {% endfor %}
</table>
</div>
</div>

{% if total %}
<script src="{{ url_for('static', filename='vendor/chart.umd.min.js') }}"></script>
<script>
(function () {
  var style = getComputedStyle(document.documentElement);
  var cGreen = style.getPropertyValue('--success').trim() || '#15803d';
  var cRed = style.getPropertyValue('--danger').trim() || '#b91c1c';
  var cBrand = style.getPropertyValue('--brand').trim() || '#2f6fed';
  var cMuted = style.getPropertyValue('--text-muted').trim() || '#64748b';
  var cGrid = style.getPropertyValue('--border').trim() || '#e2e8f0';

  Chart.defaults.color = cMuted;
  Chart.defaults.borderColor = cGrid;

  var activityEl = document.getElementById('activity-chart');
  if (activityEl) {
    new Chart(activityEl, {
      type: 'bar',
      data: {
        labels: {{ day_labels|tojson }},
        datasets: [
          { label: 'Allowed', data: {{ daily_allowed|tojson }}, backgroundColor: cGreen, stack: 's' },
          { label: 'Blocked', data: {{ daily_blocked|tojson }}, backgroundColor: cRed, stack: 's' }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { stacked: true },
          y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } }
        },
        plugins: { legend: { position: 'bottom' } }
      }
    });
  }

  var domainsEl = document.getElementById('domains-chart');
  if (domainsEl) {
    new Chart(domainsEl, {
      type: 'bar',
      data: {
        labels: {{ top_domain_labels|tojson }},
        datasets: [{ label: 'Requests', data: {{ top_domain_counts|tojson }}, backgroundColor: cBrand }]
      },
      options: {
        indexAxis: 'y',
        responsive: true,
        maintainAspectRatio: false,
        scales: { x: { beginAtZero: true, ticks: { precision: 0 } } },
        plugins: { legend: { display: false } }
      }
    });
  }
})();
</script>
{% endif %}
"""


REPORT_DAY_OPTIONS = (1, 7, 14, 30)
REPORT_DEFAULT_DAYS = 7


def _parse_report_days(value) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return REPORT_DEFAULT_DAYS
    return days if days in REPORT_DAY_OPTIONS else REPORT_DEFAULT_DAYS


def _report_redirect_kwargs(source) -> dict:
    """Pulls the current user/status/days filter off `source` (request.args
    for the page itself, request.form for an action taken from it) so every
    approve/dismiss click redirects back to the same filtered view instead
    of silently resetting it to the defaults."""
    kwargs = {}
    if source.get("user"):
        kwargs["user"] = source["user"]
    if source.get("status"):
        kwargs["status"] = source["status"]
    if source.get("days"):
        kwargs["days"] = _parse_report_days(source.get("days"))
    return kwargs


@app.route("/report")
@require_admin
def report():
    conn = get_db()
    filter_user = request.args.get("user", "")
    filter_status = request.args.get("status", "")
    days = _parse_report_days(request.args.get("days"))

    # The date range applies to literally everything below (stat strip, both
    # charts, and the activity table) -- one `where_sql` built once, reused
    # by every query, so there's no way for the charts and the table to
    # disagree about what date/kid/status window "the Report page" means.
    where_sql = "WHERE ts >= ?"
    params: list = [db.iso_secs_ago(days * 86400)]
    if filter_user:
        where_sql += " AND username = ?"
        params.append(filter_user)
    if filter_status == "blocked":
        where_sql += " AND allowed = 0"
    elif filter_status == "allowed":
        where_sql += " AND allowed = 1"

    rows = conn.execute(
        f"SELECT * FROM access_log {where_sql} ORDER BY id DESC LIMIT 200", params
    ).fetchall()

    # Chart/stat data reflects every matching row under the current filter,
    # not just the 200 most recent shown in the table below -- these are
    # separate aggregate queries, not derived from `rows`.
    status_counts = conn.execute(
        f"SELECT allowed, COUNT(*) c FROM access_log {where_sql} GROUP BY allowed", params
    ).fetchall()
    allowed_total = next((r["c"] for r in status_counts if r["allowed"]), 0)
    blocked_total = next((r["c"] for r in status_counts if not r["allowed"]), 0)
    total = allowed_total + blocked_total
    blocked_pct = round(blocked_total / total * 100) if total else 0

    top_domains = conn.execute(
        f"SELECT domain, COUNT(*) c FROM access_log {where_sql} GROUP BY domain ORDER BY c DESC LIMIT 8",
        params,
    ).fetchall()

    today = datetime.now(timezone.utc).date()
    chart_days = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    daily_rows = conn.execute(
        f"SELECT substr(ts,1,10) day, allowed, COUNT(*) c FROM access_log {where_sql} "
        "GROUP BY day, allowed ORDER BY day",
        params,
    ).fetchall()
    allowed_by_day = {r["day"]: r["c"] for r in daily_rows if r["allowed"]}
    blocked_by_day = {r["day"]: r["c"] for r in daily_rows if not r["allowed"]}

    # Independent of the user/status/days filter above -- this is a
    # persistent "needs attention" list, not part of the filtered activity
    # view, so it stays visible regardless of what window is being browsed.
    pending_requests = conn.execute(
        "SELECT * FROM access_log WHERE approval_requested_at IS NOT NULL "
        "ORDER BY approval_requested_at DESC"
    ).fetchall()

    all_users = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    body = render_template_string(
        REPORT_BODY, rows=rows, all_users=all_users, pending_requests=pending_requests,
        filter_user=filter_user, filter_status=filter_status, days=days, day_options=REPORT_DAY_OPTIONS,
        filters_active=bool(filter_user or filter_status or days != REPORT_DEFAULT_DAYS),
        redirect_kwargs=_report_redirect_kwargs(request.args),
        resolver_error=db.get_setting(conn, "cr_resolver_last_error"),
        total=total, allowed_total=allowed_total, blocked_total=blocked_total, blocked_pct=blocked_pct,
        top_domains=top_domains,
        top_domain_labels=[r["domain"] for r in top_domains],
        top_domain_counts=[r["c"] for r in top_domains],
        day_labels=[d.strftime("%m/%d") for d in chart_days],
        daily_allowed=[allowed_by_day.get(d.isoformat(), 0) for d in chart_days],
        daily_blocked=[blocked_by_day.get(d.isoformat(), 0) for d in chart_days],
    )
    return render("report", body)


@app.route("/report/approve", methods=["POST"])
@require_admin
def approve_from_report():
    log_id = request.form.get("log_id", "")
    # "user" = just the person who hit this (the default, and the only
    # option the plain Recent-activity table's inline button offers).
    # "global" = approve for everyone -- only offered from the pending-
    # requests card, since that's the one place a per-request choice makes
    # sense to surface.
    scope = request.form.get("scope", "user")
    redirect_kwargs = _report_redirect_kwargs(request.form)
    conn = get_db()
    row = conn.execute("SELECT * FROM access_log WHERE id = ?", (log_id,)).fetchone()
    if row is None or row["user_id"] is None:
        return flash_redirect("report", "Couldn't find that log entry.", error=True, **redirect_kwargs)

    # Whatever happens below, the admin has now acted on this row -- clear
    # any outstanding "Request approval" flag so it drops off the pending
    # list. Harmless no-op if it was never set.
    conn.execute("UPDATE access_log SET approval_requested_at = NULL WHERE id = ?", (log_id,))
    conn.commit()

    if row["series_id"]:
        name = cr_api.series_title(row["series_id"]) or row["series_id"]
        if scope == "global":
            # user_shows has no is_global concept (unlike domains) -- "for
            # everyone" means literally granting every existing user.
            for user_row in conn.execute("SELECT id FROM users"):
                conn.execute(
                    "INSERT INTO user_shows (user_id, series_id, series_name) VALUES (?,?,?) "
                    "ON CONFLICT(user_id, series_id) DO UPDATE SET series_name = excluded.series_name",
                    (user_row["id"], row["series_id"], name),
                )
            conn.commit()
            return flash_redirect("report", f"Approved {name} for everyone.", **redirect_kwargs)
        conn.execute(
            "INSERT INTO user_shows (user_id, series_id, series_name) VALUES (?,?,?) "
            "ON CONFLICT(user_id, series_id) DO UPDATE SET series_name = excluded.series_name",
            (row["user_id"], row["series_id"], name),
        )
        conn.commit()
        return flash_redirect("report", f"Approved {name} for {row['username']}.", **redirect_kwargs)

    if row["reason"] == "path_not_allowed":
        # The domain is already assigned to this user -- that's *why* the
        # request reached the path check at all -- so INSERT OR IGNORE into
        # user_domains below would be a silent no-op and the identical
        # request would be denied again immediately (GH #6). A path pattern
        # governs future access, not a one-time yes/no, so send the admin
        # to review a derived starting pattern rather than auto-saving one.
        # Path rules are already domain-wide (see domain_paths' schema
        # comment), so there's no separate "for everyone" version of this.
        domain = matching.find_domain(conn, row["domain"])
        if domain is None:
            return flash_redirect("report", "Couldn't find that domain anymore.", error=True, **redirect_kwargs)
        return redirect(url_for(
            "domain_detail", domain_id=domain["id"], prefill_path=path_to_pattern(row["path"]),
        ))

    domain = matching.find_domain(conn, row["domain"])
    if domain is None:
        # Never seen this domain before (it wasn't blocked by an existing
        # rule -- it just wasn't configured at all). Create it rather than
        # dead-ending: splice mode (host-only, matches the default for any
        # new domain), scoped per `scope` -- global if approved for
        # everyone, otherwise just this user, the safest default for
        # something approved reactively from a block.
        pattern = re.escape(row["domain"])
        is_global = 1 if scope == "global" else 0
        conn.execute(
            "INSERT OR IGNORE INTO domains (pattern, mode, kind, is_global, note, created_at) "
            "VALUES (?, 'splice', 'generic', ?, 'Auto-added from report approval', ?)",
            (pattern, is_global, db.now_iso()),
        )
        domain = conn.execute("SELECT * FROM domains WHERE pattern = ?", (pattern,)).fetchone()
    if scope == "global":
        if not domain["is_global"]:
            conn.execute("UPDATE domains SET is_global = 1 WHERE id = ?", (domain["id"],))
    else:
        conn.execute(
            "INSERT OR IGNORE INTO user_domains (user_id, domain_id) VALUES (?,?)",
            (row["user_id"], domain["id"]),
        )
    conn.commit()
    label = "everyone" if scope == "global" else row["username"]
    return flash_redirect("report", f"Approved {row['domain']} for {label}.", **redirect_kwargs)


@app.route("/report/dismiss-request", methods=["POST"])
@require_admin
def dismiss_request():
    """Clears a pending 'Request approval' flag without granting anything --
    the site/show stays exactly as denied as it was before the request. This
    is the admin's "no" -- distinct from Approve, and distinct from doing
    nothing (which would leave it cluttering the pending list forever)."""
    log_id = request.form.get("log_id", "")
    redirect_kwargs = _report_redirect_kwargs(request.form)
    conn = get_db()
    conn.execute("UPDATE access_log SET approval_requested_at = NULL WHERE id = ?", (log_id,))
    conn.commit()
    return flash_redirect("report", "Dismissed.", **redirect_kwargs)


# ==========================================================
# SETTINGS
# ==========================================================

SETTINGS_BODY = """
<div class="card">
<h2>Local network</h2>
<form class="add-form" method="post" action="{{ url_for('update_local_network') }}">
  <input type="text" name="local_network" value="{{ local_network }}" style="flex:1; min-width:280px;">
  <button class="add" type="submit">Save</button>
</form>
<p class="hint">Space-separated CIDRs, e.g. <code>192.168.1.0/24 192.168.0.0/24</code>. Requests from outside these ranges are denied regardless of user/site rules. <strong>Leave blank to disable this check</strong> and rely only on per-person proxy logins &mdash; do that if the proxy runs under Docker Desktop or bridge networking, where it sees an internal gateway address instead of the real client IP and this check would otherwise block everyone.</p>
</div>

<div class="card">
<h2>Blocked-site experience</h2>
<form class="add-form" method="post" action="{{ url_for('update_block_page_mode') }}">
  <select name="block_page_mode">
    <option value="terminate" {{ 'selected' if block_page_mode=='terminate' }}>Just fail the connection (default -- safe for devices that haven't installed the certificate yet)</option>
    <option value="redirect" {{ 'selected' if block_page_mode=='redirect' }}>Show a friendly page (requires the CA certificate already trusted on the device)</option>
  </select>
  <button class="add" type="submit">Save</button>
</form>
<p class="hint">
  <strong>Only switch to "Show a friendly page" after confirming the CA certificate is installed and trusted on every device this applies to.</strong>
  Showing a page requires decrypting that connection with the proxy's own certificate -- exactly like Crunchyroll already does. If a device hasn't trusted that certificate yet, it'll see a security warning ("connection not private") instead of a clean block message, which is more alarming than the plain connection failure it replaces. A simple way to check: if Crunchyroll itself loads correctly on a device, that device's certificate trust is set up correctly and this mode will work fine for it too. This is one setting for every device on the network -- there's no per-device override.
</p>
<p class="hint">
  Bump-mode domains (Crunchyroll, or anything else you've set to bump mode) always show a page when blocked regardless of this setting, since they're already decrypted either way. This setting only affects splice-mode sites. To get an actual custom page here rather than Squid's generic one, also set <code>DASHBOARD_URL</code> in <code>.env</code> to this machine's address (e.g. <code>http://192.168.1.50:8787</code>) and restart the proxy container.
</p>
</div>

<div class="card">
<h2>Dashboard admin login</h2>
<form class="add-form" method="post" action="{{ url_for('update_admin') }}">
  <input type="text" name="admin_username" value="{{ admin_username }}" placeholder="Admin username">
  <input type="password" name="admin_password" placeholder="New password (leave blank to keep current)">
  <button class="add" type="submit">Save</button>
</form>
</div>
"""


@app.route("/settings")
@require_admin
def settings_page():
    conn = get_db()
    local_network = db.get_setting(conn, "local_network", "")
    admin_username = db.get_setting(conn, "admin_username", "")
    block_page_mode = db.get_setting(conn, "block_page_mode", "terminate")
    body = render_template_string(
        SETTINGS_BODY, local_network=local_network, admin_username=admin_username,
        block_page_mode=block_page_mode,
    )
    return render("settings", body)


@app.route("/settings/local-network", methods=["POST"])
@require_admin
def update_local_network():
    value = request.form.get("local_network", "").strip()
    conn = get_db()
    db.set_setting(conn, "local_network", value)
    conn.commit()
    if not value:
        return flash_redirect(
            "settings_page",
            "Saved. LAN restriction disabled -- access is now controlled only "
            "by each person's proxy login.",
        )
    return flash_redirect("settings_page", "Saved.")


@app.route("/settings/block-page-mode", methods=["POST"])
@require_admin
def update_block_page_mode():
    value = request.form.get("block_page_mode", "redirect")
    if value not in ("redirect", "terminate"):
        return flash_redirect("settings_page", "Invalid option.", error=True)
    conn = get_db()
    db.set_setting(conn, "block_page_mode", value)
    conn.commit()
    return flash_redirect("settings_page", "Saved. Takes effect on the next new connection, no restart needed.")


@app.route("/settings/admin", methods=["POST"])
@require_admin
def update_admin():
    username = request.form.get("admin_username", "").strip()
    password = request.form.get("admin_password", "")
    if not username:
        return flash_redirect("settings_page", "Admin username can't be empty.", error=True)
    conn = get_db()
    db.set_setting(conn, "admin_username", username)
    if password:
        db.set_setting(conn, "admin_password_hash", auth.hash_password(password))
    conn.commit()
    return flash_redirect("settings_page", "Saved.")


bootstrap_admin()

_boot_conn = db.get_conn()
app.secret_key = db.get_setting(_boot_conn, "secret_key") or secrets.token_hex(32)
_boot_conn.close()


def main() -> None:
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8787"))
    from waitress import serve

    print(f"dashboard listening on http://{host}:{port}", file=sys.stderr, flush=True)
    serve(app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
