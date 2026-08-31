#!/usr/bin/env python3
"""A tiny, dependency-free HTTP server for the AdGuard-side friendly
block page -- added 2026-08-30, the third of that session's three
recommended next steps ("a friendly landing page for the blocked
case").

Why this is a SEPARATE server from the main Flask dashboard, and why
it only ever helps for plain HTTP:

`controller/adguard_sync.py` can point a hard-denied domain's DNS
answer at this machine's LAN IP via AdGuard's `$dnsrewrite` modifier
(confirmed live 2026-08-30 -- see that module's own docstring) instead
of the default 0.0.0.0. A browser that was told "crunchyroll.com is at
<this box>" then connects here directly, on whatever port it wanted --
port 80 for a plain `http://` request, port 443 for `https://`. This
server exists specifically to catch the port-80 case and answer with a
real page; there's no equivalent for port 443, and there deliberately
never will be here: the ONLY way to terminate TLS for an arbitrary
domain a browser trusts is a certificate that domain's real CA issued,
or one the DEVICE has already been told to trust -- and non-bump
devices are, BY DESIGN, never asked to trust this project's own
SSL-Bump CA (`proxy/squid.conf.template`'s whole reason to exist is
giving that trust ONLY to devices an admin deliberately opted in).
Terminating TLS here anyway would show every non-bump device a
"your connection is not private" certificate warning for every hard-
denied HTTPS domain -- objectively worse than today's plain connection
failure, by this project's own already-established reasoning (see
`dashboard.py`'s `SETTINGS_BODY` card on `block_page_mode`, which
defaults Squid's own equivalent choice to "just fail the connection"
for exactly this reason). So: port 443 here just refuses the
connection (nothing listens), identical in effect to the pre-2026-08-30
default -- no worse. Port 80 gets a real page.

Deliberately NOT the same Flask `/blocked` route the Squid path uses:
that route correlates against a recent `access_log` row Squid's own
helpers wrote by matching on identity+timing, which this server doesn't
need -- it already has the one piece of context that matters, directly
from the request itself: the `Host` header IS the blocked domain, no
correlation required. Deliberately still no "Request approval" flow
here (unlike /blocked) -- that would need a reactive UI wired to this
specific write path; a clear, simpler scope for this pass.

**2026-08-31 -- this module now DOES write to access_log** (see
`_respond()` below), closing a real gap found while scoping tighter
Squid/AdGuard integration (RoadMap.md's dated entry, GH #9): until this
fix, "AdGuard never touches this project's database at all" was
literally true, so every AdGuard-side block -- including the NEW
splice-tier enforcement `controller/adguard_sync.py`'s
`build_splice_deny_rules()` adds -- was completely invisible on the
Report page, not even the block itself. Identity is resolved the same
way the Squid helpers do (`common/device_identity.py`'s
`resolve_device()`/`resolve_user_for_device()`), from the requesting
socket's own address -- this server sees the real LAN client IP
directly (no `%>a`-style macro needed, it's a plain HTTP server), so no
proxy-specific translation is required.
"""
from __future__ import annotations

import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import db
import device_identity
import logging_util

log = logging.getLogger("dashboard.block_page_server")

_PAGE_TEMPLATE = """\
<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Blocked</title>
<style>
:root{{color-scheme:light dark;}}
body{{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;max-width:28rem;margin:4rem auto;
padding:0 1.25rem;text-align:center;color:#1e293b;}}
@media (prefers-color-scheme:dark){{body{{color:#e5eaf3;}}}}
.icon{{width:56px;height:56px;border-radius:16px;background:#2f6fed;display:inline-flex;
align-items:center;justify-content:center;margin-bottom:1rem;}}
h1{{font-size:1.15rem;margin:0 0 .5rem;}}
p{{font-size:.92rem;opacity:.75;}}
code{{opacity:.6;}}
</style></head><body>
<div class='icon'>
<svg width='28' height='28' viewBox='0 0 24 24' fill='none' stroke='white' stroke-width='2.2'
stroke-linecap='round' stroke-linejoin='round'><path d='M12 3l8 3.5v5.2c0 4.7-3.2 8.6-8 9.8
-4.8-1.2-8-5.1-8-9.8V6.5L12 3z'/></svg>
</div>
<h1>This site isn't approved.</h1>
<p>Ask a parent to check the dashboard if you think this should be allowed.</p>
<p><code>{host}</code></p>
</body></html>
"""


class _BlockPageHandler(BaseHTTPRequestHandler):
    # BaseHTTPRequestHandler logs every request to stderr via
    # log_message() by default -- redirect through the logging module
    # instead, matching the rest of this project's logging setup,
    # rather than raw prints from a stdlib class.
    def log_message(self, format: str, *args) -> None:  # noqa: A002 -- matches stdlib's own signature
        log.info("%s - %s", self.address_string(), format % args)

    def _log_block(self, host: str) -> None:
        """Record this hit in access_log, same as the Squid helpers do --
        see this module's own docstring for why this write path didn't
        exist before 2026-08-31. A fresh connection per request (this is a
        low-traffic, one-off HTTP server, not a long-lived process with a
        pooled connection) -- same pattern captive_portal_server.py already
        uses for the same reason."""
        conn = None
        try:
            conn = db.get_conn()
            device = device_identity.resolve_device(conn, self.client_address[0])
            user = device_identity.resolve_user_for_device(conn, device) if device is not None else None
            user_id, username, device_id = device_identity.log_identity_fields(device, user)
            logging_util.log_access(
                conn, user_id=user_id, username=username, domain=host, path=None,
                allowed=False, reason="dns_tier_denied", device_id=device_id,
            )
            conn.commit()
        except Exception:
            # Best-effort: a DB hiccup here (including db.get_conn() itself
            # failing) should never take down the actual block page a kid
            # is looking at -- log and move on, same "never let logging
            # break the real response" posture logging_util's own callers
            # already have (they're allowed to raise, but this server has
            # no equivalent of Squid retrying the whole connection on a
            # helper failure).
            log.warning("failed to log block-page hit for %s", host, exc_info=True)
        finally:
            if conn is not None:
                conn.close()

    def _respond(self) -> None:
        host = self.headers.get("Host", "this site")
        # Strip a trailing :port from the Host header -- browsers
        # include it for a non-default port, but showing "site.com:80"
        # to a kid asking a parent about it is just noise.
        host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        self._log_block(host)
        body = _PAGE_TEMPLATE.format(host=host).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's own naming convention
        self._respond()

    def do_HEAD(self) -> None:  # noqa: N802
        self._respond()

    def do_POST(self) -> None:  # noqa: N802
        self._respond()


def start(host: str = "0.0.0.0", port: int = 80) -> ThreadingHTTPServer:
    """Starts the block-page server on its own background thread and
    returns the live server object -- call `.shutdown()` on it to stop.
    `ThreadingHTTPServer` (not the single-threaded `HTTPServer`) so one
    slow/hanging client can't block every other device's request --
    traffic here is expected to be rare, but a household has more than
    one device.
    """
    server = ThreadingHTTPServer((host, port), _BlockPageHandler)
    thread = threading.Thread(target=server.serve_forever, name="block-page-server", daemon=True)
    thread.start()
    return server
