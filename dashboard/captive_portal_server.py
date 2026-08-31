#!/usr/bin/env python3
"""Phase 4 milestone 3: the captive-portal login server nftables has
been redirecting PREAUTH (unauthenticated_v4) devices' plain-HTTP
traffic to since Phase 3 was designed -- see
phase3/nftables-manager/internal/nft/knftables_adapter.go's
`baselineRules`, which already carries
`ip saddr @unauthenticated_v4 tcp dport 80 redirect to :3131` with a
`# -> future portal` comment in the original design doc. This module
is that future portal; the nftables/interception side needed NO
changes at all.

**Design decision, confirmed by how OS captive-portal detection
actually works (Apple/Google/Microsoft/Firefox all publicly documented
-- see RoadMap.md's dated entry for the exact expected values each OS
checks for) rather than assumed**: every one of these OSes probes a
plain-HTTP URL expecting an EXACT response (Apple:
`captive.apple.com/hotspot-detect.html` -> the literal string
"Success"; Android/Chrome: `.../generate_204` -> a bare 204; Windows:
`www.msftconnecttest.com/connecttest.txt` -> "Microsoft Connect Test";
Firefox: `detectportal.firefox.com/success.txt` -> "success"). Getting
ANYTHING else -- wrong status, wrong body -- is what every one of them
already treats as "there's a captive portal," and each then opens (or
offers to open) exactly the URL it just probed in a real browser/
webview, which -- since nftables redirects by source IP and
destination PORT, not by hostname -- lands right back on THIS server
and renders whatever HTML we send. That means a single handler that
always returns the same login page for every GET, regardless of path
or Host header, is enough to trigger every major OS's native "Sign in
to network" UI AND to show the login form for a device manually typing
a URL -- no per-OS special-casing needed.

**Self-resolving success path, not something this module has to
handle**: once a login succeeds and the device's `is_authenticated`
flag flips, it takes controller/main.py's own next reconcile cycle
(`--poll-interval`, default 5s) to actually move the device's IP from
`unauthenticated_v4` to `authenticated_v4` in the real kernel ruleset
(see controller/policy_state.py). From that point on, this device's
port-80 traffic is no longer redirected here at all -- the OS's own
routine re-probe of its detection URL reaches the REAL Apple/Google/
Microsoft server directly and gets the REAL expected response, which
is what makes the OS dismiss its own captive-portal UI on its own.
Nothing here needs to detect or announce that transition itself.

**No interception for HTTPS (tcp/443) -- a known, deliberate,
documented limitation, not an oversight**: matches virtually every
commercial captive portal (hotel/airport WiFi) in existence, and
`phase3/nftables-manager`'s baseline rules were never designed to
redirect 443 for unauthenticated_v4 either (there is no cert this
project's own CA can present that an ungated device already trusts,
same reasoning as `block_page_server.py`'s own explicit choice never to
terminate TLS for a domain a device hasn't been told to trust). In
practice this is what actually triggers detection for the overwhelming
majority of real usage anyway: the OS's own automatic captive-portal
probe fires immediately upon a new interception generation taking
effect, using plain HTTP specifically because every captive portal
implementation relies on that being interceptable. A technically
determined user who notices the redirect and deliberately avoids ever
completing an HTTP request could evade the prompt indefinitely; RoadMap.md
tracks this explicitly as a possible future hardening item (e.g.
dropping tcp/udp 443 for unauthenticated_v4 too) rather than something
silently added here without verifying it doesn't also break the OS's
own captive-portal-assistant webview, which sometimes needs its own
auxiliary HTTPS requests to render correctly.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import auth
import db
from device_identity import resolve_device

log = logging.getLogger("dashboard.captive_portal_server")

# Brute-force protection, added alongside this module's first version
# rather than retrofitted later (this project's standing security
# practice -- see RoadMap.md's cross-cutting security-by-design
# section): a login form an unauthenticated device can reach with no
# rate limiting at all is an obvious guessing-attack surface,
# especially since a kid's own password is realistically short/weak.
# In-memory only, per source IP, deliberately not a new DB table --
# losing lockout state across a dashboard restart is an acceptable
# tradeoff for a first pass (this is still a LAN-only surface; Phase 6
# would need to revisit this if/when it's ever exposed beyond the LAN).
# A module-level dict + lock (not per-instance) since BaseHTTPRequestHandler
# gets a fresh instance per request/connection -- state has to live
# somewhere that outlives any single request.
_MAX_ATTEMPTS = 5
_WINDOW_SECONDS = 60.0
_failed_attempts: dict[str, list[float]] = {}
_failed_attempts_lock = threading.Lock()


def _is_rate_limited(client_ip: str) -> bool:
    now = time.monotonic()
    with _failed_attempts_lock:
        recent = [t for t in _failed_attempts.get(client_ip, []) if now - t < _WINDOW_SECONDS]
        _failed_attempts[client_ip] = recent
        return len(recent) >= _MAX_ATTEMPTS


def _record_failed_attempt(client_ip: str) -> None:
    with _failed_attempts_lock:
        _failed_attempts.setdefault(client_ip, []).append(time.monotonic())


def _clear_failed_attempts(client_ip: str) -> None:
    with _failed_attempts_lock:
        _failed_attempts.pop(client_ip, None)

_PAGE_TEMPLATE = """\
<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Sign in</title>
<style>
:root{{color-scheme:light dark;}}
body{{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;max-width:24rem;margin:4rem auto;
padding:0 1.25rem;text-align:center;color:#1e293b;}}
@media (prefers-color-scheme:dark){{body{{color:#e5eaf3;}}}}
.icon{{width:56px;height:56px;border-radius:16px;background:#2f6fed;display:inline-flex;
align-items:center;justify-content:center;margin:0 auto 1rem;}}
h1{{font-size:1.15rem;margin:0 0 .5rem;}}
p{{font-size:.92rem;opacity:.75;}}
.error{{color:#dc2626;font-size:.85rem;margin:0 0 .75rem;}}
form{{display:flex;flex-direction:column;gap:.6rem;margin-top:1.25rem;}}
input{{font-size:1rem;padding:.55rem .7rem;border-radius:8px;border:1px solid #94a3b8;
background:transparent;color:inherit;}}
button{{font-size:1rem;padding:.6rem;border-radius:8px;border:none;background:#2f6fed;
color:white;cursor:pointer;}}
</style></head><body>
<div class='icon'>
<svg width='28' height='28' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='2.2'
stroke-linecap='round' stroke-linejoin='round'><path d='M12 3l8 3.5v5.2c0 4.7-3.2 8.6-8 9.8
-4.8-1.2-8-5.1-8-9.8V6.5L12 3z'/></svg>
</div>
<h1>Sign in to use the internet</h1>
<p>This device hasn't logged in yet. Use your own username and password below.</p>
{error}
<form method="post" action="/">
  <input type="text" name="username" placeholder="Username" autocapitalize="none" autocorrect="off" required>
  <input type="password" name="password" placeholder="Password" required>
  <button type="submit">Sign in</button>
</form>
<p>Not your login? Ask a parent to bypass or assign this device from the dashboard instead.</p>
</body></html>
"""

_SUCCESS_TEMPLATE = """\
<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Signed in</title>
<style>
:root{{color-scheme:light dark;}}
body{{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;max-width:24rem;margin:4rem auto;
padding:0 1.25rem;text-align:center;color:#1e293b;}}
@media (prefers-color-scheme:dark){{body{{color:#e5eaf3;}}}}
h1{{font-size:1.15rem;margin:0 0 .5rem;}}
p{{font-size:.92rem;opacity:.75;}}
.note{{background:#fef3c7;color:#92400e;border-radius:8px;padding:.6rem .8rem;
text-align:left;margin-top:1.25rem;}}
@media (prefers-color-scheme:dark){{.note{{background:#3f2d0a;color:#fcd34d;}}}}
</style></head><body>
<h1>You're signed in.</h1>
<p>It can take up to about 10 seconds for this device's internet access to
actually open up. Try reloading whatever page you were on.</p>
{bump_reminder}
</body></html>
"""

# Shown on the success page only when this same user already has a
# DIFFERENT device with SSL-Bump enabled -- see this module's own
# docstring's design-sketch reference (RoadMap.md): the login flow
# only ever grants DNS-tier access, so a kid whose usual device has
# full SSL-Bump refinement logging in from a NEW device here would
# otherwise have no idea why something that works on their other
# device doesn't work on this one.
_BUMP_REMINDER_HTML = (
    '<p class="note">Heads up: this is only basic (DNS-level) access. '
    "Your other device has extra access set up by a parent -- ask them "
    "to do the same for this one if you need it here too.</p>"
)


def _render(username_error: str | None = None) -> bytes:
    error_html = f'<p class="error">{username_error}</p>' if username_error else ""
    return _PAGE_TEMPLATE.format(error=error_html).encode("utf-8")


def _render_success(show_bump_reminder: bool) -> bytes:
    reminder_html = _BUMP_REMINDER_HTML if show_bump_reminder else ""
    return _SUCCESS_TEMPLATE.format(bump_reminder=reminder_html).encode("utf-8")


class _CaptivePortalHandler(BaseHTTPRequestHandler):
    # BaseHTTPRequestHandler logs every request to stderr via
    # log_message() by default -- redirect through the logging module,
    # matching block_page_server.py's own precedent.
    def log_message(self, format: str, *args) -> None:  # noqa: A002 -- matches stdlib's own signature
        log.info("%s - %s", self.address_string(), format % args)

    def _send_html(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Every response here is per-device, per-moment login state --
        # never something an OS's own captive-portal prober or a
        # browser should cache and reuse.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's own naming convention
        # Deliberately the SAME response regardless of path/Host -- see
        # this module's own docstring for why that alone is enough to
        # trigger every major OS's captive-portal-detected UI.
        self._send_html(200, _render())

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length) if length else b""
        fields = parse_qs(raw_body.decode("utf-8", errors="replace"))
        username = (fields.get("username") or [""])[0].strip()
        password = (fields.get("password") or [""])[0]

        conn = db.get_conn()
        try:
            self._handle_login(conn, username, password)
        finally:
            conn.close()

    def _handle_login(self, conn: sqlite3.Connection, username: str, password: str) -> None:
        client_ip = self.client_address[0]
        if _is_rate_limited(client_ip):
            log.warning("rate-limited login attempt from %s", client_ip)
            self._send_html(
                200, _render("Too many attempts -- wait a minute before trying again.")
            )
            return

        device = resolve_device(conn, client_ip)
        if device is None:
            # By construction this request could only have reached us
            # via nftables' unauthenticated_v4 redirect, which requires
            # an active device_bindings row to exist in the first
            # place -- reaching this branch means it was deactivated in
            # the narrow window since, not the common case. Fails
            # closed (no login granted) with an honest explanation
            # rather than silently retrying.
            log.warning("login attempt from %s but no active binding was found", client_ip)
            self._send_html(
                200, _render("We couldn't identify your device on the network yet -- try again shortly.")
            )
            return

        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None or not auth.verify_password(password, user["password_hash"]):
            _record_failed_attempt(client_ip)
            log.info("failed login for username=%r from device %s", username, device["mac_address"])
            self._send_html(200, _render("Incorrect username or password."))
            return

        _clear_failed_attempts(client_ip)

        # Grants DNS-tier access ONLY -- never bump_enabled, matching
        # Phase 4's design sketch exactly (RoadMap.md). COALESCE so a
        # device an admin already assigned to a different user (or a
        # group) keeps that assignment; this login only fills in
        # user_id when nothing has claimed the device yet.
        conn.execute(
            "UPDATE devices SET is_authenticated = 1, user_id = COALESCE(user_id, ?) WHERE id = ?",
            (user["id"], device["id"]),
        )
        conn.commit()
        log.info("device %s authenticated as %s", device["mac_address"], username)

        # Does this same user already have a DIFFERENT device with
        # SSL-Bump enabled? If so, this login -- DNS-tier only, always
        # -- is a real step down from what they're used to elsewhere,
        # worth explaining rather than leaving them to wonder why
        # something that works on their other device doesn't work here.
        has_bump_elsewhere = conn.execute(
            "SELECT 1 FROM devices WHERE user_id = ? AND bump_enabled = 1 AND id != ? LIMIT 1",
            (user["id"], device["id"]),
        ).fetchone() is not None
        self._send_html(200, _render_success(has_bump_elsewhere))


def start(host: str = "0.0.0.0", port: int = 3131) -> ThreadingHTTPServer:
    """Starts the captive-portal server on its own background thread
    and returns the live server object -- call `.shutdown()` on it to
    stop. `ThreadingHTTPServer` (not the single-threaded `HTTPServer`,
    same reasoning as block_page_server.py) so one slow/hanging client
    can't block every other gated device's login attempt, and so each
    request handler runs on its own thread -- required here (unlike
    block_page_server.py) since do_POST opens its own sqlite3
    connection per request; sqlite3.Connection objects are only usable
    from the thread that created them.
    """
    server = ThreadingHTTPServer((host, port), _CaptivePortalHandler)
    thread = threading.Thread(target=server.serve_forever, name="captive-portal-server", daemon=True)
    thread.start()
    return server
