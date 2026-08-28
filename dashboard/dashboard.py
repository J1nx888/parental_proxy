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
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 920px; margin: 1.5rem auto; padding: 0 1rem; line-height: 1.4; }
  nav { display: flex; gap: 1.2rem; margin-bottom: 1.5rem; border-bottom: 1px solid #8884; padding-bottom: .8rem; flex-wrap: wrap; }
  nav a { text-decoration: none; color: inherit; opacity: .6; font-weight: 600; }
  nav a.active { opacity: 1; border-bottom: 2px solid currentColor; }
  h1 { font-size: 1.3rem; }
  h2 { font-size: 1.05rem; margin-top: 2rem; border-bottom: 1px solid #8884; padding-bottom: .3rem; }
  table { width: 100%; border-collapse: collapse; margin-top: .6rem; }
  td, th { text-align: left; padding: .35rem .3rem; border-bottom: 1px solid #8882; font-size: .92rem; }
  th { font-size: .75rem; text-transform: uppercase; opacity: .6; }
  form.inline { display: inline; }
  .add-form { display: flex; gap: .5rem; margin-top: .8rem; flex-wrap: wrap; align-items: center; }
  .add-form input[type=text], .add-form input[type=url], .add-form input[type=password], .add-form select { padding: .4rem; }
  button, .btn { padding: .3rem .7rem; border-radius: 4px; border: none; cursor: pointer; font-size: .85rem; }
  button.danger { background: #c0392b; color: white; }
  button.add, .btn.add { background: #2e7d32; color: white; }
  button.small { padding: .15rem .5rem; font-size: .78rem; }
  .flash { padding: .5rem .8rem; border-radius: 6px; margin-bottom: 1rem; }
  .flash.error { background: #c0392b22; border: 1px solid #c0392b88; }
  .flash.ok { background: #2e7d3222; border: 1px solid #2e7d3288; }
  .hint { font-size: .82rem; opacity: .7; margin: .3rem 0 .8rem; }
  code { background: #8882; padding: .1rem .3rem; border-radius: 3px; }
  .badge { display: inline-block; padding: .05rem .45rem; border-radius: 10px; font-size: .72rem; font-weight: 600; }
  .badge.mode-splice { background: #2e7d3225; }
  .badge.mode-bump { background: #c0392b20; }
  .badge.mode-trusted { background: #8884; }
  .badge.allowed { background: #2e7d3225; color: #2e7d32; }
  .badge.blocked { background: #c0392b20; color: #c0392b; }
  .cert-banner { background: #2e7d3215; border: 1px solid #2e7d3255; border-radius: 8px; padding: .7rem 1rem; margin-bottom: 1.2rem; }
  .cert-banner a.btn { text-decoration: none; }
</style>
</head>
<body>
<h1>Parental Proxy</h1>
<nav>
  <a href="{{ url_for('report') }}" class="{{ 'active' if active=='report' else '' }}">Report</a>
  <a href="{{ url_for('users') }}" class="{{ 'active' if active=='users' else '' }}">Users</a>
  <a href="{{ url_for('domains') }}" class="{{ 'active' if active=='domains' else '' }}">Domains</a>
  <a href="{{ url_for('settings_page') }}" class="{{ 'active' if active=='settings' else '' }}">Settings</a>
</nav>
{% if message %}<div class="flash {{ 'error' if error else 'ok' }}">{{ message }}</div>{% endif %}
{{ body|safe }}
</body>
</html>
"""


def render(active: str, body: str) -> str:
    return render_template_string(
        BASE, active=active, body=body,
        message=request.args.get("message"), error=request.args.get("error"),
    )


def flash_redirect(endpoint: str, message: str, error: bool = False, **kwargs):
    return redirect(url_for(endpoint, message=message, error="1" if error else None, **kwargs))


# ==========================================================
# CA CERT (public, unauthenticated)
# ==========================================================

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


@app.route("/blocked")
def blocked():
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Blocked</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;text-align:center;}"
        "h1{font-size:1.3rem;}</style></head><body>"
        "<h1>This site or show isn't approved.</h1>"
        "<p>Ask a parent to check the dashboard if you think this should be allowed.</p>"
        "</body></html>",
        403,
    )


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

<h2>Users ({{ users|length }})</h2>
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
<form class="add-form" method="post" action="{{ url_for('add_user') }}">
  <input type="text" name="username" placeholder="username, e.g. kid1" required>
  <input type="text" name="display_name" placeholder="Display name, e.g. Alex">
  <input type="password" name="password" placeholder="Password" required>
  <button class="add" type="submit">Add user</button>
</form>
<p class="hint">This username/password is what gets configured in that person's device proxy settings (not the dashboard login).</p>
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
<h2>{{ u.display_name }} <code>({{ u.username }})</code></h2>

<h2>Assigned sites</h2>
<table>
  <tr><th>Domain</th><th>Mode</th></tr>
  {% for d in assigned_domains %}
  <tr><td><code>{{ d.pattern }}</code></td><td><span class="badge mode-{{ d.mode }}">{{ d.mode }}</span></td></tr>
  {% else %}
  <tr><td colspan="2"><em>No per-user sites assigned (still gets global sites).</em></td></tr>
  {% endfor %}
</table>
<p class="hint">Manage assignment from the <a href="{{ url_for('domains') }}">Domains</a> page -- pick the site there and check this user.</p>

<h2>Approved Crunchyroll shows ({{ shows|length }})</h2>
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
<form class="add-form" method="post" action="{{ url_for('add_show') }}">
  <input type="hidden" name="user_id" value="{{ u.id }}">
  <input type="url" name="url" placeholder="https://www.crunchyroll.com/series/GYE5K0XVR/ace-attorney" required style="flex:1; min-width:280px;">
  <input type="text" name="name" placeholder="Name (auto-filled, editable)">
  <button class="add" type="submit">Approve show</button>
</form>

<h2>Change password</h2>
<form class="add-form" method="post" action="{{ url_for('reset_password') }}">
  <input type="hidden" name="user_id" value="{{ u.id }}">
  <input type="password" name="password" placeholder="New password" required>
  <button class="add" type="submit">Update password</button>
</form>
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


DOMAINS_BODY = """
<h2>Domains ({{ domains|length }})</h2>
{% if filtered_user %}
<p class="hint">
  Showing domains assigned to <strong>{{ filtered_user.display_name }}</strong>
  (plus everyone's global domains) --
  <a href="{{ url_for('domains') }}">clear filter</a>
</p>
{% endif %}
<p class="hint">
  <span class="badge mode-splice">splice</span> host-only, never decrypted &nbsp;
  <span class="badge mode-bump">bump</span> fully decrypted, path/show rules apply &nbsp;
  <span class="badge mode-trusted">trusted</span> always passed through, unchecked
</p>
<table>
  <tr><th>Pattern</th><th>Mode</th><th>Access</th><th>Note</th><th></th></tr>
  {% for d in domains %}
  <tr>
    <td><code>{{ d.pattern }}</code></td>
    <td><span class="badge mode-{{ d.mode }}">{{ d.mode }}</span></td>
    <td>{{ 'Everyone' if d.is_global else 'Per-user' }}</td>
    <td>{{ d.note or '' }}</td>
    <td>
      <a class="btn small" href="{{ url_for('domain_detail', domain_id=d.id) }}">Manage</a>
      <form class="inline" method="post" action="{{ url_for('delete_domain') }}">
        <input type="hidden" name="domain_id" value="{{ d.id }}">
        {% if filtered_user %}<input type="hidden" name="user_id" value="{{ filtered_user.id }}">{% endif %}
        <button class="danger small" type="submit" onclick="return confirm('Delete this domain rule?')">Delete</button>
      </form>
    </td>
  </tr>
  {% else %}
  <tr><td colspan="5"><em>No domains configured.</em></td></tr>
  {% endfor %}
</table>

<form class="add-form" method="post" action="{{ url_for('add_domain') }}">
  {% if filtered_user %}<input type="hidden" name="user_id" value="{{ filtered_user.id }}">{% endif %}
  <input type="text" name="pattern" placeholder="e.g. example\\.com" required>
  <select name="mode">
    <option value="splice">splice (host-only)</option>
    <option value="bump">bump (decrypt, path rules)</option>
    <option value="trusted">trusted (always pass, unchecked)</option>
  </select>
  <label><input type="checkbox" name="is_global"> Everyone gets this</label>
  <input type="text" name="note" placeholder="Note (optional)">
  <button class="add" type="submit">Add domain</button>
</form>
<p class="hint">"Everyone gets this" is for shared infrastructure (fonts, auth providers, CDNs) -- leave it unchecked for sites you want to assign to specific users individually.</p>

{% if filtered_user %}
<h2>Approve a specific page for {{ filtered_user.display_name }}</h2>
<p class="hint">Paste a full URL to approve just that page (and anything after it), without opening the rest of the site. Creates the domain in bump mode if it doesn't already exist, adds a path rule derived from the URL, and assigns both to {{ filtered_user.display_name }}.</p>
<form class="add-form" method="post" action="{{ url_for('add_domain_from_url') }}">
  <input type="hidden" name="user_id" value="{{ filtered_user.id }}">
  <input type="url" name="url" placeholder="https://example.com/some/specific/page" required style="flex:1; min-width:320px;">
  <button class="add" type="submit">Approve this page</button>
</form>
{% endif %}
"""


@app.route("/domains")
@require_admin
def domains():
    conn = get_db()
    filter_user_id = request.args.get("user_id", "")
    filtered_user = None
    if filter_user_id:
        filtered_user = conn.execute(
            "SELECT * FROM users WHERE id = ?", (filter_user_id,)
        ).fetchone()
        if filtered_user is None:
            return flash_redirect("domains", "That user no longer exists.", error=True)
    if filtered_user:
        # Same rule the proxy itself uses at request time (matching.py),
        # reused here rather than reimplemented as a second copy of the
        # "is this domain visible to this user" logic.
        all_rows = conn.execute("SELECT * FROM domains ORDER BY is_global DESC, pattern").fetchall()
        rows = [
            d for d in all_rows
            if bool(d["is_global"]) or matching.user_has_domain(conn, filter_user_id, d["id"])
        ]
    else:
        rows = conn.execute("SELECT * FROM domains ORDER BY is_global DESC, pattern").fetchall()
    return render(
        "domains",
        render_template_string(DOMAINS_BODY, domains=rows, filtered_user=filtered_user),
    )


@app.route("/domains/add", methods=["POST"])
@require_admin
def add_domain():
    pattern = request.form.get("pattern", "").strip()
    mode = request.form.get("mode", "splice")
    is_global = 1 if request.form.get("is_global") else 0
    note = request.form.get("note", "").strip() or None
    # Preserves the Users-page "N assigned" filter (?user_id=) across this
    # POST, so adding a domain from that filtered view doesn't silently
    # drop the admin back into the unfiltered list.
    redirect_kwargs = {"user_id": request.form["user_id"]} if request.form.get("user_id") else {}
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
    conn = get_db()
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
    redirect_kwargs = {"user_id": request.form["user_id"]} if request.form.get("user_id") else {}
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
<h2><code>{{ d.pattern }}</code> <span class="badge mode-{{ d.mode }}">{{ d.mode }}</span></h2>
{% if d.kind == 'crunchyroll' %}
<p class="hint">This is the built-in Crunchyroll domain. Shows are approved per-user from each user's page; the paths below are a defense-in-depth safety net, not the main show filter.</p>
{% endif %}

<form class="add-form" method="post" action="{{ url_for('update_domain') }}">
  <input type="hidden" name="domain_id" value="{{ d.id }}">
  <select name="mode">
    <option value="splice" {{ 'selected' if d.mode=='splice' }}>splice (host-only)</option>
    <option value="bump" {{ 'selected' if d.mode=='bump' }}>bump (decrypt, path rules)</option>
    <option value="trusted" {{ 'selected' if d.mode=='trusted' }}>trusted (always pass, unchecked)</option>
  </select>
  <label><input type="checkbox" name="is_global" {{ 'checked' if d.is_global }}> Everyone gets this</label>
  <input type="text" name="note" value="{{ d.note or '' }}" placeholder="Note">
  <button class="add" type="submit">Save</button>
</form>

{% if not d.is_global %}
<h2>Assigned users</h2>
<table>
  <tr><th></th><th>User</th></tr>
  {% for u in all_users %}
  <tr>
    <td>
      <form class="inline" method="post" action="{{ url_for('toggle_user_domain') }}">
        <input type="hidden" name="domain_id" value="{{ d.id }}">
        <input type="hidden" name="user_id" value="{{ u.id }}">
        <input type="hidden" name="action" value="{{ 'remove' if u.id in assigned_ids else 'add' }}">
        <button class="small {{ '' if u.id in assigned_ids else 'add' }}" type="submit">
          {{ 'Remove' if u.id in assigned_ids else 'Grant' }}
        </button>
      </form>
    </td>
    <td>{{ u.display_name }} <code>({{ u.username }})</code></td>
  </tr>
  {% endfor %}
</table>
{% endif %}

{% if d.mode == 'bump' %}
<h2>Allowed paths ({{ paths|length }})</h2>
<p class="hint">Regex patterns matched against the request path. Leave empty to allow any path on this domain once it's otherwise permitted.</p>
{% if prefill_path %}
<p class="hint">A blocked request suggested the pattern below (derived from the actual path that was denied) -- review it, broaden or narrow it as needed, then save.</p>
{% endif %}
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
<form class="add-form" method="post" action="{{ url_for('add_path') }}">
  <input type="hidden" name="domain_id" value="{{ d.id }}">
  <input type="text" name="pattern" placeholder="e.g. ^/discover" value="{{ prefill_path or '' }}" required>
  <button class="add" type="submit">Add path</button>
</form>
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
    assigned_ids = {
        row["user_id"] for row in
        conn.execute("SELECT user_id FROM user_domains WHERE domain_id = ?", (domain_id,))
    }
    paths = conn.execute(
        "SELECT * FROM domain_paths WHERE domain_id = ? ORDER BY pattern", (domain_id,)
    ).fetchall()
    body = render_template_string(
        DOMAIN_DETAIL_BODY, d=d, all_users=all_users, assigned_ids=assigned_ids, paths=paths,
        prefill_path=request.args.get("prefill_path"),
    )
    return render("domains", body)


@app.route("/domains/update", methods=["POST"])
@require_admin
def update_domain():
    domain_id = request.form.get("domain_id", "")
    mode = request.form.get("mode", "splice")
    is_global = 1 if request.form.get("is_global") else 0
    note = request.form.get("note", "").strip() or None
    conn = get_db()
    conn.execute(
        "UPDATE domains SET mode = ?, is_global = ?, note = ? WHERE id = ?",
        (mode, is_global, note, domain_id),
    )
    conn.commit()
    return flash_redirect("domain_detail", "Saved.", domain_id=domain_id)


@app.route("/domains/toggle-user", methods=["POST"])
@require_admin
def toggle_user_domain():
    domain_id = request.form.get("domain_id", "")
    user_id = request.form.get("user_id", "")
    action = request.form.get("action", "add")
    conn = get_db()
    if action == "add":
        conn.execute(
            "INSERT OR IGNORE INTO user_domains (user_id, domain_id) VALUES (?,?)",
            (user_id, domain_id),
        )
    else:
        conn.execute(
            "DELETE FROM user_domains WHERE user_id = ? AND domain_id = ?", (user_id, domain_id)
        )
    conn.commit()
    return flash_redirect("domain_detail", "Updated.", domain_id=domain_id)


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
<h2>Recent activity</h2>
<form class="add-form" method="get" action="{{ url_for('report') }}">
  <select name="user">
    <option value="">All users</option>
    {% for u in all_users %}
    <option value="{{ u.username }}" {{ 'selected' if filter_user==u.username }}>{{ u.display_name }}</option>
    {% endfor %}
  </select>
  <select name="status">
    <option value="">All</option>
    <option value="blocked" {{ 'selected' if filter_status=='blocked' }}>Blocked only</option>
    <option value="allowed" {{ 'selected' if filter_status=='allowed' }}>Allowed only</option>
  </select>
  <button class="add" type="submit">Filter</button>
</form>

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
        <button class="add small" type="submit">Approve for {{ row.username }}</button>
      </form>
      {% endif %}
    </td>
  </tr>
  {% else %}
  <tr><td colspan="6"><em>No activity logged yet.</em></td></tr>
  {% endfor %}
</table>
"""


@app.route("/report")
@require_admin
def report():
    conn = get_db()
    filter_user = request.args.get("user", "")
    filter_status = request.args.get("status", "")
    query = "SELECT * FROM access_log WHERE 1=1"
    params: list = []
    if filter_user:
        query += " AND username = ?"
        params.append(filter_user)
    if filter_status == "blocked":
        query += " AND allowed = 0"
    elif filter_status == "allowed":
        query += " AND allowed = 1"
    query += " ORDER BY id DESC LIMIT 200"
    rows = conn.execute(query, params).fetchall()
    all_users = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
    body = render_template_string(
        REPORT_BODY, rows=rows, all_users=all_users,
        filter_user=filter_user, filter_status=filter_status,
        resolver_error=db.get_setting(conn, "cr_resolver_last_error"),
    )
    return render("report", body)


@app.route("/report/approve", methods=["POST"])
@require_admin
def approve_from_report():
    log_id = request.form.get("log_id", "")
    conn = get_db()
    row = conn.execute("SELECT * FROM access_log WHERE id = ?", (log_id,)).fetchone()
    if row is None or row["user_id"] is None:
        return flash_redirect("report", "Couldn't find that log entry.", error=True)

    if row["series_id"]:
        name = cr_api.series_title(row["series_id"]) or row["series_id"]
        conn.execute(
            "INSERT INTO user_shows (user_id, series_id, series_name) VALUES (?,?,?) "
            "ON CONFLICT(user_id, series_id) DO UPDATE SET series_name = excluded.series_name",
            (row["user_id"], row["series_id"], name),
        )
        conn.commit()
        return flash_redirect("report", f"Approved {name} for {row['username']}.")

    if row["reason"] == "path_not_allowed":
        # The domain is already assigned to this user -- that's *why* the
        # request reached the path check at all -- so INSERT OR IGNORE into
        # user_domains below would be a silent no-op and the identical
        # request would be denied again immediately (GH #6). A path pattern
        # governs future access, not a one-time yes/no, so send the admin
        # to review a derived starting pattern rather than auto-saving one.
        domain = matching.find_domain(conn, row["domain"])
        if domain is None:
            return flash_redirect("report", "Couldn't find that domain anymore.", error=True)
        return redirect(url_for(
            "domain_detail", domain_id=domain["id"], prefill_path=path_to_pattern(row["path"]),
        ))

    domain = matching.find_domain(conn, row["domain"])
    if domain is None:
        # Never seen this domain before (it wasn't blocked by an existing
        # rule -- it just wasn't configured at all). Create it rather than
        # dead-ending: splice mode (host-only, matches the default for any
        # new domain) and scoped to this user only, not global -- the
        # safest default for something approved reactively from a block.
        pattern = re.escape(row["domain"])
        conn.execute(
            "INSERT OR IGNORE INTO domains (pattern, mode, kind, is_global, note, created_at) "
            "VALUES (?, 'splice', 'generic', 0, 'Auto-added from report approval', ?)",
            (pattern, db.now_iso()),
        )
        domain = conn.execute("SELECT * FROM domains WHERE pattern = ?", (pattern,)).fetchone()
    conn.execute(
        "INSERT OR IGNORE INTO user_domains (user_id, domain_id) VALUES (?,?)",
        (row["user_id"], domain["id"]),
    )
    conn.commit()
    return flash_redirect("report", f"Approved {row['domain']} for {row['username']}.")


# ==========================================================
# SETTINGS
# ==========================================================

SETTINGS_BODY = """
<h2>Local network</h2>
<form class="add-form" method="post" action="{{ url_for('update_local_network') }}">
  <input type="text" name="local_network" value="{{ local_network }}" style="flex:1; min-width:280px;">
  <button class="add" type="submit">Save</button>
</form>
<p class="hint">Space-separated CIDRs, e.g. <code>192.168.1.0/24 192.168.0.0/24</code>. Requests from outside these ranges are denied regardless of user/site rules. <strong>Leave blank to disable this check</strong> and rely only on per-person proxy logins &mdash; do that if the proxy runs under Docker Desktop or bridge networking, where it sees an internal gateway address instead of the real client IP and this check would otherwise block everyone.</p>

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

<h2>Dashboard admin login</h2>
<form class="add-form" method="post" action="{{ url_for('update_admin') }}">
  <input type="text" name="admin_username" value="{{ admin_username }}" placeholder="Admin username">
  <input type="password" name="admin_password" placeholder="New password (leave blank to keep current)">
  <button class="add" type="submit">Save</button>
</form>
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
